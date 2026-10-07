"""The learned power floor: a miner that cannot hold a limit must not pin the controller.

Incident (2026-10-06): the supply cap forced the M64 to power_min = 1000 W, a
limit its hashboards cannot hold. The firmware restarted btminer every ~3 min
and each restart re-armed the 600 s boot hold, so the controller never raised
the limit again; supply fell 137 → 77°F with every zone calling.
"""
from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_controller_smoke import MIN, Rig, Store, const, controller_mod, pn, run_async  # noqa: E402

HOLD = controller_mod.RESUME_BOOT_HOLD_S


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
            self.phase += 1
            return
        self.uptime += 30
        if self.stale_left > 0:
            self.stale_left -= 1
        else:
            self.reported_limit = self.limit
        self.rig.mining(True, limit=self.reported_limit)
        self.rig.coord.data["uptime"] = self.uptime
        self.phase += 1
        if self.limit < self.floor_w and self.phase >= self.down_polls + self.up_polls:
            self.restart()

    @property
    def hashing(self):
        return bool(self.rig.coord.data["is_mining"])


def limits(rig):
    return [w for c, w in rig.calls if c == "set_power_limit"]


def created(key):
    return [k for k, _, _ in pn.created].count(f"whatsminer_{key}")


def notification(key):
    return [m for k, _, m in pn.created if k == f"whatsminer_{key}"]


def spy_sends(rig):
    """Record every _set_power_limit call as (t, watts, floor_fire)."""
    sends: list[tuple[float, int, bool]] = []
    orig = rig.ctl._set_power_limit

    async def wrapped(watts, floor_fire=False):
        sends.append((rig.clock.t, int(watts), bool(floor_fire)))
        return await orig(watts, floor_fire)

    rig.ctl._set_power_limit = wrapped
    return sends


async def start_at_cap(rig, miner, supply_cap=123.0):
    """Setup, settle, then trip the supply cap so power_min is commanded."""
    await rig.setup()
    rig.set_supply(104.0)  # at target: first tick does not command
    await rig.tick()
    assert rig.calls == []
    rig.set_supply(supply_cap)
    miner.poll()
    await rig.tick()
    assert limits(rig) == [1000], rig.calls
    rig.coord.api.calls.clear()


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


async def polls(rig, miner, n):
    for _ in range(n):
        miner.poll()
        await rig.tick()


# ---------------------------------------------------------------- the incident


def test_crash_loop_at_power_min_steps_up_within_bounded_time(monkeypatch):
    """Reproduces the incident; fails on the pre-fix controller (no command for 2 h)."""
    rig = Rig(monkeypatch)
    miner = CrashLoopMiner(rig, floor_w=1500)

    async def go():
        await start_at_cap(rig, miner)
        t_cap = rig.clock.t
        first_crash_at = None
        supply = 123.0
        t_end = rig.clock.t + 120 * MIN
        while rig.clock.t < t_end:
            supply = max(77.0, supply - 0.25) if miner.limit < miner.floor_w else min(104.0, supply + 0.1)
            rig.set_supply(supply)
            miner.poll()
            if first_crash_at is None and miner.crashes:
                first_crash_at = rig.clock.t
            await rig.tick()
            if miner.limit >= miner.floor_w and rig.clock.t - t_cap > 25 * MIN:
                break
        assert limits(rig), "controller never raised the limit out of the crash loop"
        assert limits(rig) == [1250, 1500], rig.calls
        assert miner.limit >= miner.floor_w
        # Bounded recovery: first step-up within 10 min of the first crash.
        first_up_at = miner.attempts[0][0]
        assert first_crash_at is not None and first_up_at - first_crash_at <= 10 * MIN
        floor = rig.pid_state["power_floor"]
        assert floor["effective"] == 1500 and floor["learned"] == 1500 and floor["unholdable_limit"] == 1250
        assert Store.saved["whatsminer.e1.controller"]["floor_learned"] == 1500
        assert created("floor_raised") == 2
        assert rig.pid_state["out_min_effective"] == 1500
        # 15 more minutes stable (supply back at target, so the PID is quiet)
        # proves the limit holdable and ends the episode.
        await crash_loop(rig, miner, 16)
        floor = rig.pid_state["power_floor"]
        assert floor["proven_ok"] == 1500 and floor["short_runs"] == 0
        assert limits(rig) == [1250, 1500]

    run_async(go())


