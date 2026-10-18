"""Proven ceiling (1.14): once the miner itself has faulted (error code,
external derate, hashrate-collapse undo), the daily plan and v2 ceiling
raises cap at what the miner has proven it can hold — until 7 clean days.
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_controller_smoke import T0, Rig, Store, const, controller_mod, run_async  # noqa: E402
from test_daily import fill, forecast, plan  # noqa: E402
from test_power_regulation import cmds, tune, v3_rig  # noqa: E402

C = controller_mod
DAILY = {const.CONF_CONTROL_STRATEGY: "daily"}
KEY = "whatsminer.e1.controller"


# ----------------------------------------------------------------- witness


def test_a_stable_run_witnesses_the_highest_held_power(monkeypatch):
    rig = Rig(monkeypatch, **DAILY)
    rig.coord.data["wattage_limit"] = 3000

    async def go():
        await rig.setup()
        assert rig.ctl._proven_max is None
        await rig.tick()
        assert rig.ctl._proven_max == 3000
        proven = rig.pid_state["proven_ceiling"]
        assert proven["proven_max_w"] == 3000 and proven["proven_max_at"] is not None
        assert proven["plan_cap"] == 5000 and proven["plan_cap_armed"] is False  # no fault: no cap
        assert Store.saved[KEY]["proven_max"] == 3000

    run_async(go())


def test_a_tainted_run_witnesses_nothing(monkeypatch):
    rig = Rig(monkeypatch, **DAILY)
    rig.coord.data["wattage_limit"] = 3000

    async def go():
        await rig.setup()
        rig.ctl._note_fault(rig.clock.t, 3000, "error code 2320")  # taints the run in progress
        await rig.tick()
        assert rig.ctl._proven_max is None

    run_async(go())


def test_a_witness_waits_out_a_recent_command(monkeypatch):
    # Same rule as the floor learner: a limit in force for under a full
    # stable run proves nothing yet.
    rig = Rig(monkeypatch, **DAILY)

    async def go():
        await rig.setup()
        await rig.ctl._set_power_limit(3000, why="test")
        rig.coord.data["wattage_limit"] = 3000
        await rig.tick(30.0)
        assert rig.ctl._proven_max is None  # sent seconds ago: not creditable

    run_async(go())


# -------------------------------------------------------------------- cap


def test_fault_evidence_without_a_witness_caps_at_fault_minus_margin(monkeypatch):
    rig = Rig(monkeypatch, **DAILY)

    async def go():
        await rig.setup()
        rig.coord.data["error_codes"] = {"2320": "2026-10-17 17:33:06"}
        await rig.tick()  # arms; the same poll must NOT witness the run it faults
        assert rig.ctl._proven_max is None
        assert rig.pid_state["proven_ceiling"]["plan_cap"] == 4200 - C.PROVEN_MARGIN_W
        fill(rig, power=3000.0, outdoor=35.0, supply=104.0)
        p = plan(rig, forecast(rig, 20.0))
        assert p["p_ff"] == 4500.0  # what the uncapped plan would have said
        assert p["power_w"] == 3950
        assert "capped by proven max 3950 W" in p["reason"]

    run_async(go())


def test_a_witnessed_hold_caps_at_proven_plus_margin(monkeypatch):
    rig = Rig(monkeypatch, **DAILY)
    rig.coord.data["wattage_limit"] = 3000

    async def go():
        await rig.setup()
        await rig.tick()  # clean stable run at 3000: witnessed
        assert rig.ctl._proven_max == 3000
        rig.coord.data["error_codes"] = {"2320": "2026-10-17 17:33:06"}
        await rig.tick()
        assert rig.pid_state["proven_ceiling"]["plan_cap"] == 3000 + C.PROVEN_MARGIN_W
        fill(rig, power=3000.0, outdoor=35.0, supply=104.0)
        p = plan(rig, forecast(rig, 20.0))
        assert p["power_w"] == 3250
        assert "capped by proven max 3250 W" in p["reason"]

    run_async(go())


def test_the_floor_wins_an_inversion_and_logs_once(monkeypatch, caplog):
    rig = Rig(monkeypatch, **DAILY)

    async def go():
        await rig.setup()
        rig.ctl._fault_at = rig.clock.t
        rig.ctl._fault_power = 1100  # fault minus margin sits under the floor
        rig.ctl._fault_reason = "error code 2320"
        caplog.set_level("INFO", logger="wm.controller")
        assert rig.ctl._proven_cap(rig.clock.t) == (1000, True)  # the floor stands
        assert rig.ctl._cap_inversion_logged is True
        assert sum("the floor stands" in r.message for r in caplog.records) == 1
        rig.ctl._proven_cap(rig.clock.t)
        assert sum("the floor stands" in r.message for r in caplog.records) == 1  # once
        fill(rig, power=1200.0, outdoor=40.0, supply=104.0)
        p = plan(rig, forecast(rig, 30.0))
        assert p["power_w"] == 1000  # floor, even armed

    run_async(go())


def test_fault_evidence_expires_after_seven_days(monkeypatch):
    rig = Rig(monkeypatch, **DAILY)

    async def go():
        await rig.setup()
        rig.ctl._note_fault(rig.clock.t, 4200, "error code 2320")
        assert rig.ctl._proven_cap(rig.clock.t) == (3950, True)
        rig.clock.t += C.FLOOR_PROVEN_TTL_S - 60
        assert rig.ctl._proven_cap(rig.clock.t)[1] is True  # still armed inside 7 days
        rig.clock.t += 120.0
        assert rig.ctl._proven_cap(rig.clock.t) == (5000, False)  # past it: Power Max again

    run_async(go())


def test_the_option_off_is_exact_parity(monkeypatch):
    rig = Rig(monkeypatch, **DAILY, **{const.CONF_PROVEN_CEILING: False})
    from test_controller_smoke import pn

    async def go():
        await rig.setup()
        rig.coord.data["error_codes"] = {"2320": "2026-10-17 17:33:06"}
        await rig.tick()  # the notification still fires; the cap must not
        assert "whatsminer_miner_error_2320" in [k for k, _, _ in pn.created]
        assert rig.pid_state["proven_ceiling"]["plan_cap"] == 5000
        assert rig.pid_state["proven_ceiling"]["plan_cap_armed"] is False
        fill(rig, power=3000.0, outdoor=35.0, supply=104.0)
        p = plan(rig, forecast(rig, 20.0))
        assert p["power_w"] == 4500 and "capped" not in p["reason"]

    run_async(go())


def test_proven_state_survives_a_restart_and_expires_stored_too(monkeypatch):
    rig = Rig(monkeypatch, **DAILY)
    Store.saved[KEY] = {
        "proven_max": 3000, "proven_max_at": T0 - 100.0,
        "proven_fault_at": T0 - 50.0, "proven_fault_power": 3000,
        "proven_fault_reason": "error code 2320",
    }

    async def go():
        await rig.setup()
        assert rig.ctl._proven_max == 3000 and rig.ctl._fault_power == 3000
        assert rig.ctl._proven_cap(rig.clock.t) == (3250, True)

    run_async(go())

    rig = Rig(monkeypatch, **DAILY)  # an 8-day-old fault is no longer evidence
    Store.saved[KEY] = {"proven_fault_at": T0 - 8 * 86400.0, "proven_fault_power": 3000}

    async def go2():
        await rig.setup()
        assert rig.ctl._proven_cap(rig.clock.t) == (5000, False)

    run_async(go2())


# ---------------------------------------------------------- v2 ceiling (v3)


def _armed_store(fault_at=T0, **extra):
    store = {
        "proven_fault_at": fault_at, "proven_fault_power": 2500,
        "proven_fault_reason": "error code 2320",  # no witness: cap = 2250
    }
    store.update(extra)
    return store


def test_an_armed_cap_caps_the_ceiling_raise(monkeypatch):
    rig = v3_rig(monkeypatch, store=_armed_store())
    rig.mining(True, limit=1950)
    rig.coord.data["wattage"] = 1940
    rig.set_supply(101.0)

    async def go():
        await rig.setup()
        for _ in range(10):
            await rig.tick()
            if cmds(rig, "set_power_limit"):
                break
        assert cmds(rig, "set_power_limit") == [2250]  # the proven cap, not Power Max
        assert rig.ctl._op_target <= 2250  # and it never asks the miner past the cap
        await rig.run(10)
        assert cmds(rig, "set_power_limit") == [2250]  # no raise-at-the-cap loop either

    run_async(go())


def test_an_armed_cap_lowers_a_higher_ceiling_once_tuned(monkeypatch):
    rig = v3_rig(
        monkeypatch,
        store=_armed_store(power_limit_w=5000, regulated_power_w=2400),
    )
    rig.coord.data["wattage"] = 2400
    rig.set_supply(104.0)

    async def go():
        await rig.setup()
        await rig.run(5)
        assert cmds(rig, "set_power_limit") == [2250]  # not just Power Max: the proven cap
        assert rig.ctl._op_target == 2250  # min(wanted 2400, cap)

    run_async(go())


def test_a_collapse_undo_arms_the_cap(monkeypatch):
    # The scenario of test_power_regulation's collapse test, watching the
    # proven ceiling: the undo write opens a clean run the witness may credit.
    rig = v3_rig(monkeypatch, store={"power_limit_w": 5000, "regulated_power_w": 2500})
    rig.mining(True, limit=5000)
    rig.coord.data.update({"wattage": 2500, "hashrate_5s": 100.0})
    rig.set_supply(104.0)

    async def go():
        await rig.setup()
        await rig.tick()
        assert cmds(rig, "set_miner_power") == [2500]
        await rig.run(2)
        await rig.ctl._command_power(1800)
        tune(rig, 0)
        rig.coord.data.update({"wattage": 1800, "hashrate_5s": 60.0})
        await rig.run(3)
        rig.coord.data.update({"hashrate_5s": 10.0})  # collapsed
        await rig.run(3)
        assert cmds(rig, "set_power_limit") == [2500]  # the undo went out
        assert rig.ctl._fault_power == 1800
        assert "hashrate collapse" in rig.ctl._fault_reason
        assert rig.ctl._hold_tainted is False  # the undo opened a clean run
        # Regulation is floored at 2500 by the collapse; the no-witness cap
        # (1550 W) inverts under the floor, and the floor stands.
        assert rig.ctl._proven_cap(rig.clock.t) == (2500, True)

    run_async(go())
