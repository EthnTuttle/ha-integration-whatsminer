"""Drive the real controller through ticks with fake HA/coordinator objects.

Catches attribute typos, wrong call order and command leaks that the pure
state-machine tests can't see. Not a substitute for the live hardware gate.
"""
from __future__ import annotations

import asyncio
import importlib
import pathlib
import sys
import types

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import ha_stubs  # noqa: E402

ha_stubs.install()

PKG = pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "whatsminer"
pkg = types.ModuleType("wm")
pkg.__path__ = [str(PKG)]
sys.modules["wm"] = pkg
controller_mod = importlib.import_module("wm.controller")
const = importlib.import_module("wm.const")
ds = importlib.import_module("wm.demand_shutoff")
pn = sys.modules["homeassistant.components.persistent_notification"]
Store = sys.modules["homeassistant.helpers.storage"].Store

T0 = 1_700_000_000.0
MIN = 60.0
THERMOSTATS = ["climate.den", "climate.great_room", "climate.back_bedroom", "climate.windowed_bedroom"]


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


def base_config(**over):
    cfg = {
        const.CONF_EXTERNAL_TEMP_SENSOR: "sensor.supply",
        const.CONF_PID_WEATHER_ENTITY: "weather.home",
        const.CONF_PID_DEMAND_ENTITIES: THERMOSTATS,
        const.CONF_PID_DEMAND_SHUTOFF_MODE: "active",
        const.CONF_PID_TARGET_TEMP: 104.0,
        const.CONF_PID_MIN_ADJUST_INTERVAL: 0,
        const.CONF_PID_MIN_ADJUST_INTERVAL_INCREASE: 0,
    }
    cfg.update(over)
    return cfg


class Rig:
    """A controller plus the fakes around it."""

    def __init__(self, monkeypatch, **cfg_over):
        Store.saved.clear()
        pn.created.clear()
        pn.dismissed.clear()
        sys.modules["homeassistant.helpers.event"].timers.clear()
        self.clock = Clock()
        monkeypatch.setattr(controller_mod, "time", self.clock)
        self.hass = ha_stubs.FakeHass()
        self.coord = ha_stubs.FakeCoordinator()
        self.pid_state = {"target": 104.0}
        entry = types.SimpleNamespace(entry_id="e1")
        self.ctl = controller_mod.WhatsminerController(
            self.hass, entry, self.coord, self.pid_state, base_config(**cfg_over)
        )
        self.set_supply(100.0)
        self.set_weather(55.0, forecast_temps=[55.0] * 24)
        self.set_thermostats("heating")

    # --- environment knobs ---------------------------------------------------
    def set_supply(self, value):
        if value is None:
            self.hass.states.items["sensor.supply"] = ha_stubs.FakeState("unavailable")
        else:
            self.hass.states.items["sensor.supply"] = ha_stubs.FakeState(
                str(value), {"unit_of_measurement": "°F"}, now=self.clock.t
            )

    def set_weather(self, temp, forecast_temps):
        self.hass.states.items["weather.home"] = ha_stubs.FakeState(
            "sunny", {"temperature": temp, "temperature_unit": "°F"}, now=self.clock.t
        )
        from datetime import datetime, timezone

        self.hass.services.forecast = [
            {
                "datetime": datetime.fromtimestamp(self.clock.t + 3600 * (i + 1), tz=timezone.utc).isoformat(),
                "temperature": t,
            }
            for i, t in enumerate(forecast_temps)
        ]
        self.ctl._forecast_cache_time = None  # force refetch

    def set_thermostats(self, action, mode="heat", cur=69.5, sp=70.0, age=10.0, ids=None):
        for eid in ids or THERMOSTATS:
            self.hass.states.items[eid] = ha_stubs.FakeState(
                mode,
                {"hvac_action": action, "current_temperature": cur, "temperature": sp},
                age_s=age,
                now=self.clock.t,
            )

    def set_freeze_sensor(self, value):
        self.hass.states.items["sensor.loop"] = ha_stubs.FakeState(
            str(value), {"unit_of_measurement": "°F"}, now=self.clock.t
        )

    def arm_gate(self, mean_f=60.0):
        """12 trailing hourly samples at mean_f plus a flat forecast."""
        self.ctl._outdoor_samples = [(self.clock.t - h * 3600, mean_f) for h in range(12)]
        self.set_weather(mean_f, [mean_f] * 24)

    def mining(self, on: bool, limit=None):
        self.coord.data["is_mining"] = on
        if limit is not None:
            self.coord.data["wattage_limit"] = limit

    # --- driving -----------------------------------------------------------------
    async def setup(self):
        await self.ctl.async_setup()

    async def tick(self, advance_s=30.0):
        self.clock.t += advance_s
        # Refresh "last_reported" of thermostats so they don't go stale by accident
        for eid in THERMOSTATS:
            st = self.hass.states.items.get(eid)
            if st is not None and st.state != "unavailable":
                self.set_thermostats(st.attributes["hvac_action"], st.state, st.attributes["current_temperature"],
                                     st.attributes["temperature"], 10.0, [eid])
        self.ctl._handle_coordinator_update()
        await asyncio.gather(*self.hass.tasks)
        self.hass.tasks.clear()

    async def timer_tick(self, advance_s=30.0):
        """The fallback timer, as HA fires it while the coordinator is quiet."""
        self.clock.t += advance_s
        self.ctl._handle_timer(None)
        await asyncio.gather(*self.hass.tasks)
        self.hass.tasks.clear()

    async def run(self, minutes, step_s=30.0):
        n = int(minutes * MIN / step_s)
        for _ in range(n):
            await self.tick(step_s)

    @property
    def calls(self):
        return self.coord.api.calls

    @property
    def shutoff(self):
        return self.pid_state["demand_shutoff"]


