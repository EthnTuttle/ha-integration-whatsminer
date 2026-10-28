"""External-derate detection (v2.0): the miner runs under a limit nobody here
commanded — the firmware lowered it on its own (2026-10-17: error 2320 and a
silent derate to 1224 W under a 2649 W plan).

The flag arms when the reported limit sits more than DERATE_MIN_DELTA_W below
the last limit WE sent, for DERATE_POLLS polls spanning at least DERATE_MIN_S,
outside the DERATE_POST_WRITE_S window after our own write, while mining and
tuned; it clears after DERATE_POLLS polls back at the commanded limit.

When it fires the fault evidence notes the derated-TO power, NOT the refused
commanded value (the livelock fix: 2026-10-17/18 commanded 1684 W into a miner
derated at 1224 W forever), so the fault ceiling converges onto what the
hardware demonstrated: ceiling = reported + PROVEN_MARGIN_W.

Self-contained scaffolding (its own rig; see tests/ha_stubs.py).
"""
from __future__ import annotations

import asyncio
import importlib
import pathlib
import sys
import types

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import ha_stubs  # noqa: E402

ha_stubs.install()

PKG = pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "whatsminer"
pkg = types.ModuleType("wm")
pkg.__path__ = [str(PKG)]
sys.modules.setdefault("wm", pkg)
controller_mod = importlib.import_module("wm.controller")
pn = sys.modules["homeassistant.components.persistent_notification"]
Store = sys.modules["homeassistant.helpers.storage"].Store

C = controller_mod
T0 = 1_700_000_000.0


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


class Rig:
    """A controller plus the fakes around it."""

    def __init__(self, monkeypatch):
        Store.saved.clear()
        pn.created.clear()
        pn.dismissed.clear()
        self.clock = Clock()
        monkeypatch.setattr(controller_mod, "time", self.clock)
        self.hass = ha_stubs.FakeHass()
        self.coord = ha_stubs.FakeCoordinator()
        self.pid_state: dict = {}
        entry = types.SimpleNamespace(entry_id="e1")
        self.ctl = controller_mod.WhatsminerController(
            self.hass, entry, self.coord, self.pid_state, {}
        )