def test_stair_up_cost_to_2000w(monkeypatch):
    """Cost model: one commanded restart per 250 W learned, 2 + 1/step crashes."""
    rig = Rig(monkeypatch)
    miner = CrashLoopMiner(rig, floor_w=2000)

    async def go():
        await start_at_cap(rig, miner)
        t0 = rig.clock.t
        await crash_loop(rig, miner, 60, supply=123.0, until=lambda: miner.limit >= miner.floor_w)
        minutes = (rig.clock.t - t0) / MIN
        print(f"\nstair-up to 2000 W: commands={limits(rig)} crashes={miner.crashes} minutes={minutes:.1f}")
        assert limits(rig) == [1250, 1500, 1750, 2000]
        assert miner.crashes == 5
        assert minutes < 30

    run_async(go())


def test_crash_loop_at_pid_chosen_limit_above_power_min_is_learned(monkeypatch):
    """A loop at a limit the PID chose (1300 W, hardware floor 1500 W) is floor evidence too."""
    rig = Rig(monkeypatch)
    miner = CrashLoopMiner(rig, floor_w=1500, limit=1300)

    async def go():
        await rig.setup()
        rig.set_supply(104.0)
        await rig.tick()
        assert rig.calls == []
        miner.restart()  # ends an 86400 s run: not a short run
        await crash_loop(rig, miner, 30, until=lambda: miner.limit >= miner.floor_w)
        assert limits(rig) == [1550], rig.calls
        assert miner.crashes == 3  # the long-run crash plus the two short runs that teach
        floor = rig.pid_state["power_floor"]
        assert floor["learned"] == 1550 and floor["unholdable_limit"] == 1300
        assert created("floor_raised") == 1 and created("restart_loop") == 0

    run_async(go())


def test_pid_lifts_off_the_floor_rail_with_flat_cold_supply(monkeypatch):
    """After the floor lands, a cold loop must get more heat even if the supply reading never dips."""
    rig = Rig(monkeypatch)
    Store.saved["whatsminer.e1.controller"] = {"floor_learned": 1500}
    rig.mining(True, limit=1500)
    rig.coord.data["uptime"] = 60  # just booted at the floor: 540 s of hold left

    async def go():
        await rig.setup()
        rig.set_supply(80.0)  # 24°F below target, every zone calling
        await rig.tick()
        hold_until = rig.ctl._resume_hold_until
        assert hold_until > rig.clock.t
        while rig.clock.t + 30 < hold_until:
            rig.coord.data["uptime"] += 30
            await rig.tick()
            assert rig.calls == []  # the hold is respected for the PID's own request
        for _ in range(2):
            rig.coord.data["uptime"] += 30
            await rig.tick()
        assert limits(rig) and limits(rig)[0] > 1500, rig.calls

    run_async(go())


def test_pid_lifts_off_the_floor_rail_with_slowly_rising_supply(monkeypatch):
    rig = Rig(monkeypatch)
    Store.saved["whatsminer.e1.controller"] = {"floor_learned": 1500}
    rig.mining(True, limit=1500)
    rig.coord.data["uptime"] = 1200  # past the hold

    async def go():
        await rig.setup()
        supply = 78.0
        for _ in range(4):
            supply += 0.05
            rig.set_supply(supply)
            rig.coord.data["uptime"] += 30
            await rig.tick()
        assert limits(rig) and limits(rig)[0] > 1500, rig.calls

    run_async(go())


# ------------------------------------------------------------- persistence