@pytest.fixture
def rig(monkeypatch):
    return Rig(monkeypatch)


def run_async(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# --------------------------------------------------------------------- tests


def test_startup_seeds_and_does_not_slam(rig):
    async def go():
        await rig.setup()
        await rig.tick()
        # First tick: seeded from the 4200 W limit; PID output ≈ 4200 + Kp·4°F
        # ≈ 4644 → delta 444 ≥ 250 fine: an actuation to ~4644 is allowed, but
        # it must NOT be a slam to power_min.
        limits = [w for c, w in rig.calls if c == "set_power_limit"]
        assert all(w > 1000 for w in limits)
        assert rig.pid_state["control_mode"] == "pid"
        assert rig.shutoff["state"] == "running"

    run_async(go())


def test_full_stop_and_resume_cycle(rig):
    async def go():
        await rig.setup()
        rig.arm_gate(60.0)
        rig.set_thermostats("idle")
        await rig.tick()
        assert rig.pid_state["control_mode"] in ("demand_lockout", "dwell")
        assert ("set_power_limit", 1000) in rig.calls  # lockout clamp
        assert rig.shutoff["gate"] == "armed"
        rig.coord.api.calls.clear()
        await rig.run(29)
        assert ("power_off", None) not in rig.calls
        assert rig.shutoff["state"] == "dwell"
        await rig.run(2)
        assert rig.calls.count(("power_off", None)) == 1
        assert rig.shutoff["state"] == "stopped" and rig.shutoff["active"] is True
        assert Store.saved["whatsminer.e1.controller"]["shutoff"]["owned"] is True
        assert rig.pid_state["control_mode"] == "stopped"
        assert rig.pid_state["safety_engaged"] is True
        # Miner reports not mining now
        rig.mining(False)
        rig.coord.api.calls.clear()
        await rig.run(10)
        assert rig.calls == []  # nothing while stopped and idle
        # Thermostat calls after min_off → resume
        rig.set_thermostats("heating", ids=["climate.den"])
        for _ in range(60):  # up to 30 min
            await rig.tick()
            if ("power_on", None) in rig.calls:
                break
        assert rig.calls.count(("power_on", None)) == 1
        assert rig.shutoff["state"] == "resuming"
        resumed_at = rig.clock.t
        rig.coord.api.calls.clear()
        # Miner comes back; boot hold prevents a limit change for 600 s
        rig.mining(True, limit=4200)
        while rig.clock.t < resumed_at + 9 * MIN:
            await rig.tick()
        assert rig.shutoff["state"] == "running"
        assert all(c != "set_power_limit" for c, _ in rig.calls), rig.calls
        await rig.run(2)
        assert any(c == "set_power_limit" for c, _ in rig.calls)

    run_async(go())


def test_stale_data_blocks_stop_commands(rig):
    async def go():
        await rig.setup()
        rig.arm_gate(60.0)
        rig.set_thermostats("idle")
        await rig.run(29)
        rig.coord.last_update_success = False
        rig.coord.api.calls.clear()
        await rig.run(5)
        assert rig.calls == []
        assert "miner data stale" in rig.shutoff["blocking"]

    run_async(go())


def test_freeze_sensor_blocks_stop_and_force_resumes(monkeypatch):
    rig = Rig(monkeypatch, **{const.CONF_FREEZE_GUARD_SENSOR: "sensor.loop"})

    async def go():
        await rig.setup()
        rig.arm_gate(60.0)
        rig.set_thermostats("idle")
        rig.set_freeze_sensor(38.0)
        await rig.run(45)
        assert ("power_off", None) not in rig.calls
        assert rig.pid_state["freeze_guard"]["active"] is True
        assert rig.pid_state["freeze_guard"]["source"] == "sensor"
        assert "freeze guard" in rig.shutoff["blocking"]
        # Freeze clears (above threshold + hysteresis) → stop proceeds
        rig.set_freeze_sensor(44.0)
        await rig.run(1)
        assert rig.calls.count(("power_off", None)) == 1
        rig.mining(False)
        rig.coord.api.calls.clear()
        # Freeze returns while stopped → immediate power_on, no min_off wait
        rig.set_freeze_sensor(39.0)
        await rig.tick()
        assert rig.calls == [("power_on", None)]
        assert "freeze guard" in rig.shutoff["reason"]

    run_async(go())


def test_freeze_weather_fallback_uses_forecast_minimum(rig):
    async def go():
        await rig.setup()
        rig.arm_gate(60.0)
        rig.set_thermostats("idle")
        # Current 60°F but forecast dips to 36°F in 8 h → freeze risk
        rig.set_weather(60.0, [55, 50, 45, 42, 40, 38, 37, 36] + [45] * 16)
        await rig.run(40)
        fg = rig.pid_state["freeze_guard"]
        assert fg["source"] == "weather" and fg["value"] == 36.0 and fg["active"] is True
        assert ("power_off", None) not in rig.calls

    run_async(go())


def test_no_freeze_source_blocks_s_stop_under_cold_gate(monkeypatch):
    rig = Rig(monkeypatch, **{const.CONF_PID_WEATHER_ENTITY: None})

    async def go():
        await rig.setup()
        rig.set_thermostats("idle")
        rig.set_supply(125.0)  # S trigger
        await rig.run(15)
        assert ("power_off", None) not in rig.calls
        assert rig.pid_state["freeze_guard"]["status"] == "unknown"
        assert "freeze status unknown (cold gate)" in rig.shutoff["blocking"]
        assert rig.pid_state["control_mode"] == "dwell"
        # Soft cap still clamps to power_min
        assert ("set_power_limit", 1000) in rig.calls

    run_async(go())


def test_supply_lockout_latches_and_freeze_unlatches(monkeypatch):
    rig = Rig(monkeypatch, **{const.CONF_FREEZE_GUARD_SENSOR: "sensor.loop"})

    async def go():
        await rig.setup()
        rig.set_freeze_sensor(50.0)
        rig.set_supply(141.0)
        await rig.tick()
        assert rig.calls[-1] == ("power_off", None)
        assert rig.pid_state["lockout_latched"] is True
        assert rig.pid_state["control_mode"] == "latched"
        assert Store.saved["whatsminer.e1.controller"]["lockout_latched"] is True
        rig.mining(False)
        rig.coord.api.calls.clear()
        # Freeze while latched and loop still hot: wait
        rig.set_freeze_sensor(35.0)
        rig.set_supply(130.0)
        await rig.tick()
        assert rig.calls == []
        # Loop cools below the soft cap: unlatch + power_on
        rig.set_supply(118.0)
        await rig.tick()
        assert rig.calls == [("power_on", None)]
        assert rig.pid_state["lockout_latched"] is False
        assert any(k == "whatsminer_freeze_unlatch" for k, _, _ in pn.created)

    run_async(go())


def test_freeze_over_lockout_holds_power_min_instead_of_stopping(monkeypatch):
    rig = Rig(monkeypatch, **{const.CONF_FREEZE_GUARD_SENSOR: "sensor.loop"})

    async def go():
        await rig.setup()
        rig.set_freeze_sensor(35.0)
        rig.set_supply(145.0)
        await rig.tick()
        assert ("power_off", None) not in rig.calls
        assert rig.pid_state["lockout_latched"] is False
        assert ("set_power_limit", 1000) in rig.calls
        assert any(k == "whatsminer_freeze_over_lockout" for k, _, _ in pn.created)
        assert rig.pid_state["control_mode"] == "safety_cap"

    run_async(go())


def test_observe_mode_never_commands(monkeypatch):
    rig = Rig(monkeypatch, **{const.CONF_PID_DEMAND_SHUTOFF_MODE: "observe"})

    async def go():
        await rig.setup()
        rig.arm_gate(60.0)
        rig.set_thermostats("idle")
        await rig.run(35)
        assert all(c != "power_off" for c, _ in rig.calls)
        assert rig.shutoff["state"] == "stopped" and rig.shutoff["active"] is False
        assert rig.shutoff["would_stop"] is not None
        # Still mining in reality; PID keeps clamping, control mode not "stopped"
        assert rig.pid_state["control_mode"] in ("demand_lockout", "dwell")

    run_async(go())


def test_user_mining_override_clears_ownership(rig):
    async def go():
        await rig.setup()
        rig.arm_gate(60.0)
        rig.set_thermostats("idle")
        await rig.run(31)
        assert rig.shutoff["active"] is True
        await rig.ctl.async_user_mining_override(True)
        assert rig.shutoff["active"] is False and rig.shutoff["state"] == "suppressed"
        rig.coord.api.calls.clear()
        await rig.run(45)
        assert ("power_off", None) not in rig.calls  # suppressed
        rig.set_thermostats("heating", ids=["climate.den"])
        await rig.tick()
        assert rig.shutoff["state"] == "running"
        # user OFF: never auto-resumed
        await rig.ctl.async_user_mining_override(False)
        rig.mining(False)
        rig.coord.api.calls.clear()
        await rig.run(60)
        assert rig.calls == []
        assert rig.pid_state["control_mode"] == "idle"

    run_async(go())


def test_restart_restores_owned_stop_and_resumes(monkeypatch):
    rig = Rig(monkeypatch)

    async def go():
        await rig.setup()
        rig.arm_gate(60.0)
        rig.set_thermostats("idle")
        await rig.run(31)
        assert rig.shutoff["active"] is True

    run_async(go())
    saved = Store.saved["whatsminer.e1.controller"]
    # "Restart": a fresh controller instance restored from the same store
    assert saved["shutoff"]["owned"] is True

    rig2 = Rig(monkeypatch)
    Store.saved["whatsminer.e1.controller"] = saved

    async def go2():
        await rig2.setup()
        assert rig2.ctl._shutoff.owned is True and rig2.ctl._shutoff.state == "stopped"
        rig2.mining(False)
        rig2.clock.t = saved["shutoff"]["since"] + 40 * MIN
        rig2.arm_gate(60.0)
        rig2.set_thermostats("heating", ids=["climate.den"])
        rig2.set_thermostats("idle", ids=THERMOSTATS[1:])
        await rig2.tick()
        await rig2.tick()
        assert rig2.calls == [("power_on", None)]

    run_async(go2())


def test_probe_lost_runs_fallback_and_s_trigger_inactive(rig):
    async def go():
        await rig.setup()
        rig.set_supply(None)
        rig.set_weather(30.0, [30.0] * 24)
        await rig.tick()
        assert rig.pid_state["control_mode"] == "fallback"
        limits = [w for c, w in rig.calls if c == "set_power_limit"]
        assert limits and limits[-1] == 3400  # 30°F on a 10..60°F curve → 60% of range
        assert rig.shutoff["state"] == "running"

    run_async(go())


def test_release_when_mode_turned_off_with_owned_stop(monkeypatch):
    rig = Rig(monkeypatch)

    async def go():
        await rig.setup()
        rig.arm_gate(60.0)
        rig.set_thermostats("idle")
        await rig.run(31)

    run_async(go())
    saved = Store.saved["whatsminer.e1.controller"]
    rig2 = Rig(monkeypatch, **{const.CONF_PID_DEMAND_SHUTOFF_MODE: "off"})
    Store.saved["whatsminer.e1.controller"] = saved

    async def go2():
        await rig2.setup()
        rig2.mining(False)
        await rig2.tick()
        assert rig2.calls == [("power_on", None)]
        assert rig2.shutoff["state"] == "disabled"

    run_async(go2())


def test_timer_tick_resumes_when_coordinator_is_quiet(rig):
    """Miner powered off and not answering: coordinator stops notifying, timer ticks."""
    event = sys.modules["homeassistant.helpers.event"]

    async def go():
        await rig.setup()
        assert len(event.timers) == 1
        rig.arm_gate(60.0)
        rig.set_thermostats("idle")
        await rig.run(31)
        assert rig.shutoff["active"] is True
        rig.coord.last_update_success = False  # summary fails while off
        rig.coord.api.calls.clear()
        rig.set_thermostats("heating", ids=["climate.den"])
        timer_cb, _ = event.timers[0]
        for _ in range(70):
            rig.clock.t += 30
            timer_cb(None)
            await asyncio.gather(*rig.hass.tasks)
            rig.hass.tasks.clear()
            if rig.calls:
                break
        assert rig.calls == [("power_on", None)]

    run_async(go())


def test_unlatch_power_on_failure_is_retried_soon(monkeypatch):
    rig = Rig(monkeypatch, **{const.CONF_FREEZE_GUARD_SENSOR: "sensor.loop"})

    async def go():
        await rig.setup()
        rig.set_freeze_sensor(50.0)
        rig.set_supply(141.0)
        await rig.tick()
        assert rig.pid_state["lockout_latched"] is True
        rig.mining(False)
        rig.coord.api.calls.clear()
        rig.coord.api.fail.add("power_on")
        rig.set_freeze_sensor(35.0)
        rig.set_supply(110.0)
        await rig.tick()
        assert rig.pid_state["lockout_latched"] is False
        assert rig.ctl._shutoff.owned and rig.ctl._shutoff.state == "resuming"
        assert rig.pid_state["control_mode"] == "resuming"
        rig.coord.api.fail.clear()
        await rig.run(1.5)
        assert rig.calls.count(("power_on", None)) == 1  # retried within ~60 s
        rig.mining(True)
        await rig.tick()
        assert rig.ctl._shutoff.state == "running" and not rig.ctl._shutoff.owned

    run_async(go())


def test_power_off_failure_keeps_ownership_and_retries(rig):
    async def go():
        await rig.setup()
        rig.arm_gate(60.0)
        rig.set_thermostats("idle")
        rig.coord.api.fail.add("power_off")
        await rig.run(31)
        assert rig.ctl._shutoff.owned and rig.ctl._shutoff.state == "stopped"
        assert rig.ctl._shutoff.stop_failed_at is not None
        rig.coord.api.fail.clear()
        rig.coord.api.calls.clear()
        await rig.run(3.5)
        assert rig.calls.count(("power_off", None)) == 1
        # The miner is really off now; a call resumes it
        rig.mining(False)
        rig.coord.api.calls.clear()
        rig.set_thermostats("heating", ids=["climate.den"])
        rig.arm_gate(50.0)
        await rig.run(2)
        assert ("power_on", None) in rig.calls

    run_async(go())


def test_unknown_limit_after_resume_still_commands(rig):
    async def go():
        await rig.setup()
        rig.arm_gate(60.0)
        rig.set_thermostats("idle")
        await rig.run(31)
        rig.mining(False, limit=0)
        rig.set_thermostats("heating", ids=["climate.den"])
        rig.arm_gate(50.0)  # cold gate: min_off waived
        await rig.run(2)
        assert ("power_on", None) in rig.calls
        rig.coord.api.calls.clear()
        rig.mining(True, limit=0)  # firmware reports no limit yet
        rig.set_supply(85.0)
        await rig.run(11)  # past the boot hold
        limits = [w for c, w in rig.calls if c == "set_power_limit"]
        assert limits and limits[0] > 1000

    run_async(go())


def test_degraded_setup_resumes_owned_stop(monkeypatch):
    """Store says we own a stop; coordinator never succeeded since restart."""
    rig = Rig(monkeypatch)
    Store.saved["whatsminer.e1.controller"] = {
        "lockout_latched": False,
        "shutoff": {"state": "stopped", "owned": True, "since": T0 - 3600, "gate_armed": False},
        "outdoor_samples": [],
        "freeze_active": False,
    }
    rig.coord.last_update_success = False
    rig.coord.data["is_mining"] = False

    async def go():
        await rig.setup()
        rig.set_thermostats("heating", ids=["climate.den"])
        rig.set_thermostats("idle", ids=THERMOSTATS[1:])
        await rig.tick()
        await rig.tick()
        assert rig.calls == [("power_on", None)]

    run_async(go())


def test_timer_tick_before_first_coordinator_callback_seeds_first(monkeypatch):
    """Regression (2026-10-07): the fallback timer fired before the coordinator's first
    callback and the unseeded PID commanded Kp·error — 4200 → 2437 W on a cold loop."""
    rig = Rig(monkeypatch)
    rig.mining(True, limit=4200)
    rig.coord.data["uptime"] = 86400

    async def go():
        await rig.setup()
        rig.set_supply(82.0)  # 22°F below target: Kp·error alone is ~2440 W
        await rig.timer_tick()
        assert rig.calls == [], rig.calls
        assert abs(rig.pid_state["requested_output"] - 4200) <= 5  # seeded to the running limit
        await rig.tick()
        assert all(w >= 4200 for c, w in rig.calls if c == "set_power_limit"), rig.calls

    run_async(go())


def test_restart_from_own_limit_change_keeps_adjust_interval(monkeypatch):
    """Regression (2026-10-07): the stop edge of the restart each limit change causes
    zeroed the throttle clock, so a 1800 s min_adjust_interval still actuated every
    ~630 s (boot hold + one tick) and the loop cycled 2000 ↔ 4200 W."""
    rig = Rig(
        monkeypatch,
        **{
            const.CONF_PID_MIN_ADJUST_INTERVAL: 1800,
            const.CONF_PID_MIN_ADJUST_INTERVAL_INCREASE: 1800,
            const.CONF_PID_DEMAND_SHUTOFF_MODE: "off",
        },
    )
    rig.mining(True, limit=3000)
    rig.coord.data["uptime"] = 86400

    async def go():
        await rig.setup()
        await rig.tick()
        rig.set_supply(80.0)  # far below target: the PID wants more power
        for _ in range(20):
            await rig.tick()
            if rig.calls:
                break
        limits = [(c, w) for c, w in rig.calls if c == "set_power_limit"]
        assert len(limits) == 1, rig.calls
        first_at = rig.clock.t

        # The restart that limit change causes: one poll not mining, then booting.
        rig.mining(False, limit=0)
        rig.coord.data["uptime"] = 0
        await rig.tick(15)
        rig.mining(True, limit=limits[0][1])
        await rig.tick(15)

        rig.set_supply(115.0)  # now far above target: the PID wants less power
        while rig.clock.t - first_at < 1800 - 30:
            await rig.tick()
            rig.coord.data["uptime"] = rig.coord.data.get("uptime", 0) + 30
        assert len([1 for c, _ in rig.calls if c == "set_power_limit"]) == 1, rig.calls
        await rig.run(2)
        assert len([1 for c, _ in rig.calls if c == "set_power_limit"]) == 2, rig.calls

    run_async(go())
