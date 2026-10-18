"""External-derate detection (1.14): the miner runs under a limit nobody here
commanded — the firmware lowered it on its own (2026-10-17: error 2320 and a
silent derate to 1224 W under a 2649 W plan).
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_controller_smoke import Rig, const, controller_mod, run_async  # noqa: E402
from test_power_regulation import v3_rig  # noqa: E402

C = controller_mod
DAILY = {const.CONF_CONTROL_STRATEGY: "daily"}  # a quiet strategy: no plan, no commands


def _setup_legacy(monkeypatch, **over):
    rig = Rig(monkeypatch, **{**DAILY, **over})

    async def go():
        await rig.setup()
        assert await rig.ctl._set_power_limit(2400, why="test")

    run_async(go())
    return rig


def _polls(rig, n, gap_s=40.0, mining=True):
    for _ in range(n):
        rig.clock.t += gap_s
        rig.ctl._note_derate(mining, rig.clock.t)


def test_a_real_sustained_external_derate_arms_and_is_fault_evidence(monkeypatch):
    rig = _setup_legacy(monkeypatch)
    rig.clock.t += C.DERATE_POST_WRITE_S + 1  # the post-write window gates, not counts
    rig.coord.data["wattage_limit"] = 2200  # we asked for 2400; the miner derated itself
    _polls(rig, 3)
    assert rig.ctl._external_derate is False  # 3 polls but only 80 s of evidence
    _polls(rig, 1)
    assert rig.ctl._external_derate is True  # >= 3 polls spanning >= 120 s
    assert rig.ctl._fault_power == 2400  # the power it failed to hold, not the 2200 it chose
    assert "external derate" in rig.ctl._fault_reason
    assert rig.ctl._hold_tainted is True  # the run in progress proves nothing anymore
    assert rig.pid_state["external_derate"] is True
    # No witnessed hold -> the cap sits PROVEN_MARGIN_W under the fault power.
    assert rig.pid_state["proven_ceiling"]["plan_cap"] == 2400 - C.PROVEN_MARGIN_W
    assert rig.pid_state["proven_ceiling"]["plan_cap_armed"] is True


def test_a_gap_at_or_under_100_w_never_arms(monkeypatch):
    rig = _setup_legacy(monkeypatch)
    rig.clock.t += C.DERATE_POST_WRITE_S + 1
    rig.coord.data["wattage_limit"] = 2300  # exactly -100 W: within tolerance
    _polls(rig, 8)
    assert rig.ctl._external_derate is False
    assert rig.ctl._derate_candidate_since is None
    rig.coord.data["wattage_limit"] = 2299  # -101 W: a real gap
    _polls(rig, 6)
    assert rig.ctl._external_derate is True


def test_two_polls_or_a_short_window_never_arms(monkeypatch):
    rig = _setup_legacy(monkeypatch)
    rig.clock.t += C.DERATE_POST_WRITE_S + 1
    rig.coord.data["wattage_limit"] = 1000
    _polls(rig, 2, gap_s=300.0)  # a long window, but only two polls
    assert rig.ctl._external_derate is False
    rig.coord.data["wattage_limit"] = 2400
    _polls(rig, 1)  # back at the commanded limit: any candidate is forgotten
    rig.coord.data["wattage_limit"] = 1000
    _polls(rig, 8, gap_s=10.0)  # plenty of polls, only 70 s of evidence
    assert rig.ctl._external_derate is False
    _polls(rig, 2, gap_s=30.0)
    assert rig.ctl._external_derate is True  # evidence now spans 130 s


def test_the_post_write_window_starts_no_candidate(monkeypatch):
    rig = _setup_legacy(monkeypatch)
    rig.coord.data["wattage_limit"] = 1000
    _polls(rig, 2, gap_s=30.0)  # 30 s and 60 s after our write
    assert rig.ctl._derate_candidate_since is None
    _polls(rig, 1, gap_s=31.0)  # 91 s: the window has closed, evidence starts here
    assert rig.ctl._derate_candidate_since == rig.clock.t


def test_an_intentional_stop_gates_the_detection(monkeypatch):
    rig = _setup_legacy(monkeypatch)
    rig.clock.t += C.DERATE_POST_WRITE_S + 1
    rig.coord.data["wattage_limit"] = 1000
    _polls(rig, 6, mining=False)  # Mining Control OFF: the limit reading means nothing
    assert rig.ctl._external_derate is False
    assert rig.ctl._derate_candidate_since is None


def test_a_never_commanded_limit_has_no_derate_reference(monkeypatch):
    rig = Rig(monkeypatch, **DAILY)
    run_async(rig.setup())
    rig.clock.t += 1000.0
    rig.coord.data["wattage_limit"] = 1000
    _polls(rig, 6)
    assert rig.ctl._external_derate is False


def test_it_clears_after_three_polls_back_at_the_commanded_limit(monkeypatch):
    rig = _setup_legacy(monkeypatch)
    rig.clock.t += C.DERATE_POST_WRITE_S + 1
    rig.coord.data["wattage_limit"] = 1200
    _polls(rig, 6)
    assert rig.ctl._external_derate is True
    rig.coord.data["wattage_limit"] = 2400  # the miner recovered to what we asked
    _polls(rig, 2)
    assert rig.ctl._external_derate is True  # still, two matching polls only
    _polls(rig, 1)
    assert rig.ctl._external_derate is False
    assert rig.pid_state["external_derate"] is False


def test_the_restored_legacy_limit_is_the_derate_reference_from_boot(monkeypatch):
    from test_controller_smoke import Store

    rig = Rig(monkeypatch, **DAILY)
    Store.saved["whatsminer.e1.controller"] = {"power_limit_w": 2400}
    run_async(rig.setup())
    assert rig.ctl._limit_commanded == 2400
    rig.clock.t += C.DERATE_POST_WRITE_S + 1  # the restored reference double-gates the boot
    rig.coord.data["wattage_limit"] = 1224  # still derated across the HA restart
    _polls(rig, 5)
    assert rig.ctl._external_derate is True


def test_the_full_poll_path_arms_it_and_sends_no_commands(monkeypatch):
    rig = _setup_legacy(monkeypatch)

    async def go():
        rig.clock.t += C.DERATE_POST_WRITE_S + 5
        rig.coord.data["wattage_limit"] = 2200
        for _ in range(5):
            await rig.tick(30.0)
        assert rig.ctl._external_derate is True
        assert rig.pid_state["external_derate"] is True
        # The detection is observation: the daily strategy itself sent nothing.
        assert rig.calls == [("set_power_limit", 2400)]

    run_async(go())


def test_v3_telemetry_adoption_never_masks_the_derate(monkeypatch):
    """v3 adopts a persistent reported limit as _limit_w (see
    _note_regulation_poll); the derate reference _limit_commanded must NOT
    follow it, or the adoption would hide the very derate we're watching for.
    """
    rig = v3_rig(monkeypatch)  # tuned, mining, 3000 W

    async def go():
        await rig.setup()
        assert await rig.ctl._set_power_limit(3200, why="ceiling")  # our last word on the limit
        rig.clock.t += 700.0  # past the post-write window and the v2 send window
        rig.coord.data["wattage_limit"] = 1224  # firmware derates itself, and persists
        for _ in range(3):
            rig.clock.t += 30.0
            rig.ctl._note_regulation_poll(True, rig.clock.t)
        assert rig.ctl._limit_w == 1224  # adopted (v3 contract)
        assert rig.ctl._limit_commanded == 3200  # the derate reference held
        assert rig.ctl._external_derate is False  # not enough evidence yet
        for _ in range(5):
            rig.clock.t += 40.0
            rig.ctl._note_derate(True, rig.clock.t)
        assert rig.ctl._external_derate is True
        assert rig.ctl._fault_power == 3200

    run_async(go())


def test_v3_while_tuning_gates_the_detection(monkeypatch):
    rig = v3_rig(monkeypatch, up=0)  # still tuning

    async def go():
        await rig.setup()
        assert await rig.ctl._set_power_limit(3200, why="ceiling")
        rig.clock.t += 700.0
        rig.coord.data["wattage_limit"] = 1224
        _polls(rig, 6)

    run_async(go())
    assert rig.ctl._external_derate is False