def test_learned_floor_persists_and_clamps_cap_lockout_dwell(monkeypatch):
    rig = Rig(monkeypatch)
    Store.saved["whatsminer.e1.controller"] = {"floor_learned": 1500, "floor_learn_limit": 1250}

    async def go():
        await rig.setup()
        assert rig.pid_state["power_floor"]["effective"] == 1500
        rig.set_supply(123.0)
        await rig.tick()
        assert limits(rig) == [1500], rig.calls
        assert rig.pid_state["out_min_effective"] == 1500
        rig.coord.api.calls.clear()
        rig.mining(True, limit=1500)
        rig.set_supply(100.0)
        rig.arm_gate(60.0)
        rig.set_thermostats("idle")
        await rig.tick()
        assert rig.pid_state["control_mode"] in ("demand_lockout", "dwell")
        assert rig.pid_state["output"] == 1500
        assert 1000 not in limits(rig)
        # Probe lost: the fallback curve's low end is the floor too.
        rig.set_supply(None)
        rig.set_weather(70.0, [70.0] * 24)
        rig.set_thermostats("heating")
        await rig.tick()
        assert rig.pid_state["control_mode"] == "fallback"
        assert rig.pid_state["requested_output"] == 1500
        assert all(w >= 1500 for w in limits(rig)), rig.calls

    run_async(go())


def test_restart_mid_crash_loop_restores_floor_and_fires_on_first_tick(monkeypatch):
    rig = Rig(monkeypatch)
    Store.saved["whatsminer.e1.controller"] = {"floor_learned": 1500}
    rig.mining(True, limit=1000)
    rig.coord.data["uptime"] = 60  # inside the uptime-derived boot hold

    async def go():
        await rig.setup()
        rig.set_supply(77.0)  # cold loop: the PID itself wants far more
        await rig.tick()
        assert rig.clock.t < rig.ctl._resume_hold_until
        assert limits(rig) == [1500], rig.calls  # exactly the floor through the hold

    run_async(go())


def test_floor_fire_in_fallback_commands_exactly_the_floor(monkeypatch):
    rig = Rig(monkeypatch)
    Store.saved["whatsminer.e1.controller"] = {"floor_learned": 1500}
    rig.mining(True, limit=1000)
    rig.coord.data["uptime"] = 60

    async def go():
        await rig.setup()
        rig.set_supply(None)
        rig.set_weather(30.0, [30.0] * 24)  # curve wants ~3600 W
        await rig.tick()
        assert rig.pid_state["control_mode"] == "fallback"
        assert rig.clock.t < rig.ctl._resume_hold_until
        assert limits(rig) == [1500], rig.calls

    run_async(go())


def test_ha_restart_during_down_phase_learns_after_two_cycles(monkeypatch):
    rig = Rig(monkeypatch)
    miner = CrashLoopMiner(rig, floor_w=1500, limit=1000, uptime=0)
    miner.phase = 0  # first poll sees the miner down

    async def go():
        await rig.setup()
        rig.set_supply(100.0)
        miner.poll()
        await rig.tick()
        assert not rig.coord.data["is_mining"]
        await crash_loop(rig, miner, 20, until=lambda: limits(rig))
        assert limits(rig) == [1250]
        assert rig.pid_state["power_floor"]["learned"] == 1250

    run_async(go())


def test_setup_drops_learned_floor_when_power_min_raised_above_it(monkeypatch):
    rig = Rig(monkeypatch, **{const.CONF_POWER_MIN: 2000})
    Store.saved["whatsminer.e1.controller"] = {"floor_learned": 1500, "floor_learn_limit": 1250}

    async def go():
        await rig.setup()
        floor = rig.pid_state["power_floor"]
        assert floor["learned"] is None and floor["effective"] == 2000

    run_async(go())


def test_reset_learned_floor_clears_floor(monkeypatch):
    rig = Rig(monkeypatch)
    Store.saved["whatsminer.e1.controller"] = {"floor_learned": 1500, "floor_learn_limit": 1250}

    async def go():
        await rig.setup()
        rig.set_supply(104.0)
        await rig.tick()
        rig.coord.api.calls.clear()
        await rig.ctl.async_reset_learned_floor()
        assert rig.pid_state["power_floor"]["effective"] == 1000
        assert Store.saved["whatsminer.e1.controller"]["floor_learned"] is None
        assert "whatsminer_floor_raised" in pn.dismissed
        rig.set_supply(123.0)
        await rig.tick()
        assert limits(rig) == [1000]

    run_async(go())


# ------------------------------------------------------ boots that never hash


