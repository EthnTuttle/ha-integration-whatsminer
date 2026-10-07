"""The heating controller: PID on the supply probe, safety caps, demand shutoff.

One instance per config entry, created in ``async_setup_entry`` and stored in
``hass.data[DOMAIN][entry_id]["controller"]``. It is not an entity; the
diagnostic sensors and binary sensors read the shared ``pid_state`` dict that
this object publishes on every coordinator poll.

Order of authority on each tick:
    supply lockout latch > freeze guard > safety caps > demand shutoff
    > demand lockout > PID (or the probe-loss fallback curve)

PID is always on. Mining Control is the only manual override: a user's OFF is
never auto-resumed, and a user's ON suppresses the shutoff until a thermostat
calls again.
"""
from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import replace
from datetime import datetime, timedelta
from time import time
from typing import Any, NamedTuple

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util.unit_conversion import TemperatureConverter

from . import demand_shutoff as ds
from .const import (
    CONF_CHIP_TEMP_SAFETY_CAP,
    CONF_EXTERNAL_TEMP_SENSOR,
    CONF_FREEZE_GUARD_FORECAST_HOURS,
    CONF_FREEZE_GUARD_SENSOR,
    CONF_FREEZE_GUARD_THRESHOLD,
    CONF_PID_COARSE_STEP_BAND,
    CONF_PID_DEMAND_ENTITIES,
    CONF_PID_DEMAND_SHUTOFF_COLD_ROOM_DELTA,
    CONF_PID_DEMAND_SHUTOFF_HYSTERESIS,
    CONF_PID_DEMAND_SHUTOFF_IDLE_DWELL_MIN,
    CONF_PID_DEMAND_SHUTOFF_MIN_OFF_MIN,
    CONF_PID_DEMAND_SHUTOFF_MIN_ON_MIN,
    CONF_PID_DEMAND_SHUTOFF_MODE,
    CONF_PID_DEMAND_SHUTOFF_OUTDOOR_MIN,
    CONF_PID_DEMAND_SHUTOFF_SUPPLY_DWELL_MIN,
    CONF_PID_DEMAND_SHUTOFF_SUPPLY_STOP,
    CONF_PID_DEMAND_SHUTOFF_UNKNOWN_GRACE_MIN,
    CONF_PID_FALLBACK_OUTDOOR_COLD,
    CONF_PID_FALLBACK_OUTDOOR_WARM,
    CONF_PID_FINE_STEP_BAND,
    CONF_PID_FORECAST_BLEND,
    CONF_PID_FORECAST_LOOKAHEAD_MIN,
    CONF_PID_INTEGRAL_BAND,
    CONF_PID_KD,
    CONF_PID_KE,
    CONF_PID_KI,
    CONF_PID_KP,
    CONF_PID_MIN_ADJUST_INTERVAL,
    CONF_PID_MIN_ADJUST_INTERVAL_INCREASE,
    CONF_PID_MIN_POWER_STEP,
    CONF_PID_MIN_POWER_STEP_FINE,
    CONF_PID_MIN_POWER_STEP_MEDIUM,
    CONF_PID_OUTDOOR_TEMP_SENSOR,
    CONF_PID_PRICE_HIGH,
    CONF_PID_PRICE_LOW,
    CONF_PID_PRICE_SENSOR,
    CONF_PID_SETPOINT_RAMP_RATE,
    CONF_PID_SLOPE_EWMA_TAU_S,
    CONF_PID_SUPPLY_TEMP_LOCKOUT,
    CONF_PID_SUPPLY_TEMP_SAFETY_CAP,
    CONF_PID_SURPLUS_DEFICIT,
    CONF_PID_SURPLUS_FULL,
    CONF_PID_SURPLUS_SENSOR,
    CONF_PID_TARGET_TEMP,
    CONF_PID_WEATHER_ENTITY,
    CONF_POWER_MAX,
    CONF_POWER_MIN,
    DEFAULT_CHIP_TEMP_SAFETY_CAP,
    DEFAULT_FREEZE_GUARD_FORECAST_HOURS,
    DEFAULT_FREEZE_GUARD_THRESHOLD,
    DEFAULT_PID_COARSE_STEP_BAND,
    DEFAULT_PID_DEMAND_ENTITIES,
    DEFAULT_PID_DEMAND_SHUTOFF_COLD_ROOM_DELTA,
    DEFAULT_PID_DEMAND_SHUTOFF_HYSTERESIS,
    DEFAULT_PID_DEMAND_SHUTOFF_IDLE_DWELL_MIN,
    DEFAULT_PID_DEMAND_SHUTOFF_MIN_OFF_MIN,
    DEFAULT_PID_DEMAND_SHUTOFF_MIN_ON_MIN,
    DEFAULT_PID_DEMAND_SHUTOFF_MODE,
    DEFAULT_PID_DEMAND_SHUTOFF_OUTDOOR_MIN,
    DEFAULT_PID_DEMAND_SHUTOFF_SUPPLY_DWELL_MIN,
    DEFAULT_PID_DEMAND_SHUTOFF_SUPPLY_STOP,
    DEFAULT_PID_DEMAND_SHUTOFF_UNKNOWN_GRACE_MIN,
    DEFAULT_PID_FALLBACK_OUTDOOR_COLD,
    DEFAULT_PID_FALLBACK_OUTDOOR_WARM,
    DEFAULT_PID_FINE_STEP_BAND,
    DEFAULT_PID_FORECAST_BLEND,
    DEFAULT_PID_FORECAST_LOOKAHEAD_MIN,
    DEFAULT_PID_INTEGRAL_BAND,
    DEFAULT_PID_KD,
    DEFAULT_PID_KE,
    DEFAULT_PID_KI,
    DEFAULT_PID_KP,
    DEFAULT_PID_MIN_ADJUST_INTERVAL,
    DEFAULT_PID_MIN_ADJUST_INTERVAL_INCREASE,
    DEFAULT_PID_MIN_POWER_STEP,
    DEFAULT_PID_MIN_POWER_STEP_FINE,
    DEFAULT_PID_MIN_POWER_STEP_MEDIUM,
    DEFAULT_PID_SETPOINT_RAMP_RATE,
    DEFAULT_PID_SLOPE_EWMA_TAU_S,
    DEFAULT_PID_SUPPLY_TEMP_LOCKOUT,
    DEFAULT_PID_SUPPLY_TEMP_SAFETY_CAP,
    DEFAULT_PID_TARGET_TEMP,
    DEFAULT_POWER_MAX,
    DEFAULT_POWER_MIN,
    DOMAIN,
    FREEZE_GUARD_RELEASE_HYSTERESIS,
)
from .coordinator import WhatsminerCoordinator
from .pid_controller import PID

_LOGGER = logging.getLogger(__name__)

# While the supply lockout is latched, re-send power_off at most this often if
# the miner is found mining (e.g. started from its own web UI).
LOCKOUT_REASSERT_INTERVAL = 180.0
# After a power_on the miner boots for minutes; an adjust_power_limit in that
# window is a second restart. Skip non-safety limit changes for this long.
RESUME_BOOT_HOLD_S = 600.0
# Retry a power_on whose send failed (timeout, token error) after this long.
POWER_ON_RETRY_S = 60.0
# Learned power floor. The firmware accepts any limit and treats it as a
# ceiling; below the hashboards' real minimum it cannot find a frequency
# solution and simply restarts btminer, forever (M64: ~3 min cycles at 1000 W).
# A restart is an Elapsed (uptime) regression, confirmed by the next poll (a
# single garbled summary reads Elapsed 0 too), that we did not command. Two in
# a row from runs shorter than FLOOR_STABLE_S at about the same limit prove
# that limit unholdable and raise the effective floor to limit +
# FLOOR_RAISE_STEP; every further crash in the same episode raises again. A
# run that hashes for FLOOR_STABLE_S proves its limit holdable and ends the
# episode. The floor can climb at most FLOOR_MAX_RAISE_W above power_min;
# restarts above that ceiling are reported as a restart loop, not learned.
FLOOR_STABLE_S = 900.0
FLOOR_SHORT_RUNS_TO_LEARN = 2
FLOOR_RAISE_STEP = 250
FLOOR_MAX_RAISE_W = 1500
# Re-send a floor enforcement command no sooner than this if the send failed.
FLOOR_FIRE_RETRY_S = 60.0
# Caps that clamp to the configured power_min even when a higher floor has
# been learned: the chip-temp cap and the freeze-guard hold over the supply
# lockout. There the alternative to an intermittent crash loop is up to
# FLOOR_MAX_RAISE_W more watts into chips already over temperature or a loop
# already past its hard limit, and the crash loop dissipates less heat.
HARD_CAPS = frozenset({"chip", "lockout"})
# After a user's Mining Control OFF, ignore lingering "mining" readings for this
# long so the PID can't send a limit change to a miner that is powering down.
USER_OFF_GRACE_S = 180.0
# Hourly forecast cache (met.no updates hourly), and how long a stale cache is
# still used when the forecast service keeps failing.
FORECAST_CACHE_S = 900.0
FORECAST_STALE_MAX_S = 6 * 3600.0
# Hourly outdoor sample spacing for the centred 24 h mean.
OUTDOOR_SAMPLE_INTERVAL_S = 3600.0

STORE_VERSION = 1


class _Regression(NamedTuple):
    """An Elapsed regression awaiting confirmation by the next poll."""

    prev_uptime: float
    seen_uptime: float
    limit: int | None
    ours: bool


