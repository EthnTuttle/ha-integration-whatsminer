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

HOLD = controller_mod.RESUME_BOOT_HOLD_S
HOT_WINDOW = controller_mod.SUPPLY_HOT_WINDOW_S


def v3_plant(plant, retune_s=None):
    """v2.0: set.miner.power moves the operating power restart-free.

    The plant's heat and the controller's belief follow the operating power;
    the reported v2 wattage limit stays the ceiling until adjust_power_limit
    moves it (every adjust_power_limit restarts btminer, which runs at the
    limit until a v3 write re-establishes the operating power).

    The write gate gives every dynamic write one tuned epoch: each
    set.miner.power re-opens frequency search (up-freq-finish drops), and
    tuning finishes ``retune_s`` later (default: one poll — the replays
    exercise the soft-start/predictive mechanics, not epoch cadence).
    """
    rig = plant.rig
    if retune_s is None:
        retune_s = loop_plant.POLL_S
    retune_until = 0.0
    plant.power = float(plant.limit)  # operating power; == ceiling while not regulated

    def retune_state():
        rig.coord.data["up_freq_finish"] = 1 if rig.clock.t >= retune_until else 0

    orig_advance = plant.advance

    def advance(dt=loop_plant.POLL_S, sub=5.0):
        orig_advance(dt, sub)
        retune_state()

    plant.advance = advance

    def draw(t):
        return plant.power if plant.hashing and t >= plant.power_from else 0.0

    plant.draw = draw
    orig_set_draw = plant._set_draw
    orig_set_limit = plant._set_limit
    orig_power_on = plant._power_on

    def _set_draw():
        plant.power_hist.append((rig.clock.t, draw(rig.clock.t)))

    async def set_power_limit(watts):
        await orig_set_limit(watts)
        plant.power = float(plant.limit)  # after the restart the miner runs at the limit

    async def power_on():
        await orig_power_on()
        plant.power = float(plant.limit)

    async def set_miner_power(watts):
        nonlocal retune_until
        plant.commands.append((rig.clock.t, "set_miner_power", int(watts)))
        rig.coord.api.calls.append(("set_miner_power", int(watts)))
        plant.power = float(int(watts))  # no restart
        retune_until = rig.clock.t + retune_s  # the write re-opened the tune
        retune_state()
        _set_draw()

    plant._set_draw = _set_draw
    rig.coord.api.set_power_limit = set_power_limit
    rig.coord.api.power_on = power_on
    rig.coord.api.set_miner_power = set_miner_power
    return plant


# ------------------------------------------------- crash-floor rig (from the
# deleted tests/test_power_floor.py; only what the 1200 W floor test needs)


class CrashLoopMiner:
    """Fake firmware for the Rig's coordinator.

    Any limit below ``floor_w`` boots, hashes for ``up_polls`` polls and
    restarts (Elapsed resets, Power Limit reads 0 for ``down_polls`` polls).
    ``set_power_limit`` restarts btminer; ``stale_polls`` makes the summary
    keep reporting the previous limit for that many polls afterwards.
    """

    def __init__(
        self, rig: Rig, floor_w: int, limit=3293, uptime=86400, down_polls=2, up_polls=4, stale_polls=0
    ):
        self.rig, self.floor_w = rig, floor_w
        self.limit, self.reported_limit = limit, limit
        self.uptime = uptime
        self.down_polls, self.up_polls, self.stale_polls = down_polls, up_polls, stale_polls
        self.phase = down_polls  # hashing
        self.stale_left = 0
        self.attempts: list[tuple[float, int]] = []
        self.crashes = 0
        self.crash_times: list[float] = []
        rig.coord.api.set_power_limit = self._set_limit
        rig.mining(True, limit=limit)
        rig.coord.data["uptime"] = uptime
        rig.coord.data["wattage"] = limit

    async def _set_limit(self, watts):
        self.attempts.append((self.rig.clock.t, int(watts)))
        if "set_power_limit" in self.rig.coord.api.fail:
            raise RuntimeError("boom")
        self.rig.coord.api.calls.append(("set_power_limit", int(watts)))
        self.limit = int(watts)
        self.stale_left = self.stale_polls
        self.uptime = 0
        self.phase = 0  # adjust_power_limit restarts btminer

    def restart(self):
        """A spontaneous restart (not commanded)."""
        self.crashes += 1
        self.crash_times.append(self.rig.clock.t)
        self.uptime = 0
        self.phase = 0

    def poll(self):
        """Advance one poll and write what the summary would say."""
        if self.phase < self.down_polls:
            self.rig.mining(False, limit=0)
            self.rig.coord.data["uptime"] = 0
            self.rig.coord.data["wattage"] = 0
            self.phase += 1
            return
        self.uptime += 30
        if self.stale_left > 0:
            self.stale_left -= 1
        else:
            self.reported_limit = self.limit
        self.rig.mining(True, limit=self.reported_limit)
        self.rig.coord.data["uptime"] = self.uptime
        self.rig.coord.data["wattage"] = self.limit
        self.phase += 1
        if self.limit < self.floor_w and self.phase >= self.down_polls + self.up_polls:
            self.restart()

    @property
    def hashing(self):
        return bool(self.rig.coord.data["is_mining"])


def limits(rig):
    return [w for c, w in rig.calls if c == "set_power_limit"]


def cooled_at(rig):
    """When restarts started counting again: the supply hot window's end."""
    return rig.ctl._supply_hot_at + HOT_WINDOW