def test_floor_enforced_on_boot_that_never_hashes(monkeypatch):
    """btminer up (Elapsed > 0) at a known limit below the floor, hashrate still 0: act anyway."""
    rig = Rig(monkeypatch)
    Store.saved["whatsminer.e1.controller"] = {"floor_learned": 1500}
    rig.mining(False, limit=1000)
    rig.coord.data["uptime"] = 0

    async def go():
        await rig.setup()
        rig.set_supply(100.0)
        await rig.tick()
        assert rig.calls == []  # Elapsed 0: btminer is down, nothing to talk to
        rig.coord.data["uptime"] = 30
        await rig.tick()
        assert limits(rig) == [1500], rig.calls
        assert rig.pid_state["control_mode"] == "idle"
        rig.coord.data["uptime"] = 60
        rig.mining(False, limit=1500)
        await rig.tick()
        assert limits(rig) == [1500]  # not repeated once the believed limit is the floor

    run_async(go())


def test_boots_that_never_hash_still_teach_the_floor(monkeypatch):
    rig = Rig(monkeypatch)
    rig.mining(False, limit=1000)
    rig.coord.data["uptime"] = 0

    async def go():
        await rig.setup()
        rig.set_supply(100.0)
        await rig.tick()
        for _ in range(3):
            for up in (30, 60, 90, 120, 150):
                rig.coord.data["uptime"] = up
                rig.mining(False, limit=1000)
                await rig.tick()
            rig.coord.data["uptime"] = 0
            await rig.tick()
            if limits(rig):
                break
        assert rig.pid_state["power_floor"]["learned"] == 1250
        assert limits(rig) == [1250], rig.calls

    run_async(go())


# --------------------------------------------------------- negative guards


def test_commanded_restart_is_not_counted(monkeypatch):
    """Our own adjust_power_limit restarts the miner; that must never look like a crash."""
    rig = Rig(monkeypatch)
    miner = CrashLoopMiner(rig, floor_w=1000, limit=3000)

    async def go():
        await rig.setup()
        rig.set_supply(104.0)
        await rig.tick()
        for supply in (85.0, 120.0, 85.0, 120.0):
            rig.set_supply(supply)
            await crash_loop(rig, miner, 11)  # past the boot hold each time
        assert len(limits(rig)) >= 3, rig.calls
        assert miner.crashes == 0
        floor = rig.pid_state["power_floor"]
        assert floor["learned"] is None and floor["short_runs"] == 0
        assert created("floor_raised") == 0 and created("restart_loop") == 0

    run_async(go())


def test_single_spontaneous_restart_keeps_boot_hold(monkeypatch):
    rig = Rig(monkeypatch)
    miner = CrashLoopMiner(rig, floor_w=1000, limit=3000)

    async def go():
        await rig.setup()
        rig.set_supply(104.0)
        await rig.tick()
        assert rig.clock.t >= rig.ctl._resume_hold_until  # no hold: uptime 86400
        miner.restart()
        await crash_loop(rig, miner, 1.5)  # down, down, up
        assert miner.hashing
        assert rig.ctl._resume_hold_until >= rig.clock.t + 9 * MIN
        floor = rig.pid_state["power_floor"]
        assert floor["short_runs"] == 0  # a 86400 s run is not a short run
        assert floor["learned"] is None
        assert rig.calls == []

    run_async(go())


def test_pool_outage_without_uptime_reset_is_not_counted(monkeypatch):
    rig = Rig(monkeypatch)
    rig.mining(True, limit=1000)
    rig.coord.data["uptime"] = 300

    async def go():
        await rig.setup()
        rig.set_supply(104.0)
        await rig.tick()
        for _ in range(3):
            # Hashrate drops to 0 (pool unreachable) while Elapsed keeps climbing.
            for _ in range(3):
                rig.coord.data["uptime"] += 30
                rig.mining(False, limit=1000)
                await rig.tick()
            for _ in range(3):
                rig.coord.data["uptime"] += 30
                rig.mining(True, limit=1000)
                await rig.tick()
        floor = rig.pid_state["power_floor"]
        assert floor["short_runs"] == 0 and floor["learned"] is None
        assert rig.calls == []

    run_async(go())