def run_async(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _commanded_rig(monkeypatch, limit=2400, tuned=True):
    """A rig whose controller already sent one limit (the derate reference).

    The write goes out as a safety-class command: this fixture establishes
    the derate reference directly, whether or not the miner is tuning — the
    write gate is not what these tests exercise.
    """
    rig = Rig(monkeypatch)
    if tuned:
        rig.coord.data["up_freq_finish"] = 1

    async def go():
        assert await rig.ctl._set_power_limit(limit, why="test", safety=True)

    run_async(go())
    return rig


def _polls(rig, n, gap_s=40.0, mining=True):
    for _ in range(n):
        rig.clock.t += gap_s
        rig.ctl._note_derate(mining, rig.clock.t)


def _past_the_post_write_window(rig):
    rig.clock.t += C.DERATE_POST_WRITE_S + 1  # the post-write window gates, not counts


# ------------------------------------------------------------- it arms


def test_a_sustained_derate_arms_and_notes_the_derated_power(monkeypatch):
    rig = _commanded_rig(monkeypatch)
    rig.coord.data["wattage_limit"] = 2200  # we asked for 2400; the miner derated itself
    _past_the_post_write_window(rig)
    _polls(rig, 3)
    assert rig.ctl._external_derate is False  # 3 polls but only 80 s of evidence
    _polls(rig, 1)
    assert rig.ctl._external_derate is True  # >= 3 polls spanning >= 120 s
    # The livelock fix: the fault power is the derated-TO level, NOT the 2400 W
    # we commanded and it refused.
    assert rig.ctl._fault_power == 2200
    assert "external derate" in rig.ctl._fault_reason
    assert "2200" in rig.ctl._fault_reason and "2400" in rig.ctl._fault_reason
    assert rig.ctl._hold_tainted is True  # the run in progress proves nothing anymore
    assert rig.pid_state["external_derate"] is True
    faults = rig.pid_state["faults"]
    assert faults["fault_power_w"] == 2200
    assert faults["fault_reason"] == rig.ctl._fault_reason
    # And the ceiling sits PROVEN_MARGIN_W above the demonstrated power.
    assert rig.ctl._bounds(rig.clock.t) == (1000, 2200 + C.PROVEN_MARGIN_W)
    assert faults["ceiling_w"] == 2200 + C.PROVEN_MARGIN_W


def test_a_gap_at_or_under_100_w_never_arms(monkeypatch):
    rig = _commanded_rig(monkeypatch)
    _past_the_post_write_window(rig)
    rig.coord.data["wattage_limit"] = 2300  # exactly -100 W: within tolerance
    _polls(rig, 8)
    assert rig.ctl._external_derate is False
    assert rig.ctl._derate_candidate_since is None
    rig.coord.data["wattage_limit"] = 2299  # -101 W: a real gap
    _polls(rig, 6)
    assert rig.ctl._external_derate is True


def test_two_polls_or_a_short_window_never_arms(monkeypatch):
    rig = _commanded_rig(monkeypatch)
    _past_the_post_write_window(rig)
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
    rig = _commanded_rig(monkeypatch)
    rig.coord.data["wattage_limit"] = 1000
    _polls(rig, 2, gap_s=30.0)  # 30 s and 60 s after our write
    assert rig.ctl._derate_candidate_since is None
    _polls(rig, 1, gap_s=31.0)  # 91 s: the window has closed, evidence starts here
    assert rig.ctl._derate_candidate_since == rig.clock.t


def test_an_intentional_stop_gates_the_detection(monkeypatch):
    rig = _commanded_rig(monkeypatch)
    _past_the_post_write_window(rig)
    rig.coord.data["wattage_limit"] = 1000
    _polls(rig, 6, mining=False)  # Mining Control OFF: the limit reading means nothing
    assert rig.ctl._external_derate is False
    assert rig.ctl._derate_candidate_since is None


def test_still_tuning_gates_the_detection(monkeypatch):
    rig = _commanded_rig(monkeypatch, tuned=False)  # up-freq-finish never 1
    _past_the_post_write_window(rig)
    rig.coord.data["wattage_limit"] = 1000
    _polls(rig, 8)
    assert rig.ctl._external_derate is False
    assert rig.ctl._derate_candidate_since is None


def test_a_never_commanded_limit_has_no_derate_reference(monkeypatch):
    rig = Rig(monkeypatch)
    rig.coord.data["up_freq_finish"] = 1
    rig.clock.t += 1000.0
    rig.coord.data["wattage_limit"] = 1000
    _polls(rig, 6)
    assert rig.ctl._external_derate is False
    assert rig.ctl._derate_candidate_since is None


def test_it_clears_after_three_polls_back_at_the_commanded_limit(monkeypatch):
    rig = _commanded_rig(monkeypatch)
    _past_the_post_write_window(rig)
    rig.coord.data["wattage_limit"] = 1200
    _polls(rig, 6)
    assert rig.ctl._external_derate is True
    rig.coord.data["wattage_limit"] = 2400  # the miner recovered to what we asked
    _polls(rig, 2)
    assert rig.ctl._external_derate is True  # still, two matching polls only
    _polls(rig, 1)
    assert rig.ctl._external_derate is False
    assert rig.pid_state["external_derate"] is False
    # The flag cleared; the fault evidence (and its ceiling) stays for the TTL.
    assert rig.ctl._fault_power == 1200
    assert rig.ctl._bounds(rig.clock.t)[1] == 1200 + C.PROVEN_MARGIN_W


def test_telemetry_adoption_never_masks_the_derate(monkeypatch):
    """A persistent reported limit is adopted as the ceiling in force
    (_limit_w; see _note_reported_limit); the derate reference
    _limit_commanded must NOT follow it, or the adoption would hide the very
    derate we're watching for.
    """
    rig = _commanded_rig(monkeypatch, limit=3200)
    rig.ctl._proven_max = None
    rig.clock.t += 700.0  # past the post-write window and the sent-limit window
    rig.coord.data["wattage_limit"] = 1224  # firmware derates itself, and persists
    for _ in range(3):
        rig.clock.t += 30.0
        rig.ctl._note_reported_limit(True, rig.clock.t)
    assert rig.ctl._limit_w == 1224  # adopted
    assert rig.ctl._limit_commanded == 3200  # the derate reference held
    assert rig.ctl._external_derate is False  # not enough evidence yet
    for _ in range(5):
        rig.clock.t += 40.0
        rig.ctl._note_derate(True, rig.clock.t)
    assert rig.ctl._external_derate is True
    assert rig.ctl._fault_power == 1224  # the derated-TO level, not the 3200 we sent
    assert rig.ctl._bounds(rig.clock.t)[1] == 1224 + C.PROVEN_MARGIN_W
