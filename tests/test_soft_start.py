"""Resume overshoot: soft start, predictive limiting and anti-windup (1.8.2).

Incident (2026-10-08): every demand-shutoff resume from ~88°F ran the M64 at
3.5-4.2 kW (integral wound up through the boot hold) and the supply peaked at
124-130°F; the zones satisfied mid-climb and the stagnant loop kept rising on
heat already sent. Replays use tests/loop_plant.py (fitted to that history).

1.8.1 baselines on these exact scenarios (peak °F / set_power_limit in 6 h):

    12 min lag, half flow   Kp 111 1500-5000 W: 126.8 / 11   Kp 60 1200-3500 W: 114.3 / 12
    fitted lag, satisfying  Kp 111 1500-5000 W: 126.2 /  2   Kp 60 1200-3500 W: 123.5 /  2
"""
from __future__ import annotations

import datetime as dt
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import loop_plant  # noqa: E402
from loop_plant import LoopPlant  # noqa: E402
from test_controller_smoke import MIN, T0, Rig, Store, const, controller_mod, run_async  # noqa: E402
from test_power_floor import CrashLoopMiner, cooled_at, crash_loop, limits  # noqa: E402

HOLD = controller_mod.RESUME_BOOT_HOLD_S
OLD = {"kp": dict(kp=111.1, pmin=1500, pmax=5000), "stopgap": dict(kp=60.0, pmin=1200, pmax=3500)}


def resume_rig(monkeypatch, kp, pmin, pmax):
    """Production-like settings, miner stopped by the demand shutoff an hour ago."""
    rig = Rig(
        monkeypatch,
        **{
            const.CONF_PID_KP: kp, const.CONF_PID_KI: 0.06, const.CONF_PID_KD: 55.556,
            const.CONF_POWER_MIN: pmin, const.CONF_POWER_MAX: pmax,
            const.CONF_PID_MIN_ADJUST_INTERVAL: 1800, const.CONF_PID_MIN_ADJUST_INTERVAL_INCREASE: 1200,
        },
    )
    rig.coord.update_interval = dt.timedelta(seconds=loop_plant.POLL_S)
    Store.saved["whatsminer.e1.controller"] = {
        "shutoff": {"state": "stopped", "owned": True, "since": T0 - 3600, "gate_armed": True,
                    "last_stop_at": T0 - 3600},
    }
    return rig


def half_flow(plant):
    return 0.5


def satisfy_after_target(plant, frac=0.5):
    """Zones calling at ``frac`` until 2 min after the supply first reaches 104°F, then all idle."""
    if plant.temp >= 104.0 and not hasattr(plant, "reached_at"):
        plant.reached_at = plant.t
    return 0.0 if plant.t >= getattr(plant, "reached_at", float("inf")) + 120 else frac


async def resume(rig, calling, lag, start=88.0, minutes=360, each_tick=None):
    rig.mining(False, limit=rig.ctl._power_min)
    plant = LoopPlant(rig, start, rig.ctl._power_min, False, calling, lag=lag)
    await rig.setup()
    rig.arm_gate(67.0)
    end = rig.clock.t + minutes * MIN
    while rig.clock.t < end:
        await plant.run(loop_plant.POLL_S / 60)
        if each_tick is not None:
            each_tick(plant)
    return plant


# ---------------------------------------------------------------- the replays


@pytest.mark.parametrize("tune,old_cmds", [("kp", 11), ("stopgap", 12)])
def test_resume_from_88f_with_12_min_lag_stays_under_the_cap(monkeypatch, tune, old_cmds):
    rig = resume_rig(monkeypatch, **OLD[tune])
    plant = run_async(resume(rig, half_flow, loop_plant.LAG_12MIN))
    assert plant.commands[0][1] == "power_on"
    assert plant.max_temp < 122.0, plant.max_temp
    assert len(plant.limits()) <= old_cmds, plant.commands
    assert not rig.pid_state.get("lockout_latched")


@pytest.mark.parametrize("tune", ["kp", "stopgap"])
def test_resume_with_zones_satisfying_mid_climb_backs_off_before_the_cap(monkeypatch, tune, caplog):
    rig = resume_rig(monkeypatch, **OLD[tune])
    plant = run_async(resume(rig, satisfy_after_target, loop_plant.LAG_FITTED, minutes=120))
    assert plant.max_temp < 122.0, plant.max_temp
    assert len(plant.limits()) <= 3, plant.commands
    assert "backing off to" in caplog.text