def test_one_poll_uptime_glitch_is_not_a_restart(monkeypatch):
    """A garbled summary parses to Elapsed 0; two of them must not raise the floor."""
    rig = Rig(monkeypatch)
    rig.mining(True, limit=1000)
    rig.coord.data["uptime"] = 300

    async def go():
        await rig.setup()
        rig.set_supply(104.0)
        await rig.tick()
        for up in (330, 0, 390, 420, 0, 480, 510):
            rig.coord.data["uptime"] = up
            rig.mining(up > 0, limit=1000 if up > 0 else 0)
            await rig.tick()
        floor = rig.pid_state["power_floor"]
        assert floor["short_runs"] == 0 and floor["learned"] is None
        assert rig.calls == []

    run_async(go())


def test_uptime_regression_without_mining_edge_counts(monkeypatch):
    """A 30 s poll can miss the down phase; Elapsed going backwards (and staying back) is a restart."""
    rig = Rig(monkeypatch)
    rig.mining(True, limit=1000)
    rig.coord.data["uptime"] = 200

    async def go():
        await rig.setup()
        rig.set_supply(104.0)
        await rig.tick()
        assert rig.calls == []
        for up in (230, 30, 60, 90, 30, 60):
            rig.coord.data["uptime"] = up
            await rig.tick()
        assert rig.pid_state["power_floor"]["learned"] == 1250
        assert limits(rig) == [1250], rig.calls

    run_async(go())


def test_short_runs_at_another_limit_do_not_seed_learning_at_the_clamp(monkeypatch):
    """A fault that restarts the miner at 3000 W is not evidence about a 1000 W clamp."""
    rig = Rig(monkeypatch)
    miner = CrashLoopMiner(rig, floor_w=1500, limit=3000)

    async def go():
        await rig.setup()
        rig.set_supply(104.0)
        await rig.tick()
        miner.restart()  # ends the long run
        await polls(rig, miner, 6)
        miner.restart()  # ends a 2 min run at 3000 W: short run #1 at 3000
        await polls(rig, miner, 3)
        assert rig.pid_state["power_floor"]["short_runs"] == 1
        assert created("restart_loop") == 0  # one short run is not "repeatedly"
        rig.set_supply(123.0)
        await polls(rig, miner, 1)
        assert limits(rig) == [1000]  # the cap clamps (our restart)
        rig.set_supply(104.0)
        await crash_loop(rig, miner, 20, until=lambda: miner.crashes == 3)
        await polls(rig, miner, 3)  # regression, confirmation, hashing tick
        floor = rig.pid_state["power_floor"]
        assert floor["short_runs"] == 1 and floor["learned"] is None, floor
        assert limits(rig) == [1000]
        await crash_loop(rig, miner, 20, until=lambda: miner.crashes == 4)
        await polls(rig, miner, 3)
        assert rig.pid_state["power_floor"]["learned"] == 1250
        assert limits(rig) == [1000, 1250], rig.calls

    run_async(go())


def test_restart_loop_above_ceiling_notifies_after_two_short_runs_and_does_not_learn(monkeypatch):
    rig = Rig(monkeypatch)
    miner = CrashLoopMiner(rig, floor_w=9999, limit=4000)

    async def go():
        await rig.setup()
        rig.set_supply(104.0)
        await rig.tick()
        assert rig.calls == []
        miner.restart()
        await crash_loop(rig, miner, 30, until=lambda: miner.crashes == 2)
        await polls(rig, miner, 3)
        assert rig.pid_state["power_floor"]["short_runs"] == 1
        assert created("restart_loop") == 0
        await crash_loop(rig, miner, 30)
        assert miner.crashes >= 5
        floor = rig.pid_state["power_floor"]
        assert floor["learned"] is None and floor["effective"] == 1000
        assert created("restart_loop") == 1 and created("floor_raised") == 0
        assert "restarted 2 times" in notification("restart_loop")[0]
        assert limits(rig) == [], rig.calls  # supply at target: the PID has nothing to say

    run_async(go())