class WhatsminerController:
    """Owns the control loop for one miner."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        coordinator: WhatsminerCoordinator,
        pid_state: dict,
        config: dict[str, Any],
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.coordinator = coordinator
        self._pid_state = pid_state
        g = config.get

        self._power_min = int(g(CONF_POWER_MIN, DEFAULT_POWER_MIN))
        self._power_max = int(g(CONF_POWER_MAX, DEFAULT_POWER_MAX))
        self._kp = float(g(CONF_PID_KP, DEFAULT_PID_KP))
        self._ke = float(g(CONF_PID_KE, DEFAULT_PID_KE))
        self._default_target = float(g(CONF_PID_TARGET_TEMP, DEFAULT_PID_TARGET_TEMP))
        self._external_sensor_id: str | None = g(CONF_EXTERNAL_TEMP_SENSOR) or None
        self._outdoor_temp_sensor_id: str | None = g(CONF_PID_OUTDOOR_TEMP_SENSOR) or None
        self._min_power_step = int(g(CONF_PID_MIN_POWER_STEP, DEFAULT_PID_MIN_POWER_STEP))
        self._min_power_step_medium = int(
            g(CONF_PID_MIN_POWER_STEP_MEDIUM, DEFAULT_PID_MIN_POWER_STEP_MEDIUM)
        )
        self._min_power_step_fine = int(
            g(CONF_PID_MIN_POWER_STEP_FINE, DEFAULT_PID_MIN_POWER_STEP_FINE)
        )
        self._coarse_step_band = float(g(CONF_PID_COARSE_STEP_BAND, DEFAULT_PID_COARSE_STEP_BAND))
        self._fine_step_band = float(g(CONF_PID_FINE_STEP_BAND, DEFAULT_PID_FINE_STEP_BAND))
        self._min_adjust_interval = int(
            g(CONF_PID_MIN_ADJUST_INTERVAL, DEFAULT_PID_MIN_ADJUST_INTERVAL)
        )
        self._min_adjust_interval_increase = int(
            g(CONF_PID_MIN_ADJUST_INTERVAL_INCREASE, DEFAULT_PID_MIN_ADJUST_INTERVAL_INCREASE)
        )
        self._chip_temp_safety_cap = float(g(CONF_CHIP_TEMP_SAFETY_CAP, DEFAULT_CHIP_TEMP_SAFETY_CAP))
        self._supply_temp_safety_cap = float(
            g(CONF_PID_SUPPLY_TEMP_SAFETY_CAP, DEFAULT_PID_SUPPLY_TEMP_SAFETY_CAP)
        )
        self._supply_temp_lockout = float(
            g(CONF_PID_SUPPLY_TEMP_LOCKOUT, DEFAULT_PID_SUPPLY_TEMP_LOCKOUT)
        )
        self._demand_entities: list[str] = list(
            g(CONF_PID_DEMAND_ENTITIES, DEFAULT_PID_DEMAND_ENTITIES) or []
        )
        self._integral_band = float(g(CONF_PID_INTEGRAL_BAND, DEFAULT_PID_INTEGRAL_BAND))
        self._setpoint_ramp_rate = float(g(CONF_PID_SETPOINT_RAMP_RATE, DEFAULT_PID_SETPOINT_RAMP_RATE))
        self._slope_ewma_tau_s = float(g(CONF_PID_SLOPE_EWMA_TAU_S, DEFAULT_PID_SLOPE_EWMA_TAU_S))
        self._price_sensor_id: str | None = g(CONF_PID_PRICE_SENSOR) or None
        self._price_high = float(g(CONF_PID_PRICE_HIGH, 0.0))
        self._price_low = float(g(CONF_PID_PRICE_LOW, 0.0))
        self._surplus_sensor_id: str | None = g(CONF_PID_SURPLUS_SENSOR) or None
        self._surplus_deficit = float(g(CONF_PID_SURPLUS_DEFICIT, 0.0))
        self._surplus_full = float(g(CONF_PID_SURPLUS_FULL, 0.0))
        self._weather_entity_id: str | None = g(CONF_PID_WEATHER_ENTITY) or None
        self._forecast_lookahead_min = int(
            g(CONF_PID_FORECAST_LOOKAHEAD_MIN, DEFAULT_PID_FORECAST_LOOKAHEAD_MIN)
        )
        self._forecast_blend = float(g(CONF_PID_FORECAST_BLEND, DEFAULT_PID_FORECAST_BLEND))
        self._fallback_outdoor_cold = float(
            g(CONF_PID_FALLBACK_OUTDOOR_COLD, DEFAULT_PID_FALLBACK_OUTDOOR_COLD)
        )
        self._fallback_outdoor_warm = float(
            g(CONF_PID_FALLBACK_OUTDOOR_WARM, DEFAULT_PID_FALLBACK_OUTDOOR_WARM)
        )
        # Demand shutoff
        self._shutoff_cfg = ds.ShutoffConfig(
            mode=str(g(CONF_PID_DEMAND_SHUTOFF_MODE, DEFAULT_PID_DEMAND_SHUTOFF_MODE)),
            has_entities=bool(self._demand_entities),
            outdoor_min=float(g(CONF_PID_DEMAND_SHUTOFF_OUTDOOR_MIN, DEFAULT_PID_DEMAND_SHUTOFF_OUTDOOR_MIN)),
            hysteresis=float(g(CONF_PID_DEMAND_SHUTOFF_HYSTERESIS, DEFAULT_PID_DEMAND_SHUTOFF_HYSTERESIS)),
            idle_dwell_s=60.0 * float(g(CONF_PID_DEMAND_SHUTOFF_IDLE_DWELL_MIN, DEFAULT_PID_DEMAND_SHUTOFF_IDLE_DWELL_MIN)),
            min_off_s=60.0 * float(g(CONF_PID_DEMAND_SHUTOFF_MIN_OFF_MIN, DEFAULT_PID_DEMAND_SHUTOFF_MIN_OFF_MIN)),
            min_on_s=60.0 * float(g(CONF_PID_DEMAND_SHUTOFF_MIN_ON_MIN, DEFAULT_PID_DEMAND_SHUTOFF_MIN_ON_MIN)),
            supply_stop=bool(g(CONF_PID_DEMAND_SHUTOFF_SUPPLY_STOP, DEFAULT_PID_DEMAND_SHUTOFF_SUPPLY_STOP)),
            supply_dwell_s=60.0 * float(g(CONF_PID_DEMAND_SHUTOFF_SUPPLY_DWELL_MIN, DEFAULT_PID_DEMAND_SHUTOFF_SUPPLY_DWELL_MIN)),
            unknown_grace_s=60.0 * float(g(CONF_PID_DEMAND_SHUTOFF_UNKNOWN_GRACE_MIN, DEFAULT_PID_DEMAND_SHUTOFF_UNKNOWN_GRACE_MIN)),
            soft_cap=self._supply_temp_safety_cap,
            lockout=self._supply_temp_lockout,
            freeze_threshold=float(g(CONF_FREEZE_GUARD_THRESHOLD, DEFAULT_FREEZE_GUARD_THRESHOLD)),
        )
        self._cold_room_delta = float(
            g(CONF_PID_DEMAND_SHUTOFF_COLD_ROOM_DELTA, DEFAULT_PID_DEMAND_SHUTOFF_COLD_ROOM_DELTA)
        )
        # Freeze guard
        self._freeze_sensor_id: str | None = g(CONF_FREEZE_GUARD_SENSOR) or None
        self._freeze_threshold = float(g(CONF_FREEZE_GUARD_THRESHOLD, DEFAULT_FREEZE_GUARD_THRESHOLD))
        self._freeze_forecast_hours = int(
            g(CONF_FREEZE_GUARD_FORECAST_HOURS, DEFAULT_FREEZE_GUARD_FORECAST_HOURS)
        )
        self._freeze_active = False
        self._freeze_unknown_logged = False
        self._freeze_hold_over_lockout = False
        self._freeze_off_notified = False

        # --- runtime state ---------------------------------------------------
        self._shutoff = ds.ShutoffState()
        self._outdoor_samples: list[tuple[float, float]] = []
        self._store: Store = Store(hass, STORE_VERSION, f"{DOMAIN}.{entry.entry_id}.controller")
        self._unsub_coordinator = None
        self._unsub_timer = None
        self._last_tick_at = 0.0
        self._user_off_at = 0.0
        self._step_task: asyncio.Task | None = None
        self._saved_target: float | None = None
        self._resume_hold_until = 0.0
        self._max_off_notified = False
        self._no_demand_logged = False
        self._demand_unavail_logged = False
        self._slope_ewma: float | None = None
        self._slope_last_pv: tuple[float, float] | None = None
        self._price_unavail_logged = False
        self._surplus_unavail_logged = False
        self._forecast_cache: list[tuple[float, float]] = []
        self._forecast_cache_time: float | None = None
        self._in_fallback = False
        self._ramped_target: float | None = None
        self._external_unavail_logged = False
        self._outdoor_unavail_logged = False
        self._freeze_unavail_logged = False
        self._last_input_time: float | None = None
        self._last_commanded_power: int | None = None
        self._last_is_mining: bool | None = None
        self._last_command_time: float = 0.0
        self._step_lock = asyncio.Lock()
        self._caps_active: frozenset[str] = frozenset()
        self._last_lockout_power_off: float = 0.0
        # Restart attribution and the learned floor. _last_actuation_at is
        # never zeroed (unlike _last_command_time) so it survives the stop
        # edge; _pending_restart_at is consumed by the first uptime regression
        # after one of our own commands.
        self._last_actuation_at: float = 0.0
        self._pending_restart_at: float | None = None
        self._pending_regression: _Regression | None = None
        self._last_restart_ours = False
        self._sent_limit: int | None = None
        self._sent_limit_at: float = 0.0
        self._last_uptime: float | None = None
        self._run_limit: int | None = None
        self._floor_short_runs = 0
        self._floor_short_run_limit: int | None = None
        self._floor_learned: int | None = None
        self._floor_learned_at: float | None = None
        self._floor_learn_limit: int | None = None
        self._floor_proven_ok: int | None = None
        self._floor_exhausted = False
        self._restart_loop_notified = False
        self._floor_chip_notified = False
        self._last_floor_fire_at: float = 0.0
        self._pid = PID(
            kp=self._kp,
            ki=float(g(CONF_PID_KI, DEFAULT_PID_KI)),
            kd=float(g(CONF_PID_KD, DEFAULT_PID_KD)),
            ke=self._ke,
            out_min=float(self._power_min),
            out_max=float(self._power_max),
            sampling_period=0,
        )
        self._pid_state.setdefault("lockout_latched", False)
        if self._pid_state.get("target") is None:
            self._pid_state["target"] = self._default_target
        self._publish_shutoff(ds.Decision(ds.NONE, "starting", (), self._shutoff))
        self._publish_freeze(None, None, None)
        self._publish_floor()
        self._pid_state["control_mode"] = "idle"

    # ------------------------------------------------------------------ setup

    async def async_setup(self) -> None:
        """Restore persisted state and start listening to the coordinator."""
        data = await self._store.async_load() or {}
        now = time()
        if data.get("lockout_latched"):
            self._pid_state["lockout_latched"] = True
            self._pid_state["safety_engaged"] = True
            _LOGGER.warning(
                "Supply temperature lockout is still latched from before restart — "
                "mining stays off until Reset Supply Lockout is pressed"
            )
        if data.get("target") is not None:
            # The PID Target number restores its own value later in startup;
            # until then seed from our copy so the first tick uses the right SP.
            try:
                self._pid_state["target"] = float(data["target"])
            except (TypeError, ValueError):
                pass
        self._saved_target = self._pid_state.get("target")
        if self._target() >= self._supply_temp_safety_cap:
            _LOGGER.warning(
                "PID target %.1f°F is at or above the %.1f°F supply safety cap — the cap "
                "will clamp output in normal operation; lower the target or raise the cap",
                self._target(), self._supply_temp_safety_cap,
            )
        self._shutoff = ds.ShutoffState.from_dict(data.get("shutoff"))
        if self._shutoff.simulated and not self._shutoff_cfg.observe:
            # Observe-mode simulation does not carry into active/off.
            self._shutoff = ds.ShutoffState(gate_armed=self._shutoff.gate_armed)
        if self._shutoff.owned:
            _LOGGER.warning(
                "Demand shutoff owned a miner stop before restart (state %s) — "
                "resume logic continues from persisted state",
                self._shutoff.state,
            )
        self._outdoor_samples = ds.trim_samples(
            [(float(ts), float(v)) for ts, v in data.get("outdoor_samples") or []], now
        )
        self._freeze_active = bool(data.get("freeze_active", False))
        try:
            learned = data.get("floor_learned")
            if learned is not None and int(learned) > self._power_min:
                self._floor_learned = int(learned)
                learned_at, learn_limit = data.get("floor_learned_at"), data.get("floor_learn_limit")
                self._floor_learned_at = float(learned_at) if learned_at is not None else None
                self._floor_learn_limit = int(learn_limit) if learn_limit is not None else None
            if data.get("floor_proven_ok") is not None:
                self._floor_proven_ok = int(data["floor_proven_ok"])
        except (TypeError, ValueError):
            self._floor_learned = self._floor_learned_at = self._floor_learn_limit = None
        if self._floor_learned is not None:
            _LOGGER.warning(
                "Learned power floor %dW in effect (Power Min %dW) — the miner could not hold "
                "%sW before; press Reset Learned Floor to re-test lower limits",
                self._floor(), self._power_min, self._floor_learn_limit,
            )
        self._publish_floor()
        self._publish_shutoff(ds.Decision(ds.NONE, "restored", (), self._shutoff))
        self._unsub_coordinator = self.coordinator.async_add_listener(
            self._handle_coordinator_update
        )
        # DataUpdateCoordinator only notifies listeners when the data or the
        # success flag changes. A powered-off miner that stops answering would
        # therefore starve us of ticks and block the resume. This timer runs a
        # tick whenever the coordinator hasn't driven one for a full interval.
        interval = self.coordinator.update_interval or timedelta(seconds=30)
        self._tick_interval_s = interval.total_seconds()
        self._unsub_timer = async_track_time_interval(self.hass, self._handle_timer, interval)

    async def async_unload(self) -> None:
        if self._unsub_coordinator is not None:
            self._unsub_coordinator()
            self._unsub_coordinator = None
        if self._unsub_timer is not None:
            self._unsub_timer()
            self._unsub_timer = None
        task = self._step_task
        if task is not None and not task.done():
            # Let an in-flight miner command finish so the new controller
            # instance doesn't load a Store this one is about to overwrite.
            try:
                await asyncio.wait_for(asyncio.shield(task), 20)
            except (asyncio.TimeoutError, Exception) as err:  # noqa: BLE001
                _LOGGER.warning("Control step still running at unload: %s", err)
        await self._save()

    @callback
    def _handle_timer(self, _now: datetime) -> None:
        """Fallback tick when the coordinator has gone quiet (miner unreachable).

        Routed through the coordinator handler, not straight to the step: at
        startup the timer can fire before the coordinator's first callback,
        and a step before the bumpless-transfer seed runs the PID on Kp·error
        alone (2026-10-07: 4200 → 2437 W on a cold loop, one wasted restart).
        """
        if self._step_lock.locked():
            return
        if time() - self._last_tick_at < self._tick_interval_s * 0.9:
            return
        _LOGGER.debug("Timer-driven control tick (coordinator quiet)")
        self._handle_coordinator_update()

    def _store_data(self) -> dict[str, Any]:
        return {
            "lockout_latched": bool(self._pid_state.get("lockout_latched")),
            "target": self._pid_state.get("target"),
            "shutoff": self._shutoff.to_dict(),
            "outdoor_samples": [[ts, v] for ts, v in self._outdoor_samples],
            "freeze_active": self._freeze_active,
            "floor_learned": self._floor_learned,
            "floor_learned_at": self._floor_learned_at,
            "floor_learn_limit": self._floor_learn_limit,
            "floor_proven_ok": self._floor_proven_ok,
        }

    async def _save(self) -> None:
        try:
            await self._store.async_save(self._store_data())
        except Exception as err:  # never let persistence break control
            _LOGGER.error("Failed to persist controller state: %s", err)

    def _save_later(self) -> None:
        self._store.async_delay_save(self._store_data, 60)

    # ------------------------------------------------------------ user hooks

    async def async_user_mining_override(self, on: bool) -> None:
        """Mining Control was toggled by the user. Runs under the step lock."""
        async with self._step_lock:
            now = time()
            before = self._shutoff
            if on:
                self._shutoff = ds.user_mining_on(self._shutoff, now)
                if before.owned or before.state in (ds.DWELL, ds.STOPPED, ds.RESUMING):
                    _LOGGER.warning(
                        "Mining Control ON overrides the demand shutoff — stops are "
                        "suppressed until a thermostat calls for heat"
                    )
                self._resume_hold_until = now + RESUME_BOOT_HOLD_S
            else:
                self._shutoff = ds.user_mining_off(self._shutoff, now)
                self._user_off_at = now
                _LOGGER.warning(
                    "Mining Control OFF by user — the controller will not auto-resume"
                )
            self._mark_actuation()
            self._freeze_off_notified = False
            await self._save()
            self._publish_shutoff(ds.Decision(ds.NONE, "user override", (), self._shutoff))
            self.coordinator.async_update_listeners()

    async def async_reset_lockout(self) -> None:
        """Clear the supply lockout latch once the loop has cooled.

        Requires a live probe reading below the soft cap, so a reset can't be
        issued blind. Does not power the miner on — the operator does that
        with Mining Control afterwards.
        """
        if not self._pid_state.get("lockout_latched"):
            return
        temp = self._current_temperature()
        if temp is None:
            raise HomeAssistantError(
                "Cannot reset supply lockout: the supply temperature sensor is unavailable."
            )
        if temp >= self._supply_temp_safety_cap:
            raise HomeAssistantError(
                f"Cannot reset supply lockout: supply is {temp:.1f}°F, must be "
                f"below the {self._supply_temp_safety_cap:.1f}°F safety cap."
            )
        async with self._step_lock:
            self._pid_state["lockout_latched"] = False
            self._pid_state["safety_engaged"] = False
            self._shutoff = ds.lockout_reset(self._shutoff, time())
            await self._save()
        _LOGGER.warning("Supply lockout reset at %.1f°F — turn Mining Control on to resume", temp)
        self.coordinator.async_update_listeners()

    async def async_reset_learned_floor(self) -> None:
        """Forget the learned floor so lower limits are tried again.

        For after the hardware has been serviced (reseated ribbon, busbar).
        There is no automatic downward re-test: boot survival at a marginal
        limit is stochastic, so a timed re-probe would recreate the crash loop.
        """
        async with self._step_lock:
            had = self._floor_learned
            self._floor_learned = None
            self._floor_learned_at = None
            self._floor_learn_limit = None
            self._floor_proven_ok = None
            self._floor_short_runs = 0
            self._floor_short_run_limit = None
            self._floor_exhausted = False
            self._restart_loop_notified = False
            self._floor_chip_notified = False
            await self._save()
        for key in ("floor_raised", "floor_ceiling", "restart_loop", "restart_loop_chip_cap"):
            persistent_notification.async_dismiss(self.hass, f"{DOMAIN}_{key}")
        _LOGGER.warning(
            "Learned power floor reset (was %s) — the effective minimum is Power Min %dW again",
            f"{had}W" if had is not None else "not set", self._power_min,
        )
        self._publish_floor()
        self.coordinator.async_update_listeners()

    # --------------------------------------------------------- coordinator

    @callback
    def _handle_coordinator_update(self) -> None:
        """Detect mining transitions then run a control step."""
        is_mining = bool(self.coordinator.data.get("is_mining"))
        now = time()
        if self.coordinator.last_update_success:
            try:
                self._note_poll(is_mining, now)
            except Exception:  # bookkeeping must never block edge handling or the tick
                _LOGGER.exception("Restart bookkeeping failed")
        if self._last_is_mining is None and is_mining:
            # First poll after (re)load with the miner already hashing: seed so
            # the first tick doesn't slam the limit from Kp·error alone, and
            # hold actuation in case we restarted inside a boot window.
            seeded = self._seed_bumpless_transfer()
            try:
                uptime = float(self.coordinator.data.get("uptime") or 0)
            except (TypeError, ValueError):
                uptime = 0.0
            if uptime < RESUME_BOOT_HOLD_S:
                self._resume_hold_until = max(
                    self._resume_hold_until, time() + RESUME_BOOT_HOLD_S - uptime
                )
            _LOGGER.info("Controller started — seeded PID from current limit (≈%dW)", seeded)
        elif self._last_is_mining is not None and is_mining != self._last_is_mining:
            if not is_mining:
                # Miner just stopped (our shutoff, firmware cutback, manual,
                # network drop). Clear controller state so a resume doesn't
                # fire hours of stale integral and a dead throttle clock.
                self._pid.clear_samples()
                self._pid.integral = 0.0
                self._last_input_time = None
                self._ramped_target = None
                if self._recently_sent_limit() is None:
                    self._last_commanded_power = None
                    self._last_command_time = 0.0
                    _LOGGER.info("Mining stopped — PID controller state reset")
                else:
                    # The restart every adjust_power_limit causes. Zeroing the
                    # throttle clock here capped min_adjust_interval at the boot
                    # hold (2026-10-07: a 1800 s interval still actuated every
                    # ~630 s, cycling 2000 ↔ 4200 W).
                    _LOGGER.info("Mining stopped after our limit change — throttle clock kept")
            else:
                # Whatever started it (our resume, firmware auto-start, web UI),
                # it is booting now: no limit change for the boot hold. A start
                # we caused always arms it; an uncommanded start arms it only
                # while no short-run episode is open (the first spontaneous
                # restart gets a hold, the crash loop that may follow does
                # not), so the hold expires at most RESUME_BOOT_HOLD_S after
                # the first start of an episode however fast the miner loops.
                seeded = self._seed_bumpless_transfer()
                ours = self._claim_start(now)
                if ours or self._floor_short_runs == 0:
                    self._resume_hold_until = max(self._resume_hold_until, now + RESUME_BOOT_HOLD_S)
                _LOGGER.info(
                    "Mining resumed — re-seeded PID for bumpless transfer (≈%dW)%s", seeded,
                    "" if ours else " (restart not commanded by the controller)",
                )
        self._last_is_mining = is_mining
        if not self._step_lock.locked():
            self._step_task = self.hass.async_create_task(self._run_control_step())

    async def _run_control_step(self) -> None:
        async with self._step_lock:
            self._last_tick_at = time()
            try:
                await self._control_step()
            except Exception:  # defensive — never break the coordinator loop
                _LOGGER.exception("Whatsminer control step failed")
            # Entities rendered pid_state before this tick updated it.
            self.coordinator.async_update_listeners()

    # ------------------------------------------------------------- the tick

    async def _control_step(self) -> None:
        now = time()
        temp = self._current_temperature()
        fresh = bool(self.coordinator.last_update_success)
        # coordinator.data keeps the last successful poll, so when not fresh
        # this is the last known value; decide() treats it accordingly.
        is_mining = bool(self.coordinator.data.get("is_mining"))
        if is_mining and now - self._user_off_at < USER_OFF_GRACE_S:
            is_mining = False  # user just powered it off; the 1 m hashrate lingers
        if self._pid_state.get("target") != self._saved_target:
            self._saved_target = self._pid_state.get("target")
            self._save_later()
        latched = bool(self._pid_state.get("lockout_latched"))

        forecast = await self._hourly_forecast(now)
        freeze = self._update_freeze(now, forecast)
        gate_mean = self._update_outdoor_mean(now, forecast)
        summary, calling, unknown, demand_index = self._demand_snapshot(now)
        self._pid_state["demand_index"] = demand_index

        # --- supply lockout -----------------------------------------------
        if not latched and temp is not None and temp >= self._supply_temp_lockout:
            if freeze is True:
                if not self._freeze_hold_over_lockout:
                    self._freeze_hold_over_lockout = True
                    _LOGGER.critical(
                        "Supply %.1f°F ≥ lockout %.1f°F but the freeze guard is active — "
                        "holding %dW instead of stopping mining. Check the loop now.",
                        temp, self._supply_temp_lockout, self._power_min,
                    )
                    self._notify(
                        "freeze_over_lockout",
                        "Supply overheat while freeze guard active",
                        f"Supply is {temp:.1f}°F (lockout {self._supply_temp_lockout:.0f}°F) but "
                        f"the outdoor coolant loop is at freeze risk, so the miner was kept "
                        f"running at {self._power_min} W. Check the heating loop.",
                    )
            else:
                await self._trip_lockout(temp)
                latched = True
        elif temp is not None and temp < self._supply_temp_lockout - 5.0:
            if self._freeze_hold_over_lockout:
                self._freeze_hold_over_lockout = False
                persistent_notification.async_dismiss(self.hass, f"{DOMAIN}_freeze_over_lockout")

        # --- demand shutoff state machine ---------------------------------
        inputs = ds.ShutoffInputs(
            summary=summary,
            calling=tuple(calling),
            unknown=tuple(unknown),
            gate_mean=gate_mean,
            supply=temp,
            target=self._target(),
            is_mining=is_mining,
            fresh=fresh,
            latched=latched,
            freeze=freeze,
            user_off=now - self._user_off_at < USER_OFF_GRACE_S,
        )
        decision = ds.decide(self._shutoff_cfg, self._shutoff, inputs, now)
        await self._apply_decision(decision, now)
        state = self._shutoff
        # UNLATCH clears the latch inside _apply_decision; re-read it.
        latched = bool(self._pid_state.get("lockout_latched"))

        if latched:
            self._pid_state["safety_engaged"] = True
            self._pid_state["control_mode"] = "latched"
            if is_mining and fresh:
                await self._reassert_lockout()
            return

        if state.state in (ds.STOPPED, ds.RESUMING) and not state.simulated:
            # Stays engaged through a stop so the sensor doesn't toggle.
            self._pid_state["safety_engaged"] = True
            self._pid_state["control_mode"] = "stopped" if state.state == ds.STOPPED else "resuming"
            self._null_pid_internals()
            return

        if not is_mining:
            self._pid_state["control_mode"] = "idle"
            self._pid_state["safety_engaged"] = False
            self._null_pid_internals()
            if fresh and self._floor_learned is not None and now - self._user_off_at >= USER_OFF_GRACE_S:
                await self._enforce_floor_while_booting(temp, now)
            if freeze is True and fresh and not self._freeze_off_notified:
                self._freeze_off_notified = True
                _LOGGER.warning(
                    "Miner is off (not by the demand shutoff) while the freeze guard is "
                    "active — the outdoor coolant loop may freeze"
                )
                self._notify(
                    "freeze_miner_off",
                    "Miner off during freeze risk",
                    "The miner is not mining and the freeze guard is active. It was not "
                    "stopped by the demand shutoff, so it will not be resumed "
                    "automatically. Turn Mining Control on if the loop is at risk.",
                )
            return
        if is_mining and self._freeze_off_notified:
            self._freeze_off_notified = False
            persistent_notification.async_dismiss(self.hass, f"{DOMAIN}_freeze_miner_off")

        if not fresh:
            return  # don't actuate on a stale poll

        caps = self._evaluate_safety_caps(temp)
        if self._freeze_hold_over_lockout:
            caps = caps | {"lockout"}
        await self._run_pid_step(temp, caps, summary, state)

    async def _enforce_floor_while_booting(self, temp: float | None, now: float) -> None:
        """Send the floor to a miner that is up at an unholdable limit but not hashing.

        A boot below the hardware floor can die before the short-window
        hashrate ever turns non-zero, so is_mining never flips and the hashing
        tick that normally enforces the floor never comes; the loop would run
        forever with the floor learned but inert. btminer being up (Elapsed >
        0) at a known limit is enough to act on.
        """
        try:
            uptime = float(self.coordinator.data.get("uptime") or 0)
            limit = int(self.coordinator.data.get("wattage_limit") or 0)
        except (TypeError, ValueError):
            return
        if uptime <= 0:
            return
        believed = self._reference(limit)
        caps = self._evaluate_safety_caps(temp)
        if self._freeze_hold_over_lockout:
            caps = caps | {"lockout"}
        if not self._floor_fire(believed, now, caps):
            return
        _LOGGER.warning(
            "Miner is up at %dW (Elapsed %.0fs) but not hashing, below the learned floor %dW — "
            "commanding the floor", believed, uptime, self._floor(),
        )
        await self._set_power_limit(self._floor(), True)

    # ------------------------------------------------------ shutoff plumbing

    async def _apply_decision(self, decision: ds.Decision, now: float) -> None:
        prev = self._shutoff
        new = decision.state
        changed = prev.to_dict() != new.to_dict()
        transition = prev.state != new.state or prev.owned != new.owned
        if transition or prev.gate_armed != new.gate_armed or prev.suppressed != new.suppressed:
            _LOGGER.info(
                "Demand shutoff: %s → %s (%s)%s",
                prev.state, new.state, decision.reason,
                f" blocking: {', '.join(decision.blocking)}" if decision.blocking else "",
            )
            if prev.gate_armed != new.gate_armed:
                _LOGGER.info("Demand shutoff warm gate %s", "armed" if new.gate_armed else "disarmed")
        self._shutoff = new
        if new.would_stop_at != prev.would_stop_at and new.would_stop_at is not None:
            _LOGGER.warning("Demand shutoff (observe): would stop the miner now — %s", decision.reason)
        if new.would_resume_at != prev.would_resume_at and new.would_resume_at is not None:
            _LOGGER.warning("Demand shutoff (observe): would resume the miner now — %s", decision.reason)

        action = decision.action
        if action == ds.STOP:
            # Persist ownership before the command so a crash can't orphan a stop.
            await self._save()
            _LOGGER.warning("Demand shutoff: powering the miner off — %s", decision.reason)
            try:
                await self.coordinator.api.power_off()
                self._mark_actuation()
                self._last_commanded_power = None
                self._last_command_time = time()
                self._max_off_notified = False
            except Exception as err:
                _LOGGER.error("Demand shutoff: power_off failed, will retry: %s", err)
                self._shutoff = ds.mark_stop_failed(self._shutoff, time())
                await self._save()
        elif action in (ds.RESUME, ds.RELEASE, ds.UNLATCH):
            if action == ds.UNLATCH:
                self._pid_state["lockout_latched"] = False
                self._pid_state["safety_engaged"] = False
                _LOGGER.critical(
                    "Freeze guard cleared the supply lockout and is restarting the miner "
                    "to protect the outdoor coolant loop"
                )
                self._notify(
                    "freeze_unlatch",
                    "Freeze guard cleared supply lockout",
                    "The supply lockout was cleared automatically because the outdoor "
                    "coolant loop is at freeze risk. The miner is being restarted.",
                )
            if self._pid_state.get("lockout_latched"):
                _LOGGER.error("Refusing to power on while the supply lockout is latched")
            else:
                level = _LOGGER.warning if "fail-warm" in decision.reason or "freeze" in decision.reason else _LOGGER.info
                level("Demand shutoff: powering the miner on — %s", decision.reason)
                await self._save()
                try:
                    await self.coordinator.api.power_on()
                    self._mark_actuation()
                    self._resume_hold_until = time() + RESUME_BOOT_HOLD_S
                    self._last_commanded_power = None
                    self._last_command_time = time()
                except Exception as err:
                    _LOGGER.error("Demand shutoff: power_on failed, retrying in %ds: %s", POWER_ON_RETRY_S, err)
                    # Pull the verify clock back so the RESUMING/RELEASE retry
                    # fires soon instead of after the full verify window.
                    self._shutoff = replace(
                        self._shutoff,
                        resume_sent_at=time() - max(ds.RESUME_VERIFY_S, ds.RELEASE_RETRY_S) + POWER_ON_RETRY_S,
                    )
                    await self._save()
            if decision.notify == "resume_failed":
                self._notify(
                    "resume_failed",
                    "Miner failed to resume",
                    f"The demand shutoff has sent power_on {self._shutoff.resume_attempts} "
                    "times without the miner hashing. Check the miner.",
                )
        elif action == ds.REASSERT:
            _LOGGER.warning("Demand shutoff: miner started while we own a stop — reasserting power_off")
            try:
                await self.coordinator.api.power_off()
                self._mark_actuation()
                self._last_command_time = time()
            except Exception as err:
                _LOGGER.error("Demand shutoff: reassert power_off failed: %s", err)
        elif action == ds.ADOPT:
            _LOGGER.warning(
                "Demand shutoff: miner kept running after reassert — adopting the external "
                "start and suppressing stops until a thermostat calls"
            )
        if decision.notify == "adopted":
            self._notify(
                "adopted",
                "Miner started outside the demand shutoff",
                "The miner kept hashing after three power_off attempts, so the demand "
                "shutoff released its stop and will not stop it again until a thermostat "
                "calls for heat. Check the miner's web UI and power history.",
            )
        if decision.notify == "max_off" and not self._max_off_notified:
            self._max_off_notified = True
            self._notify(
                "max_off",
                "Miner stopped for over 24 hours",
                "The demand shutoff has kept the miner off for more than 24 hours. "
                "No automatic action is taken; check thermostats and the outdoor mean.",
            )
        if changed:
            if transition or action != ds.NONE:
                await self._save()
            else:
                self._save_later()
        self._publish_shutoff(decision)

    def _publish_shutoff(self, decision: ds.Decision) -> None:
        st = decision.state
        cfg = self._shutoff_cfg
        if not cfg.active:
            visible = "disabled"
        elif st.suppressed and st.state == ds.RUNNING:
            visible = "suppressed"
        else:
            visible = st.state
        self._pid_state["demand_shutoff"] = {
            "state": visible,
            "mode": cfg.mode,
            "active": bool(st.owned),
            "reason": decision.reason,
            "blocking": list(decision.blocking),
            "since": _iso(st.since),
            "gate": ("armed" if st.gate_armed else "disarmed") if cfg.active else "unknown",
            "would_stop": _iso(st.would_stop_at),
            "would_resume": _iso(st.would_resume_at),
            "trigger": st.dwell_trigger,
        }

    def _notify(self, key: str, title: str, message: str) -> None:
        persistent_notification.async_create(
            self.hass, message, title=f"{self.coordinator.name}: {title}",
            notification_id=f"{DOMAIN}_{key}",
        )

    # -------------------------------------------------------------- lockout

    async def _trip_lockout(self, temp: float) -> None:
        self._pid_state["lockout_latched"] = True
        self._pid_state["safety_engaged"] = True
        _LOGGER.critical(
            "Supply temp %.1f°F ≥ lockout %.1f°F — stopping mining (latched until "
            "Reset Supply Lockout is pressed)",
            temp, self._supply_temp_lockout,
        )
        await self._save()
        await self._send_lockout_power_off()

    async def _reassert_lockout(self) -> None:
        if time() - self._last_lockout_power_off < LOCKOUT_REASSERT_INTERVAL:
            return
        _LOGGER.warning("Miner is mining while supply lockout is latched — stopping it")
        await self._send_lockout_power_off()

    async def _send_lockout_power_off(self) -> None:
        self._last_lockout_power_off = time()
        try:
            await self.coordinator.api.power_off()
            self._mark_actuation()
            self._last_commanded_power = None
            self._last_command_time = time()
        except Exception as err:
            _LOGGER.error("Failed to stop mining on supply-temp lockout: %s", err)

    # ------------------------------------------------------------ safety caps

    def _evaluate_safety_caps(self, temp: float | None) -> frozenset[str]:
        """Return the engaged soft caps, logging only when the set changes.

        Chip-temp guards the *miner*; the supply cap guards the *plant* (a
        stagnant loop can trip the boiler's own high-limit even at power_min).
        The supply cap forces the effective floor (power_min, or the learned
        floor when the miner has proven it cannot hold power_min); the chip
        cap forces power_min regardless (see HARD_CAPS).
        """
        caps: set[str] = set()
        chip = self._chip_temp()
        if chip is not None and chip >= self._chip_temp_safety_cap:
            caps.add("chip")
        if temp is not None and temp >= self._supply_temp_safety_cap:
            caps.add("supply")
        active = frozenset(caps)
        if active != self._caps_active:
            if active - self._caps_active:
                _LOGGER.warning(
                    "Safety cap engaged (chip %s°F / cap %.1f°F, supply %s°F / cap "
                    "%.1f°F) — forcing %dW%s",
                    f"{chip:.1f}" if chip is not None else "?",
                    self._chip_temp_safety_cap,
                    f"{temp:.1f}" if temp is not None else "?",
                    self._supply_temp_safety_cap,
                    self._cap_clamp(active),
                    f" (Power Min {self._power_min}W is unholdable)"
                    if self._cap_clamp(active) > self._power_min else "",
                )
            elif not active:
                _LOGGER.info("Safety caps cleared")
            self._caps_active = active
        return active

    def _chip_temp(self) -> float | None:
        temp = self.coordinator.data.get("temperature_avg")
        try:
            value = float(temp)
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None

    # --------------------------------------------------------------- readers

    def _read_temp_entity(self, entity_id: str | None, what: str, flag: str) -> float | None:
        """Read a temperature sensor entity in °F; log unavailability once."""
        if entity_id is None:
            return None
        state = self.hass.states.get(entity_id)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN, None):
            if not getattr(self, flag):
                _LOGGER.warning("%s %s unavailable", what, entity_id)
                setattr(self, flag, True)
            return None
        try:
            value = float(state.state)
        except (TypeError, ValueError):
            _LOGGER.warning("%s %s returned non-numeric state %r", what, entity_id, state.state)
            return None
        if getattr(self, flag):
            _LOGGER.info("%s %s is back", what, entity_id)
            setattr(self, flag, False)
        unit = state.attributes.get("unit_of_measurement")
        if unit and unit != UnitOfTemperature.FAHRENHEIT:
            try:
                value = TemperatureConverter.convert(value, unit, UnitOfTemperature.FAHRENHEIT)
            except Exception as err:
                _LOGGER.warning("Could not convert %s from %s to °F: %s", entity_id, unit, err)
                return None
        return value

    def _current_temperature(self) -> float | None:
        """The regulated variable: the supply probe, in °F."""
        return self._read_temp_entity(
            self._external_sensor_id, "Supply probe", "_external_unavail_logged"
        )

    def _read_weather_temp_fahrenheit(self) -> float | None:
        if self._weather_entity_id is None:
            return None
        state = self.hass.states.get(self._weather_entity_id)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN, None):
            return None
        try:
            value = float(state.attributes.get("temperature"))
        except (TypeError, ValueError):
            return None
        unit = state.attributes.get("temperature_unit") or self.hass.config.units.temperature_unit
        if unit != UnitOfTemperature.FAHRENHEIT:
            try:
                value = TemperatureConverter.convert(value, unit, UnitOfTemperature.FAHRENHEIT)
            except Exception:
                return None
        return value

    def _read_outdoor_fahrenheit(self) -> float | None:
        """Current outdoor temp: dedicated sensor, else the weather entity."""
        value = self._read_temp_entity(
            self._outdoor_temp_sensor_id, "Outdoor temp sensor", "_outdoor_unavail_logged"
        )
        if value is None:
            value = self._read_weather_temp_fahrenheit()
        return value

    def _read_numeric_sensor(self, entity_id: str | None, what: str, flag: str) -> float | None:
        if entity_id is None:
            return None
        state = self.hass.states.get(entity_id)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN, None):
            if not getattr(self, flag):
                _LOGGER.warning("%s %s unavailable — envelope disabled", what, entity_id)
                setattr(self, flag, True)
            return None
        try:
            value = float(state.state)
        except (TypeError, ValueError):
            _LOGGER.warning("%s %s returned non-numeric state %r", what, entity_id, state.state)
            return None
        setattr(self, flag, False)
        return value

    def _target(self) -> float:
        t = self._pid_state.get("target")
        return float(t) if t is not None else self._default_target

    # -------------------------------------------------------------- forecast

    async def _hourly_forecast(self, now: float) -> list[tuple[float, float]]:
        """Hourly forecast as (unix_ts, °F), cached. Empty without a weather entity."""
        if self._weather_entity_id is None:
            return []
        if (
            self._forecast_cache_time is not None
            and now - self._forecast_cache_time < FORECAST_CACHE_S
        ):
            return self._forecast_cache
        try:
            forecasts = await self.hass.services.async_call(
                "weather",
                "get_forecasts",
                {"entity_id": self._weather_entity_id, "type": "hourly"},
                blocking=True,
                return_response=True,
            )
        except Exception as err:
            _LOGGER.debug("Could not get forecast from %s: %s", self._weather_entity_id, err)
            if self._forecast_cache_time is not None and now - self._forecast_cache_time > FORECAST_STALE_MAX_S:
                self._forecast_cache = []
            return self._forecast_cache  # stale but better than nothing
        out: list[tuple[float, float]] = []
        # Forecast temperatures come in the entity's unit (which can be
        # overridden per entity), not necessarily HA's system unit.
        wstate = self.hass.states.get(self._weather_entity_id)
        unit = (
            (wstate.attributes.get("temperature_unit") if wstate is not None else None)
            or self.hass.config.units.temperature_unit
        )
        for entry in (forecasts or {}).get(self._weather_entity_id, {}).get("forecast", []) or []:
            ts = entry.get("datetime")
            if isinstance(ts, str):
                try:
                    ts = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
                except ValueError:
                    continue
            elif isinstance(ts, datetime):
                ts = ts.timestamp()
            else:
                continue
            temp = entry.get("temperature")
            if temp is None:
                continue
            try:
                temp = float(temp)
            except (TypeError, ValueError):
                continue
            if unit != UnitOfTemperature.FAHRENHEIT:
                temp = TemperatureConverter.convert(temp, unit, UnitOfTemperature.FAHRENHEIT)
            out.append((float(ts), temp))
        if out:
            self._forecast_cache = out
            self._forecast_cache_time = now
        return self._forecast_cache

    def _blended_outdoor_temp(self, now: float, forecast: list[tuple[float, float]]) -> float | None:
        """Outdoor temp for the Ke feedforward, blended with the forecast at lookahead."""
        outdoor = self._read_outdoor_fahrenheit()
        if outdoor is None:
            return None
        if not forecast or self._forecast_lookahead_min <= 0 or self._forecast_blend <= 0:
            return outdoor
        target_time = now + self._forecast_lookahead_min * 60
        ahead = next((v for ts, v in forecast if ts >= target_time), None)
        if ahead is None:
            return outdoor
        if self._forecast_blend >= 1:
            return ahead
        return (1.0 - self._forecast_blend) * outdoor + self._forecast_blend * ahead

    # ------------------------------------------------------ gate and freeze

    def _update_outdoor_mean(self, now: float, forecast: list[tuple[float, float]]) -> float | None:
        outdoor = self._read_outdoor_fahrenheit()
        if outdoor is not None:
            if not self._outdoor_samples or now - self._outdoor_samples[-1][0] >= OUTDOOR_SAMPLE_INTERVAL_S:
                self._outdoor_samples.append((now, outdoor))
                self._outdoor_samples = ds.trim_samples(self._outdoor_samples, now)
                self._save_later()
        mean = ds.centred_mean(self._outdoor_samples, forecast, now)
        self._pid_state["outdoor_mean"] = round(mean, 2) if mean is not None else None
        return mean

    def _update_freeze(self, now: float, forecast: list[tuple[float, float]]) -> bool | None:
        source: str | None = None
        value = self._read_temp_entity(
            self._freeze_sensor_id, "Freeze guard sensor", "_freeze_unavail_logged"
        )
        if value is not None:
            source = "sensor"
        else:
            horizon = now + self._freeze_forecast_hours * 3600
            ahead = [v for ts, v in forecast if now < ts <= horizon]
            value = ds.freeze_fallback_value(self._read_outdoor_fahrenheit(), ahead)
            if value is not None:
                source = "weather"
        status = ds.freeze_status(
            value, self._freeze_threshold, FREEZE_GUARD_RELEASE_HYSTERESIS, self._freeze_active
        )
        if status is None:
            if not self._freeze_unknown_logged:
                _LOGGER.warning("Freeze guard has no source (no sensor, outdoor or forecast reading)")
                self._freeze_unknown_logged = True
        else:
            self._freeze_unknown_logged = False
            if status != self._freeze_active:
                _LOGGER.warning(
                    "Freeze guard %s (%s %.1f°F, threshold %.1f°F)",
                    "ACTIVE — stops blocked, stopped miner will be resumed" if status else "released",
                    source or "no source", value if value is not None else float("nan"),
                    self._freeze_threshold,
                )
                self._freeze_active = status
                self._save_later()
        self._publish_freeze(status, source, value)
        return status

    def _publish_freeze(self, status: bool | None, source: str | None, value: float | None) -> None:
        self._pid_state["freeze_guard"] = {
            "active": bool(status),
            "status": "unknown" if status is None else ("active" if status else "clear"),
            "source": source,
            "value": round(value, 1) if value is not None else None,
            "threshold": self._freeze_threshold,
            "sensor": self._freeze_sensor_id,
        }

    # ------------------------------------------------------- demand snapshot

    def _demand_snapshot(self, now: float) -> tuple[str, list[str], list[str], float | None]:
        """Strict thermostat classification. Returns (summary, calling, unknown, index)."""
        if not self._demand_entities:
            return ds.UNKNOWN, [], [], None
        classes: dict[str, str] = {}
        calling: list[str] = []
        unknown: list[str] = []
        for eid in self._demand_entities:
            state = self.hass.states.get(eid)
            if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
                cls = ds.UNKNOWN
            else:
                attrs = state.attributes
                reported = getattr(state, "last_reported", None) or state.last_updated
                age = now - reported.timestamp() if reported is not None else None
                cls = ds.classify_thermostat(
                    attrs.get("hvac_action"),
                    state.state,  # hvac_mode is the climate entity's state
                    attrs.get("current_temperature"),
                    attrs.get("temperature"),
                    age,
                    self._cold_room_delta,
                )
            classes[eid] = cls
            if cls == ds.CALLING:
                calling.append(eid)
            elif cls == ds.UNKNOWN:
                unknown.append(eid)
        summary = ds.summarize(classes)
        known = [c for c in classes.values() if c != ds.UNKNOWN]
        if not known:
            if not self._demand_unavail_logged:
                _LOGGER.warning(
                    "All demand entities (%s) are unavailable or stale — ignoring demand "
                    "until one reports", ", ".join(self._demand_entities),
                )
                self._demand_unavail_logged = True
            index = None
        else:
            if self._demand_unavail_logged:
                _LOGGER.info("Demand entities reporting again")
                self._demand_unavail_logged = False
            index = sum(1.0 for c in known if c == ds.CALLING) / len(known)
        return summary, calling, unknown, index

    # ------------------------------------------------------------------- PID

    def _null_pid_internals(self) -> None:
        self._pid_state.update(
            {
                "error": None, "proportional": None, "integral": None,
                "derivative": None, "external": None, "output": None,
                "requested_output": None,
            }
        )

    def _seed_bumpless_transfer(self) -> int:
        """Seed the PID integral so the first tick output ≈ current miner wattage."""
        current_limit = self.coordinator.data.get("wattage_limit") or 0
        known = current_limit > 0
        sent = self._recently_sent_limit()
        if sent is not None:
            # The summary lags a limit change by a poll or two (and reads 0
            # mid-reboot); what we just sent is the better estimate.
            current_limit, known = sent, True
        if not known:
            # Unknown limit: seed the integrator from power_max (fail-warm) but
            # leave the actuation reference unknown so the first real command
            # is not suppressed by a fabricated "already there".
            current_limit = self._last_commanded_power or self._power_max
        current_temp = self._current_temperature()
        target = self._target()
        first_tick_p = self._kp * (target - float(current_temp)) if current_temp is not None else 0.0
        self._pid.integral = float(current_limit) - first_tick_p
        self._last_commanded_power = int(current_limit) if known else None
        return int(current_limit)

    def _fallback_power(self, summary: str) -> tuple[int | None, str]:
        """Open-loop power for when the supply probe is unavailable."""
        if summary == ds.ALL_IDLE:
            return self._floor(), "no thermostat demand"
        outdoor = self._read_outdoor_fahrenheit()
        if outdoor is None or self._fallback_outdoor_warm <= self._fallback_outdoor_cold:
            return None, "no outdoor temperature"
        frac = (self._fallback_outdoor_warm - outdoor) / (
            self._fallback_outdoor_warm - self._fallback_outdoor_cold
        )
        frac = max(0.0, min(1.0, frac))
        lo, hi = float(self._floor()), float(self._power_max)
        return int(round(lo + (hi - lo) * frac)), f"outdoor {outdoor:.1f}°F"

    async def _run_fallback_step(self, caps: frozenset[str], summary: str, now: float) -> None:
        """Supply probe unavailable: run open-loop on outdoor temp and demand."""
        if not self._in_fallback:
            self._in_fallback = True
            _LOGGER.warning(
                "Supply probe %s unavailable — running on outdoor temperature and "
                "thermostat demand until it returns", self._external_sensor_id,
            )
        target, basis = self._fallback_power(summary)
        current_limit = self.coordinator.data.get("wattage_limit") or 0
        reference = self._reference(current_limit)
        if target is None and reference is None:
            self._pid_state["control_mode"] = "fallback"
            return  # nothing to go on and no known limit: hold whatever it is
        requested = target if target is not None else reference
        new_power = self._cap_clamp(caps) if caps else max(requested, self._floor())
        demand_lockout = basis == "no thermostat demand"
        self._null_pid_internals()
        self._pid_state.update(
            {
                "requested_output": requested,
                "output": reference,
                "safety_engaged": bool(caps) or demand_lockout,
                "out_max_effective": self._power_max,
                "out_min_effective": self._floor(),
            }
        )
        self._pid_state["control_mode"] = (
            "safety_cap" if caps else ("demand_lockout" if demand_lockout else "fallback")
        )
        floor_fire = self._floor_fire(reference, now, caps)
        if floor_fire:
            new_power = self._floor()  # enforce exactly the floor, never a curve step through the hold
        if reference is None:
            step_ok, interval_ok = True, True
        else:
            delta = abs(new_power - reference)
            interval = self._min_adjust_interval_increase if new_power > reference else self._min_adjust_interval
            step_ok = delta >= self._min_power_step
            interval_ok = now - self._last_command_time >= interval
        if not floor_fire and (not step_ok or not (interval_ok or caps)):
            return
        if now < self._resume_hold_until and not caps and not floor_fire:
            _LOGGER.debug("Fallback actuation held: miner booting after resume")
            return
        _LOGGER.info(
            "Fallback (%s): power %dW (was %s)", "safety cap" if caps else basis, new_power,
            f"{reference}W" if reference is not None else "unknown",
        )
        await self._set_power_limit(new_power, floor_fire)

    def _reference(self, current_limit: int) -> int | None:
        """What we believe the miner's limit is, or None when unknown."""
        if self._last_commanded_power is not None:
            return self._last_commanded_power
        sent = self._recently_sent_limit()
        if sent is not None:
            return sent
        return int(current_limit) if current_limit and current_limit > 0 else None

    async def _set_power_limit(self, new_power: int, floor_fire: bool = False) -> None:
        try:
            await self.coordinator.api.set_power_limit(new_power)
            self._mark_actuation()
            self._sent_limit = new_power
            self._sent_limit_at = time()
            self._run_limit = new_power
            self._last_commanded_power = new_power
            self._last_command_time = time()
            self._pid_state["output"] = new_power
        except Exception as err:
            _LOGGER.error("Failed to set power limit to %dW: %s", new_power, err)
            return
        if floor_fire:
            # The raise was only scheduled for persistence (callback context);
            # make sure an HA crash right after this restart can't lose it.
            await self._save()

    # --------------------------------------------------------- learned floor

    def _floor(self) -> int:
        """The lowest limit we will command: power_min, or the learned floor."""
        floor = max(self._power_min, self._floor_learned or 0)
        return max(self._power_min, min(floor, self._power_max - self._min_power_step))

    def _floor_ceiling(self) -> int:
        """The highest floor learning may reach; restarts above it are not floor evidence."""
        return min(self._power_min + FLOOR_MAX_RAISE_W, self._power_max - self._min_power_step)

    def _cap_clamp(self, caps: frozenset[str]) -> int:
        """What an engaged cap forces: power_min for HARD_CAPS, else the floor."""
        return self._power_min if caps & HARD_CAPS else self._floor()

    def _mark_actuation(self) -> None:
        """We just sent a command that stops or restarts btminer."""
        now = time()
        self._last_actuation_at = now
        self._pending_restart_at = now

    def _pending_restart_is_ours(self, now: float) -> bool:
        pending = self._pending_restart_at
        return pending is not None and now - pending < RESUME_BOOT_HOLD_S

    def _claim_start(self, now: float) -> bool:
        """At a mining start edge: did one of our own commands cause this boot?

        Normally the Elapsed regression that preceded the start settled it
        (``_last_restart_ours``). A regression seen but not yet confirmed
        carries its own attribution. With no regression at all (power_on from
        off, a one-poll down phase the regression check missed) a recent
        command of ours claims the start and is consumed by it.
        """
        ours = self._last_restart_ours
        self._last_restart_ours = False
        if self._pending_regression is not None:
            return ours or self._pending_regression.ours
        if not ours and self._pending_restart_is_ours(now):
            self._pending_restart_at = None
            ours = True
        return ours

    def _recently_sent_limit(self) -> int | None:
        if self._sent_limit is not None and time() - self._sent_limit_at < RESUME_BOOT_HOLD_S:
            return self._sent_limit
        return None

    def _floor_fire(self, believed: int | None, now: float, caps: frozenset[str] = frozenset()) -> bool:
        """True when the miner is believed to sit below a learned floor.

        Like a safety cap this bypasses the interval and boot-hold gates: a
        miner below a floor it has proven it cannot hold is about to restart
        anyway, so the restart our command causes costs nothing. Inert while
        a HARD_CAP holds the miner at power_min, once learning is exhausted,
        and when no floor has been learned (a limit merely below the
        configured power_min waits for the normal gates).
        """
        if self._floor_learned is None or self._floor_exhausted or caps & HARD_CAPS:
            return False
        if believed is None or believed >= self._floor():
            return False
        if now - self._last_floor_fire_at < FLOOR_FIRE_RETRY_S:
            return False
        self._last_floor_fire_at = now
        _LOGGER.info(
            "Floor enforcement: believed limit %dW is below the learned floor %dW — commanding the floor",
            believed, self._floor(),
        )
        return True

    def _note_poll(self, is_mining: bool, now: float) -> None:
        """Track uptime and the limit in force; count uncommanded restarts.

        A restart is an Elapsed regression confirmed by the following poll
        (Elapsed still below the pre-regression value): a single garbled
        summary parses to Elapsed 0 and must not count. Hashrate blips (pool
        outage) do not reset Elapsed and so are never counted. The first
        regression after one of our own commands is ours.
        """
        try:
            uptime = float(self.coordinator.data.get("uptime") or 0)
        except (TypeError, ValueError):
            uptime = 0.0
        try:
            limit = int(self.coordinator.data.get("wattage_limit") or 0)
        except (TypeError, ValueError):
            limit = 0
        regression = self._pending_regression
        if regression is not None:
            self._pending_regression = None
            if uptime + 5 < regression.prev_uptime:
                self._count_restart(regression, now)
            else:
                _LOGGER.debug(
                    "Ignoring a one-poll Elapsed glitch (%.0fs → %.0fs → %.0fs)",
                    regression.prev_uptime, regression.seen_uptime, uptime,
                )
        elif self._last_uptime is not None and uptime + 5 < self._last_uptime:
            self._pending_regression = _Regression(
                prev_uptime=self._last_uptime,
                seen_uptime=uptime,
                limit=self._run_limit,
                ours=self._pending_restart_is_ours(now),
            )
        self._last_uptime = uptime
        sent = self._recently_sent_limit()
        if sent is not None:
            self._run_limit = sent
        elif limit > 0:
            self._run_limit = limit
        if is_mining and uptime >= FLOOR_STABLE_S and self._run_limit:
            # This run held its limit: the episode (if any) is over.
            changed = self._floor_short_runs or self._floor_exhausted or self._restart_loop_notified
            self._floor_short_runs = 0
            self._floor_short_run_limit = None
            self._floor_exhausted = False
            self._restart_loop_notified = False
            self._floor_chip_notified = False
            # A limit we sent during this run has not been held for a second
            # yet (its restart is still to come): credit only a limit in force
            # since the run began.
            if sent is None and (self._floor_proven_ok is None or self._run_limit < self._floor_proven_ok):
                self._floor_proven_ok = self._run_limit
                changed = True
            if changed:
                self._save_later()
        self._publish_floor()

    def _count_restart(self, regression: _Regression, now: float) -> None:
        """A confirmed Elapsed regression: attribute it, and count it if it ended a short run."""
        if regression.ours:
            self._pending_restart_at = None
        self._last_restart_ours = regression.ours
        if regression.ours or regression.prev_uptime >= FLOOR_STABLE_S or not regression.limit:
            return
        limit = regression.limit
        if (
            self._floor_short_run_limit is not None
            and abs(limit - self._floor_short_run_limit) > FLOOR_RAISE_STEP
        ):
            # A different limit: evidence about the old one says nothing about
            # this one (a PSU fault at 3300 W must not seed learning at a 1000 W
            # clamp). Consecutive floor raises differ by exactly one step.
            self._floor_short_runs = 0
        self._floor_short_run_limit = limit
        self._floor_short_runs += 1
        quiet = self._floor_exhausted or self._restart_loop_notified or "chip" in self._caps_active
        (_LOGGER.info if quiet else _LOGGER.warning)(
            "Miner restarted on its own after hashing %.0fs at %dW (%d short run%s in a row)",
            regression.prev_uptime, limit, self._floor_short_runs,
            "" if self._floor_short_runs == 1 else "s",
        )
        self._consider_floor_raise(limit, now)

    def _consider_floor_raise(self, limit: int, now: float) -> None:
        if FLOOR_RAISE_STEP <= 0:
            return
        if "chip" in self._caps_active:
            # Restarts under chip over-temperature must not be answered with
            # more power. Nothing breaks this loop: the clamp stays at
            # power_min and floor enforcement is inert while the cap holds, so
            # it persists until the chips cool. Tell the operator once.
            if not self._floor_chip_notified:
                self._floor_chip_notified = True
                _LOGGER.warning(
                    "Miner restarting at %dW while the chip-temp cap is active — not raising the floor",
                    limit,
                )
                self._notify(
                    "restart_loop_chip_cap",
                    "Miner restarting under the chip-temp cap",
                    f"The miner keeps restarting at {limit} W while its chips are over the "
                    f"{self._chip_temp_safety_cap:.0f}°F cap. The controller will not raise the "
                    "power floor under over-temperature; check coolant flow to the miner.",
                )
            return
        if self._floor_short_runs < FLOOR_SHORT_RUNS_TO_LEARN:
            return
        ceiling = self._floor_ceiling()
        if limit > ceiling:
            # A loop above anything the floor could reach is a PSU/pool/thermal
            # problem, not floor evidence.
            if not self._restart_loop_notified:
                self._restart_loop_notified = True
                _LOGGER.error(
                    "Miner is restarting repeatedly at %dW (%d short runs; floor %dW) — check the miner",
                    limit, self._floor_short_runs, self._floor(),
                )
                self._notify(
                    "restart_loop",
                    "Miner restarting repeatedly",
                    f"The miner restarted {self._floor_short_runs} times in a row at a {limit} W "
                    f"limit without a command from the controller. This is above the {ceiling} W "
                    "the power floor could ever reach, so it is not treated as a power-floor "
                    "problem. Check the miner's error codes, PSU and pool.",
                )
            return
        new = limit + FLOOR_RAISE_STEP
        if new <= self._floor():
            return  # already enforcing a higher floor; the command is pending/retrying
        if new > ceiling:
            if not self._floor_exhausted:
                self._floor_exhausted = True
                _LOGGER.critical(
                    "Miner cannot hold %dW and the floor may not exceed %dW (%d restarts) — "
                    "not raising it further; the miner needs service",
                    limit, ceiling, self._floor_short_runs,
                )
                self._notify(
                    "floor_ceiling",
                    f"Miner cannot hold any limit up to {ceiling} W",
                    f"The miner keeps restarting at {limit} W. Raising the power floor further "
                    f"would take it past {ceiling} W (Power Min {self._power_min} W + "
                    f"{FLOOR_MAX_RAISE_W} W), so the controller has stopped raising it. On the "
                    "M64 this pattern goes with hashboard errors 560-563 (slot power/hashrate "
                    "imbalance: reseat the adapter/ribbon, re-torque the busbar). Turn Mining "
                    "Control off if you want it stopped.",
                )
            return
        self._floor_learned = new
        self._floor_learned_at = now
        self._floor_learn_limit = limit
        self._floor_exhausted = False
        if self._floor_proven_ok is not None and self._floor_proven_ok <= limit:
            self._floor_proven_ok = None  # the hardware floor has moved
        _LOGGER.warning(
            "Miner restarted %d times in a row at %dW without a command — raising the effective "
            "floor to %dW (Power Min %dW)", self._floor_short_runs, limit, new, self._power_min,
        )
        self._notify(
            "floor_raised",
            f"Miner cannot hold {limit} W",
            f"The miner restarted {self._floor_short_runs} times in a row at a {limit} W limit "
            f"without a command from the controller — hashing never lasted "
            f"{FLOOR_STABLE_S / 60:.0f} min. The controller now treats {new} W as its lowest "
            f"limit (Power Min is {self._power_min} W) and will not command below it. On the M64 "
            "this pattern goes with hashboard errors 560-563 (slot power/hashrate imbalance: "
            "reseat the adapter/ribbon, re-torque the busbar). After servicing, press Reset "
            "Learned Floor to re-test lower limits.",
        )
        self._publish_floor()
        self._save_later()

    def _publish_floor(self) -> None:
        self._pid_state["power_floor"] = {
            "effective": self._floor(),
            "configured": self._power_min,
            "learned": self._floor_learned,
            "learned_at": _iso(self._floor_learned_at),
            "unholdable_limit": self._floor_learn_limit,
            "proven_ok": self._floor_proven_ok,
            "short_runs": self._floor_short_runs,
            "exhausted": self._floor_exhausted,
            "run_uptime_s": self._last_uptime,
        }

    async def _run_pid_step(
        self, temp: float | None, caps: frozenset[str], summary: str, shutoff: ds.ShutoffState
    ) -> None:
        """Compute PID output and push to the miner if it changed meaningfully."""
        now = time()
        if temp is None:
            await self._run_fallback_step(caps, summary, now)
            return
        if self._in_fallback:
            self._in_fallback = False
            self._pid.clear_samples()
            self._last_input_time = None
            self._ramped_target = None
            seeded = self._seed_bumpless_transfer()
            _LOGGER.info("Supply probe %s is back — resuming PID from ≈%dW", self._external_sensor_id, seeded)

        if self._slope_last_pv is not None and self._slope_ewma_tau_s > 0:
            prev_t, prev_v = self._slope_last_pv
            dt = now - prev_t
            if dt > 0:
                inst = (temp - prev_v) / (dt / 60.0)
                alpha = 1 - math.exp(-dt / self._slope_ewma_tau_s)
                self._slope_ewma = inst if self._slope_ewma is None else self._slope_ewma + alpha * (inst - self._slope_ewma)
        self._slope_last_pv = (now, temp)

        user_target = self._target()
        last = self._last_input_time or now

        # Setpoint ramp: move the effective setpoint toward the user target at
        # ≤ ramp_rate °F/min, seeded from current PV on the first tick.
        if self._setpoint_ramp_rate > 0:
            if self._ramped_target is None:
                self._ramped_target = float(temp)
            dt_min = max(0.0, (now - last) / 60.0)
            max_move = self._setpoint_ramp_rate * dt_min
            delta = user_target - self._ramped_target
            if abs(delta) <= max_move:
                self._ramped_target = user_target
            else:
                self._ramped_target += max_move if delta > 0 else -max_move
            target = self._ramped_target
        else:
            self._ramped_target = user_target
            target = user_target

        # Integral band: only freeze accumulation when far from SP AND the
        # output has hit a saturation rail (see const.py).
        error_abs = abs(target - float(temp))
        integral_snapshot = self._pid.integral

        outdoor_temp = None
        if self._ke > 0 and (self._outdoor_temp_sensor_id is not None or self._weather_entity_id is not None):
            outdoor_temp = self._blended_outdoor_temp(now, self._forecast_cache)

        # Price/surplus envelope: constrain out_max. Order of precedence:
        # safety_caps > demand lockout > tou/surplus envelope > pid_output
        floor = self._floor()
        self._pid.out_min = float(floor)
        self._pid.out_max = float(self._power_max)
        if self._price_sensor_id is not None or self._surplus_sensor_id is not None:
            price_score = surplus_score = 1.0
            if self._price_sensor_id is not None and self._price_high > self._price_low:
                price = self._read_numeric_sensor(self._price_sensor_id, "Price sensor", "_price_unavail_logged")
                if price is not None:
                    price_score = (self._price_high - price) / (self._price_high - self._price_low)
                    price_score = round(max(0.0, min(1.0, price_score)) / 0.05) * 0.05
            if self._surplus_sensor_id is not None and self._surplus_full > self._surplus_deficit:
                surplus = self._read_numeric_sensor(self._surplus_sensor_id, "Surplus sensor", "_surplus_unavail_logged")
                if surplus is not None:
                    surplus_score = (surplus - self._surplus_deficit) / (self._surplus_full - self._surplus_deficit)
                    surplus_score = round(max(0.0, min(1.0, surplus_score)) / 0.05) * 0.05
            multiplier = min(price_score, surplus_score, 1.0)
            self._pid.out_max = self._pid.out_min + (self._pid.out_max - self._pid.out_min) * multiplier
        self._pid_state["out_max_effective"] = int(self._pid.out_max)
        self._pid_state["out_min_effective"] = int(self._pid.out_min)

        try:
            output, did_calc = self._pid.calc(
                input_val=float(temp), set_point=float(target), input_time=now,
                last_input_time=last, ext_temp=outdoor_temp,
            )
        except Exception as err:  # defensive — don't break coordinator loop
            _LOGGER.exception("PID calculation failed: %s", err)
            return

        sat_tol = 1.0
        error = target - float(temp)
        on_low_rail = output <= float(floor) + sat_tol
        # Directional: freeze the integral only when it would wind further
        # into the rail the output already sits on.
        output_saturated = (output >= float(self._power_max) - sat_tol and error > 0) or (
            on_low_rail and error < 0
        )
        if self._integral_band > 0 and error_abs > self._integral_band and output_saturated:
            self._pid.integral = integral_snapshot
            output = self._pid.proportional + integral_snapshot + self._pid.derivative + self._pid.external
            output = max(min(output, float(self._power_max)), float(floor))
            self._pid._output = output
        elif on_low_rail and error > 0:
            # On the low rail with the supply below target. Every clamp
            # restarts the miner and the start edge re-seeds output ≈ limit,
            # i.e. exactly onto out_min; the vendored PID never integrates
            # while its last output sits on a rail, so with a flat or rising
            # supply nothing would ever lift it off. Nudge it just above so
            # the integral can run next tick (a 1 W change commands nothing).
            self._pid._output = float(floor) + sat_tol + 0.01

        self._last_input_time = now
        if not did_calc:
            return

        requested_power = int(round(output))
        new_power = requested_power
        current_limit = self.coordinator.data.get("wattage_limit") or 0

        safety_engaged = bool(caps)
        mode = "pid"
        if safety_engaged:
            new_power = self._cap_clamp(caps)
            mode = "safety_cap"

        # Demand lockout: no thermostat calling → zone pumps idle → stagnant
        # primary loop. Force the floor and engage safety. The shutoff dwell
        # runs on top of this clamp.
        if self._demand_entities and summary == ds.ALL_IDLE:
            new_power = floor
            safety_engaged = True
            if mode == "pid":
                mode = "demand_lockout"
            if not self._no_demand_logged:
                _LOGGER.warning(
                    "No demand from %s — forcing %dW until a thermostat calls%s",
                    ", ".join(self._demand_entities), new_power,
                    f" (shutoff: {self._pid_state['demand_shutoff']['reason']})" if self._shutoff_cfg.active else "",
                )
                self._no_demand_logged = True
        elif self._no_demand_logged:
            _LOGGER.info("Demand returned — releasing no-demand lockout")
            self._no_demand_logged = False
        if shutoff.state == ds.DWELL:
            mode = "dwell"
            safety_engaged = True
            new_power = floor
        self._pid_state["control_mode"] = mode

        self._pid_state.update(
            {
                "error": self._pid.error,
                "proportional": self._pid.proportional,
                "integral": self._pid.integral,
                "derivative": self._pid.derivative,
                "external": self._pid.external,
                "output": new_power,
                "requested_output": requested_power,
                "safety_engaged": safety_engaged,
                "pv_slope": self._slope_ewma,
            }
        )

        # Actuation gate: magnitude, time, and the post-resume boot hold. Each
        # adjust_power_limit restarts mining. Safety caps and floor enforcement
        # (believed limit below a floor the miner cannot hold) bypass the time
        # gates and the boot hold.
        reference = self._reference(current_limit)
        floor_fire = self._floor_fire(reference, now, caps)
        if floor_fire:
            # Enforce exactly the floor through the hold; the PID's own request
            # (often far higher on a cold loop) goes through the normal gates.
            new_power = floor
            self._pid_state["output"] = new_power
        if reference is None:
            # No idea what the miner is at: one command is cheaper than guessing.
            reference = -10_000
        delta = abs(new_power - reference)
        elapsed = now - self._last_command_time
        if error_abs <= self._fine_step_band:
            effective_min_step, band_label = self._min_power_step_fine, "fine"
        elif error_abs <= self._coarse_step_band:
            effective_min_step, band_label = self._min_power_step_medium, "medium"
        else:
            effective_min_step, band_label = self._min_power_step, "coarse"
        if self._slope_ewma is not None and self._slope_ewma_tau_s > 0:
            target_dir = math.copysign(1, target - temp)
            if math.copysign(1, self._slope_ewma) == target_dir and abs(self._slope_ewma) > 0.5:
                if effective_min_step == self._min_power_step:
                    effective_min_step, band_label = self._min_power_step_medium, "coarse→medium"
                elif effective_min_step == self._min_power_step_medium:
                    effective_min_step, band_label = self._min_power_step_fine, "medium→fine"
        step_ok = delta >= effective_min_step
        effective_interval = self._min_adjust_interval_increase if new_power > reference else self._min_adjust_interval
        interval_ok = elapsed >= effective_interval
        safety_fire = bool(caps) and step_ok
        boot_hold = now < self._resume_hold_until

        if not safety_fire and not floor_fire and (not (step_ok and interval_ok) or boot_hold):
            _LOGGER.debug(
                "PID actuation throttled: Δ=%dW (need %dW, %s band), elapsed=%.0fs (need %ds)%s",
                delta, effective_min_step, band_label, elapsed, effective_interval,
                ", boot hold" if boot_hold else "",
            )
            self._pid_state["output"] = self._last_commanded_power
            return

        _LOGGER.info(
            "PID: temp=%.1f°F target=%.1f°F → power %dW (was %dW, err=%.2f)",
            temp, target, new_power, reference, self._pid.error,
        )
        await self._set_power_limit(new_power, floor_fire)


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")