async def crash_loop(rig, miner, minutes, supply=None, decay=0.25, floor_hit=77.0, until=None):
    """Drive polls; supply decays while the miner crash-loops and recovers while it holds."""
    t_end = rig.clock.t + minutes * MIN
    while rig.clock.t < t_end:
        if supply is not None:
            if miner.limit < miner.floor_w:
                supply = max(floor_hit, supply - decay)
            else:
                supply = min(104.0, supply + 0.1)
            rig.set_supply(supply)
        miner.poll()
        await rig.tick()
        if until is not None and until():
            break
    return supply
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
    plant = v3_plant(LoopPlant(rig, start, rig.ctl._power_min, False, calling, lag=lag))
    await rig.setup()
    rig.coord.data["up_freq_finish"] = 1  # tuned: v3 carries in-limit power moves
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


def test_soft_start_is_not_armed_by_our_own_limit_change_restart(monkeypatch, caplog):
    """After the hand-back, an adjust_power_limit restart must not re-arm the soft start.

    v2.0: in-limit power moves are restart-free (v3), so the restart under test
    is provoked by a target bump: once the PID asks for more than the ceiling
    in force, the ceiling raise goes out as adjust_power_limit. That restart is
    ours: the start edge it causes must not arm the soft start again.
    """
    import logging

    rig = resume_rig(monkeypatch, **OLD["stopgap"])
    states: list[tuple[float, bool]] = []
    phase = {"armed_seen": False, "ended": None, "spiked": False, "restored": False}

    def watch(p):
        states.append((p.t, rig.ctl._soft_start))
        if rig.ctl._soft_start:
            phase["armed_seen"] = True
        elif phase["armed_seen"] and phase["ended"] is None:
            phase["ended"] = p.t
        if phase["ended"] is not None and not phase["spiked"] and p.t >= phase["ended"] + 10 * MIN:
            rig.pid_state["target"] = 112.0  # ask above the ceiling in force
            phase["spiked"] = True
        if phase["spiked"] and not phase["restored"] and p.t >= phase["ended"] + 30 * MIN:
            rig.pid_state["target"] = 104.0
            phase["restored"] = True

    with caplog.at_level(logging.INFO, logger="wm.controller"):
        plant = run_async(resume(rig, half_flow, loop_plant.LAG_FITTED, minutes=300, each_tick=watch))
    ended = phase["ended"]
    assert ended is not None, "soft start never handed back to the PID"
    assert phase["spiked"], plant.commands
    later = [(t, w) for t, c, w in plant.commands if c == "set_power_limit" and t > ended]
    assert later, plant.commands  # the ceiling raise did restart the miner after the hand-back
    # Only the initial demand-shutoff resume arms the soft start; the restarts
    # our limit changes cause afterwards never do (predictive back-off arms are
    # a separate, legitimate trigger).
    arms = [r for r in caplog.records if "Soft start armed (mining resumed)" in r.getMessage()]
    assert len(arms) == 1, [r.getMessage() for r in arms]
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
    """Resume near target: one bounded step after the boot hold, then the PID (1.8.1: 111.9°F).

    v2.0: raising the ceiling above the power in force sends the bound ceiling
    (one restart); the OPERATING power the miner is then regulated to must stay
    within one soft-start step of the resume power.
    """
    rig = resume_rig(monkeypatch, **OLD["kp"])
    plant = run_async(resume(rig, half_flow, loop_plant.LAG_FITTED, start=103.0, minutes=60))
    regulated = [w for _, c, w in plant.commands if c == "set_miner_power"]  # what the miner runs at
    assert regulated and regulated[0] <= 1500 + (5000 - 1500) // 2, plant.commands
    assert len(plant.limits()) <= 2, plant.commands
    assert plant.max_temp < 110.0, plant.max_temp


# ------------------------------------------------------------- 1200 W floor


def test_1200w_power_min_raises_the_crash_floor_once_cool(monkeypatch):
    """A 1200 W power_min the miner can't hold: the cap still commands 1200 W, and
    the first counted crash (once the loop cooled) lifts the crash floor by
    CRASH_STEP_W, so the next command is clamped above the hardware floor."""
    rig = Rig(monkeypatch, **{const.CONF_POWER_MIN: 1200})
    miner = CrashLoopMiner(rig, floor_w=1400)

    async def go():
        await rig.setup()
        rig.set_supply(104.0)
        await rig.tick()
        rig.set_supply(123.0)
        miner.poll()
        await rig.tick()
        assert limits(rig) == [1200], rig.calls  # the cap forces Power Min, not the floor
        rig.coord.api.calls.clear()
        # Tuning finishes: the recovery raise above the armed crash floor is a
        # gated non-safety write, so it needs the tuned epoch open.
        rig.coord.data["up_freq_finish"] = 1
        await crash_loop(rig, miner, 120, supply=123.0, until=lambda: miner.limit >= 1400)
        faults = rig.pid_state["faults"]
        assert faults["crash_floor_w"] == 1200 + controller_mod.CRASH_STEP_W, faults
        assert faults["floor_w"] == faults["crash_floor_w"], faults
        ups = limits(rig)
        assert ups and ups[0] >= faults["floor_w"], rig.calls  # never below the crash floor
        assert miner.attempts[1][0] >= cooled_at(rig)  # hot restarts did not count
        assert miner.limit >= miner.floor_w
        await crash_loop(rig, miner, 30, supply=104.0)
        assert limits(rig) == ups, rig.calls  # holding at target: no further commands
        faults = rig.pid_state["faults"]
        assert faults["crash_floor_w"] == 1200 + controller_mod.CRASH_STEP_W, faults
        assert faults["floor_w"] == faults["crash_floor_w"], faults

    run_async(go())