def test_restart_loop_above_ceiling_with_cold_loop_lets_pid_command_once_per_hold(monkeypatch):
    """The hold is no longer pinned, so the PID acts on a cold loop during an unrelated loop,
    but each command is ours and re-arms the hold: at most one restart per hold."""
    rig = Rig(monkeypatch)
    miner = CrashLoopMiner(rig, floor_w=9999, limit=3000)  # above the 2500 W ceiling

    async def go():
        await rig.setup()
        rig.set_supply(104.0)
        await rig.tick()
        assert rig.calls == []
        miner.restart()
        await crash_loop(rig, miner, 45, supply=104.0, decay=0.05)  # the loop cools slowly
        assert miner.crashes >= 8
        assert rig.pid_state["power_floor"]["learned"] is None
        times = [t for t, _ in miner.attempts]
        assert len(times) >= 2, miner.attempts
        assert all(b - a >= HOLD for a, b in zip(times, times[1:])), times
        assert all(w > 3000 for w in limits(rig)), rig.calls

    run_async(go())


def test_no_learning_while_chip_cap_active(monkeypatch):
    rig = Rig(monkeypatch)
    miner = CrashLoopMiner(rig, floor_w=1500)
    rig.coord.data["temperature_avg"] = 190.0  # ≥ 185°F chip cap

    async def go():
        await rig.setup()
        rig.set_supply(100.0)
        await rig.tick()
        assert limits(rig) == [1000]
        rig.coord.api.calls.clear()
        await crash_loop(rig, miner, 30)
        assert miner.crashes >= 5
        floor = rig.pid_state["power_floor"]
        assert floor["learned"] is None and floor["effective"] == 1000
        assert created("floor_raised") == 0
        assert created("restart_loop_chip_cap") == 1  # the loop persists; say so once
        assert limits(rig) == [], rig.calls

    run_async(go())


def test_chip_cap_clamps_to_power_min_not_learned_floor(monkeypatch):
    rig = Rig(monkeypatch)
    Store.saved["whatsminer.e1.controller"] = {"floor_learned": 1500}
    rig.coord.data["temperature_avg"] = 190.0

    async def go():
        await rig.setup()
        rig.set_supply(100.0)
        await rig.tick()
        assert limits(rig) == [1000], rig.calls
        rig.mining(True, limit=1000)
        rig.coord.data["uptime"] = 30
        await rig.tick()
        await rig.tick()
        assert limits(rig) == [1000]  # floor enforcement is inert under the chip cap

    run_async(go())


def test_freeze_over_lockout_holds_power_min_not_learned_floor(monkeypatch):
    rig = Rig(monkeypatch, **{const.CONF_FREEZE_GUARD_SENSOR: "sensor.loop"})
    Store.saved["whatsminer.e1.controller"] = {"floor_learned": 1500}

    async def go():
        await rig.setup()
        rig.set_freeze_sensor(35.0)
        rig.set_supply(145.0)
        await rig.tick()
        assert ("power_off", None) not in rig.calls
        assert limits(rig) == [1000], rig.calls
        assert rig.pid_state["control_mode"] == "safety_cap"
        assert "running at 1000 W" in notification("freeze_over_lockout")[0]
        rig.mining(True, limit=1000)
        rig.coord.data["uptime"] = 30
        await rig.tick()
        await rig.tick()
        assert limits(rig) == [1000]  # no floor enforcement against the hard hold

    run_async(go())


def test_floor_ceiling_stops_raising_and_notifies(monkeypatch):
    rig = Rig(monkeypatch)
    miner = CrashLoopMiner(rig, floor_w=9999)

    async def go():
        await start_at_cap(rig, miner)
        await crash_loop(rig, miner, 60, supply=123.0)
        # ceiling = min(power_min + 1500, power_max - coarse step) = 2500
        assert limits(rig)[:6] == [1250, 1500, 1750, 2000, 2250, 2500], rig.calls
        floor = rig.pid_state["power_floor"]
        assert floor["effective"] == 2500 and floor["exhausted"] is True
        assert created("floor_ceiling") == 1
        # Past the ceiling the floor is inert. The ordinary PID still acts on
        # the cold loop once the (no longer pinned) hold expires; each of its
        # commands is ours and re-arms the hold, so they come ≥ 600 s apart.
        later = miner.attempts[7:]  # [0] is the cap's 1000 W, [1:7] the six floor steps
        assert later, miner.attempts
        assert all(w > 2500 for _, w in later), later
        assert all(b - a >= HOLD for (a, _), (b, _) in zip(later, later[1:])), later
        assert ("power_off", None) not in rig.calls  # we never stop it ourselves

    run_async(go())