@pytest.mark.parametrize("tune", ["kp", "stopgap"])
def test_resume_holds_through_the_boot_hold_and_never_jumps_to_max(monkeypatch, tune):
    """No limit change in the boot hold; the first increase is at most half the span."""
    cfg = OLD[tune]
    rig = resume_rig(monkeypatch, **cfg)
    integrals: list[tuple[float, float | None]] = []

    def watch(plant):
        integrals.append((plant.t, rig.pid_state.get("integral")))

    plant = run_async(resume(rig, half_flow, loop_plant.LAG_12MIN, minutes=90, each_tick=watch))
    on_at = plant.commands[0][0]
    ups = [(t, w) for t, c, w in plant.commands if c == "set_power_limit"]
    assert ups, plant.commands
    assert ups[0][0] - on_at >= HOLD
    assert ups[0][1] <= cfg["pmin"] + (cfg["pmax"] - cfg["pmin"]) // 2 + 1
    assert max(w for _, w in ups) < cfg["pmax"]
    # Anti-windup: through the boot hold the integral tracks the limit in
    # force (P + I + D ≈ power_min), it does not ramp toward power_max.
    held = [i for t, i in integrals if on_at + 2 * MIN < t < on_at + HOLD and i is not None]
    assert held and max(held) - min(held) < 0.25 * (cfg["pmax"] - cfg["pmin"]), held


def test_soft_start_is_not_armed_by_our_own_limit_change_restart(monkeypatch):
    """After the hand-back, PID limit changes restart btminer without re-arming the soft start."""
    rig = resume_rig(monkeypatch, **OLD["stopgap"])
    states: list[tuple[float, bool]] = []
    # All zones open, then a third of the flow closes at 150 min: the PID
    # trims the limit and that restart must be treated as ours.
    plant = run_async(
        resume(rig, lambda p: 1.0 if p.t < T0 + 150 * MIN else 0.65, loop_plant.LAG_FITTED, minutes=300,
               each_tick=lambda p: states.append((p.t, rig.ctl._soft_start)))
    )
    armed = next(t for t, s in states if s)
    ended = next((t for t, s in states if not s and t > armed), None)
    assert ended is not None, "soft start never handed back to the PID"
    later = [t for t, c, _ in plant.commands if c == "set_power_limit" and t > ended]
    assert later, plant.commands  # the PID did restart the miner after the hand-back
    assert not any(s for t, s in states if t > ended), "a limit-change restart re-armed the soft start"
    assert abs(plant.temp - 104.0) < 3.0


# ---------------------------------------------------------------- unit checks


def test_supply_jump_restarts_the_slope_window(monkeypatch):
    rig = Rig(monkeypatch)
    ctl = rig.ctl
    for i in range(10):
        ctl._note_supply(T0 + 30 * i, 90.0 + 0.1 * i)
    assert ctl._supply_slope() == pytest.approx(0.2, abs=1e-6)
    ctl._note_supply(T0 + 300, 115.0)  # glitch / reconnect
    assert ctl._supply_slope() is None
    for i in range(1, 5):
        ctl._note_supply(T0 + 300 + 30 * i, 115.0)
    assert ctl._supply_slope() == pytest.approx(0.0, abs=1e-9)
    # Samples older than the window are dropped.
    ctl._note_supply(T0 + 2000, 115.0)
    assert len(ctl._supply_samples) == 1


def test_resume_onto_a_warm_loop_steps_once_and_does_not_overshoot(monkeypatch):
    """Resume near target: one bounded step after the boot hold, then the PID (1.8.1: 111.9°F)."""
    rig = resume_rig(monkeypatch, **OLD["kp"])
    plant = run_async(resume(rig, half_flow, loop_plant.LAG_FITTED, start=103.0, minutes=60))
    ups = plant.limits()
    assert ups and ups[0] <= 1500 + (5000 - 1500) // 2, plant.commands
    assert len(ups) <= 2, plant.commands
    assert plant.max_temp < 110.0, plant.max_temp


# ------------------------------------------------------------- 1200 W floor


def test_1200w_power_min_learns_one_step_once_cool(monkeypatch):
    """A 1200 W power_min the miner can't hold: the cap still commands 1200, learning raises 250 W once."""
    rig = Rig(monkeypatch, **{const.CONF_POWER_MIN: 1200})
    miner = CrashLoopMiner(rig, floor_w=1400)

    async def go():
        await rig.setup()
        rig.set_supply(104.0)
        await rig.tick()
        rig.set_supply(123.0)
        miner.poll()
        await rig.tick()
        assert limits(rig) == [1200], rig.calls
        rig.coord.api.calls.clear()
        await crash_loop(rig, miner, 120, supply=123.0, until=lambda: miner.limit >= 1400)
        assert limits(rig) == [1450], rig.calls
        assert miner.attempts[1][0] >= cooled_at(rig)  # hot restarts did not count
        await crash_loop(rig, miner, 30, supply=104.0)
        floor = rig.pid_state["power_floor"]
        assert floor["learned"] == 1450 and floor["unholdable_limit"] == 1200
        assert limits(rig) == [1450], rig.calls

    run_async(go())