@pytest.mark.parametrize("up_polls", [4, 18])  # 180 s cycle, and a 600 s cycle that divides the hold
def test_boot_hold_is_not_pinned_by_unattributed_restarts(monkeypatch, up_polls):
    """Even with learning off, a crash loop may not extend the hold beyond one hold after our start."""
    monkeypatch.setattr(controller_mod, "FLOOR_RAISE_STEP", 0)
    rig = Rig(monkeypatch)
    miner = CrashLoopMiner(rig, floor_w=1500, up_polls=up_polls)

    async def go():
        await start_at_cap(rig, miner)
        t_cap = rig.clock.t
        expired_at = None
        t_end = rig.clock.t + 60 * MIN
        while rig.clock.t < t_end:
            miner.poll()
            await rig.tick()
            if miner.hashing and rig.clock.t >= rig.ctl._resume_hold_until:
                expired_at = rig.clock.t
                break
        assert expired_at is not None, "boot hold pinned for 60 min"
        # Our command's own start arms one hold; the crash-loop starts that
        # follow do not, so it expires within HOLD + boot latency + one cycle.
        assert expired_at - t_cap <= HOLD + 3 * MIN, expired_at - t_cap
        assert rig.pid_state["power_floor"]["learned"] is None

    run_async(go())


def test_proven_ok_is_not_credited_to_a_limit_just_sent(monkeypatch):
    rig = Rig(monkeypatch)
    rig.mining(True, limit=3000)
    rig.coord.data["uptime"] = 840

    async def go():
        await rig.setup()
        rig.set_supply(104.0)
        await rig.tick()
        assert rig.calls == []
        rig.set_supply(85.0)
        rig.coord.data["uptime"] = 870
        await rig.tick()
        sent = limits(rig)
        assert sent and sent[0] > 3000, rig.calls
        rig.coord.data["uptime"] = 900  # the summary has not shown the restart yet
        await rig.tick()
        assert rig.pid_state["power_floor"]["proven_ok"] is None
        # ...and once the restart is over and the new limit really holds 15 min, it is.
        for up in (0, 0, 30, 920):
            rig.coord.data["uptime"] = up
            rig.mining(up > 0, limit=sent[0] if up > 0 else 0)
            await rig.tick()
        rig.clock.t += HOLD
        rig.coord.data["uptime"] = 950
        await rig.tick()
        assert rig.pid_state["power_floor"]["proven_ok"] == sent[0]

    run_async(go())


# ------------------------------------------------------------ command path


def test_floor_command_send_failure_persists_floor_and_retries(monkeypatch):
    rig = Rig(monkeypatch)
    miner = CrashLoopMiner(rig, floor_w=1500)

    async def go():
        await start_at_cap(rig, miner)
        sends = spy_sends(rig)
        rig.coord.api.fail.add("set_power_limit")
        await crash_loop(rig, miner, 12, supply=123.0)
        assert rig.pid_state["power_floor"]["learned"] == 1250
        assert Store.saved["whatsminer.e1.controller"]["floor_learned"] == 1250
        assert created("floor_raised") == 1  # no double raise while the send fails
        fires = [t for t, w, ff in sends if ff and w == 1250]
        assert len(fires) >= 2
        assert all(b - a >= controller_mod.FLOOR_FIRE_RETRY_S for a, b in zip(fires, fires[1:])), fires
        assert all(w == 1250 for _, w in miner.attempts[1:]), miner.attempts
        assert limits(rig) == []
        rig.coord.api.fail.clear()
        await crash_loop(rig, miner, 20, supply=110.0, until=lambda: miner.limit >= miner.floor_w)
        assert limits(rig) == [1250, 1500]

    run_async(go())


def test_stale_power_limit_after_floor_command_does_not_double_command(monkeypatch):
    rig = Rig(monkeypatch)
    miner = CrashLoopMiner(rig, floor_w=1500, stale_polls=2)

    async def go():
        await start_at_cap(rig, miner)
        await crash_loop(rig, miner, 40, supply=123.0)
        assert limits(rig) == [1250, 1500], rig.calls
        assert miner.limit == 1500

    run_async(go())
