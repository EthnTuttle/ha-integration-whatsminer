"""The heating controller: PID on the supply probe, safety caps, demand shutoff (v2.0 — see docs/v2.0-simplification.md).

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

All power moves go out over v3 ``set.miner.power`` (restart-free once tuning
finished); v2 ``adjust_power_limit`` only raises or lowers the ceiling in
force, or carries a safety clamp during the boot window. Every commanded
power is clamped to the fault bounds (see ``_bounds``): the floor is Power
Min, or the last uncommanded short run's power plus ``CRASH_STEP_W`` while
that crash evidence is fresh; the ceiling is Power Max, or the highest
witnessed hold (else the power that faulted) plus ``PROVEN_MARGIN_W`` while
fault evidence is fresh. On an inversion the ceiling wins: protect the
hardware.
"""
from __future__ import annotations

import asyncio
import logging
import math
from collections import deque
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
# Predictive limiting. The actuation gate looks PREDICT_HORIZON_S ahead along
# the supply's least-squares slope over SLOPE_WINDOW_S (needs SLOPE_MIN_SPAN_S
# of samples). The horizon is the pessimistic miner-to-probe lag: a loop with
# zones calling answers a cut in 2-4 min, but zones satisfying mid-climb
# leave it stagnant while the heat already sent keeps arriving for ~10-15
# min (2026-10-08: resumes from ~88°F at 3.5-4.2 kW peaked at 124-130°F).
# Only a slope of at least PREDICT_MIN_SLOPE_F_MIN counts as rising, so probe
# quantisation (0.1125°F) near setpoint never projects anything.
PREDICT_HORIZON_S = 720.0
SLOPE_WINDOW_S = 300.0
SLOPE_MIN_SPAN_S = 120.0
PREDICT_MIN_SLOPE_F_MIN = 0.2
# A change larger than this between two samples (the Scout reports once a
# minute; the fastest real climb is ~3.3°F/min) is a probe glitch or a
# reconnect: restart the slope window instead of projecting it.
SUPPLY_JUMP_F = 5.0
# Soft start. After a start we did not cause with a limit change (demand-
# shutoff resume, Mining Control on, lockout reset, firmware restart), an HA
# restart onto a cold loop, or a predictive back-off, the soft start owns
# increases: hold the limit through the boot hold, then step up only while
# the projection is short of target, at most once per PREDICT_HORIZON_S (or
# the increase interval if longer), by Kp × that shortfall capped at
# SOFT_START_STEP_FRAC of the floor..ceiling span. The integral is frozen
# meanwhile and re-seeded from the power in force once the supply is within
# the fine band of target. Never forces a decrease, never blocks floor work.
SOFT_START_STEP_FRAC = 0.5
# A run this long (hashing, Elapsed minus idle uptime) is "stable": a shorter
# uncommanded restart is a crash (raises the fault floor), a full run
# witnesses a proven hold (raises the proven ceiling).
STABLE_RUN_S = 900.0
# Fault evidence (crash, error code, external derate, hashrate collapse)
# expires after this long; bounds revert to the configured min/max.
FAULT_TTL_S = 7 * 86400.0
# A crash says "the miner cannot hold this low": the fault floor becomes the
# crashed power plus this step.
CRASH_STEP_W = 250
# While fault evidence is armed the ceiling is the proven hold (or the fault
# power, or Power Min) plus this margin.
PROVEN_MARGIN_W = 250
# While a hold keeps running, refresh the proven-max timestamp at most this
# often (each refresh is a store write).
PROVEN_REFRESH_S = 3600.0
# Restarts while the supply is hot may be thermal, not power evidence. Hot:
# the supply cap or the freeze-guard hold over the lockout is engaged, the
# supply is within SUPPLY_HOT_MARGIN_F of the supply cap (the S-trigger
# dwell's release band), or it was within SUPPLY_HOT_WINDOW_S ago. The
# window equals STABLE_RUN_S, so a short run that ended inside it may have
# begun hot.
SUPPLY_HOT_MARGIN_F = 5.0
SUPPLY_HOT_WINDOW_S = STABLE_RUN_S
# v3 power regulation. v2 adjust_power_limit restarts btminer on every
# change. v3 set.miner.power is applied without an immediate btminer
# restart — but around a large step the mining process can recycle ~80 s
# after the command (observed on firmware 20260908.12: the 1250 W step of
# 2026-10-12 ramped 1884 -> 1244 W with elapsed counting, then elapsed
# reset to 0 and working went false for ~45 s;
# /home/ethan/network/power-test-20261012-220429.log, adjudicated in
# /home/ethan/network/api-design-synthesis.md §1.2). Treat every power
# write as potentially interrupting hashing briefly, and only send one
# while tuning has finished (summary up-freq-finish 1).
#
# The v2 limit is a ceiling. It is raised only when the regulator asks for
# more than the limit in force and the loop can take a boot at the ceiling
# (see _ceiling_ok); the increase is then delivered via set.miner.power once
# tuned. Boot window (up-freq-finish != 1, unknown, or within
# V3_TUNE_GRACE_S of our own write): set.miner.power would only apply at the
# next boot, so safety clamps keep the legacy limit clamp; every other
# change is held as the target and delivered once tuned.
V3_TUNE_GRACE_S = 90.0
# Still tuning this long after a start or our write, with the miner above
# the power we want (it tunes toward the ceiling): give up on the ceiling,
# send the wanted power as the limit, and keep the ceiling down for
# V3_CEILING_BACKOFF_S.
V3_TUNE_TIMEOUT_S = 2700.0
V3_CEILING_BACKOFF_S = 6 * 3600.0
# set.miner.power answered a non-zero code: that change goes out as
# adjust_power_limit, and v3 is retried after this long. Transport failures
# (4433 not answering) fall back after this many in a row.
V3_POWER_RETRY_S = 1800.0
V3_TRANSPORT_FAILS = 3
# Hashrate collapse after a set.miner.power step: hashrate per watt below
# this fraction of the pre-step value for V3_COLLAPSE_POLLS polls in a row,
# within V3_COLLAPSE_WATCH_S of the step, is crash evidence at the stepped-to
# power. The step is NOT undone — an undoing adjust_power_limit would answer
# a documented side effect of one write with a certain restart — the
# collapse is reported (log, notification, power_regulation_error) and left
# for the operator. Live, a 1884 -> 1244 W step kept ~85% of the pre-step
# TH/W through the ramp.
V3_COLLAPSE_FRAC = 0.5
V3_COLLAPSE_POLLS = 3
V3_COLLAPSE_WATCH_S = 1800.0
# Set-and-hold ("pinned") mode: when the configured band collapses below the
# default fine step (power_max - power_min < PINNED_BAND_W) there is no
# meaningful regulation left — the synthesis verdict is that a PID inside a
# sub-step band only re-issues tune-churning writes. The PID actuation is
# bypassed for a fixed power_min target, reconciled at most once per tuned
# epoch and only when the believed operating point differs by at least
# PINNED_RECONCILE_W (see _pinned_step and the write gate).
PINNED_BAND_W = 50
PINNED_RECONCILE_W = 25
# The telemetry power limit is only believed when it persists: it differs
# from what we last sent for LIMIT_PERSIST_POLLS polls in a row and btminer
# has been up BOOT_TRANSIENT_S. 2026-10-12: ~40 s after an uncommanded
# restart the M64 reported a derated 1905 W.
LIMIT_PERSIST_POLLS = 3
BOOT_TRANSIENT_S = 120.0
# External-derate detection: the miner/firmware lowered the reported wattage
# limit without us commanding it (2026-10-17: firmware error 2320 and a
# silent derate to 1224 W while the controller believed 2649 W). Armed only
# when the gap is real (more than DERATE_MIN_DELTA_W below the last limit WE
# sent), not inside the DERATE_POST_WRITE_S window after our own write (a
# limit write restarts btminer and the report lags), not while the miner is
# stopped or still tuning, and sustained for DERATE_POLLS polls spanning at
# least DERATE_MIN_S. Clears after DERATE_POLLS polls back at the commanded
# limit. The fault power noted is the derated-TO power, so the ceiling
# converges onto what the hardware demonstrated (the 2026-10-17/18 1684-vs-
# 1224 livelock came from noting the refused value instead).
DERATE_MIN_DELTA_W = 100
DERATE_POST_WRITE_S = 90.0
DERATE_MIN_S = 120.0
DERATE_POLLS = 3
# After a user's Mining Control OFF, ignore lingering "mining" readings for
# this long so the PID can't send a limit change to a miner powering down.
USER_OFF_GRACE_S = 180.0
# Hourly forecast cache (met.no updates hourly), and how long a stale cache
# is still used when the forecast service keeps failing.
FORECAST_CACHE_S = 900.0
FORECAST_STALE_MAX_S = 6 * 3600.0
# Hourly outdoor sample spacing for the centred 24 h mean.
OUTDOOR_SAMPLE_INTERVAL_S = 3600.0

STORE_VERSION = 1


class _Regression(NamedTuple):
    """An Elapsed regression awaiting confirmation by the next poll."""

    prev_uptime: float
    seen_uptime: float
    ours: bool
    # How long the run that ended had hashed: prev_uptime minus Elapsed at
    # the last idle poll (a v3 stop/start does not reset Elapsed).
    run_s: float


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
        self._ki = float(g(CONF_PID_KI, DEFAULT_PID_KI))
        self._kd = float(g(CONF_PID_KD, DEFAULT_PID_KD))
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
        self._freeze_threshold = float(g(CONF_FREEZE_GUARD_THRESHOLD, DEFAULT_FREEZE_GUARD_THRESHOLD))
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
            freeze_threshold=self._freeze_threshold,
        )
        self._cold_room_delta = float(
            g(CONF_PID_DEMAND_SHUTOFF_COLD_ROOM_DELTA, DEFAULT_PID_DEMAND_SHUTOFF_COLD_ROOM_DELTA)
        )
        # Freeze guard
        self._freeze_sensor_id: str | None = g(CONF_FREEZE_GUARD_SENSOR) or None
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
        self._tick_interval_s = 30.0
        self._last_tick_at = 0.0
        self._user_off_at = 0.0
        self._step_task: asyncio.Task | None = None
        self._step_lock = asyncio.Lock()
        self._saved_target: float | None = None
        self._resume_hold_until = 0.0
        self._max_off_notified = False
        self._v2_fallback_notified = False
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
        # Supply samples for the predictive slope, and the soft start.
        self._supply_samples: deque[tuple[float, float]] = deque()
        self._soft_start = False
        self._caps_active: frozenset[str] = frozenset()
        self._last_lockout_power_off: float = 0.0
        # Restart attribution. _pending_restart_at is consumed by the first
        # uptime regression after one of our own commands.
        self._pending_restart_at: float | None = None
        self._pending_regression: _Regression | None = None
        # The power the run under a pending regression held; _op_in_force is
        # re-pointed at the limit the moment the regression is first seen, so
        # crash evidence must be snapshotted here.
        self._regression_power: int | None = None
        self._last_restart_ours = False
        self._sent_limit: int | None = None
        self._sent_limit_at: float = 0.0
        self._last_uptime: float | None = None
        # Elapsed at the last poll that was up but not hashing. A v3 service
        # stop keeps btminer (and Elapsed) running, so the run that follows a
        # v3 start is uptime minus this, not uptime.
        self._idle_uptime: float | None = None
        self._supply_hot_at: float | None = None
        # Power regulation (v3 only; see V3_TUNE_GRACE_S). _limit_w is the v2
        # ceiling last sent (or adopted from persistent telemetry); _op_target
        # is the operating power the regulator wants, _op_in_force what the
        # miner is believed to run at (the limit after any restart or
        # adjust_power_limit), _op_established whether _op_target has been
        # delivered since the last restart.
        self._limit_w: int | None = None
        self._limit_mismatch_polls = 0
        self._reported_limit_last = 0
        self._reported_limit_polls = 0
        self._op_target: int | None = None
        self._op_in_force: int | None = None
        self._op_in_force_at: float = time()
        self._op_established = False
        self._tune_grace_until = 0.0
        self._untuned_since: float | None = None
        self._ceiling_hold_until = 0.0
        self._v3_retry_at = 0.0
        self._v3_transport_fails = 0
        self._v3_error: str | None = None
        self._power_fallback_notified = False
        self._last_power_command: str | None = None
        # Hashrate-collapse watch after a set.miner.power step:
        # (until, pre-step TH per W, pre-step power, stepped-to power).
        self._collapse_watch: tuple[float, float, int, int] | None = None
        self._collapse_polls = 0
        # Set-and-hold: the configured band collapsed below the fine step, so
        # the PID is bypassed for a fixed power_min target (see _pinned_step).
        self._pinned = (self._power_max - self._power_min) < PINNED_BAND_W
        # The write gate (see _gate_write): one non-safety power write per
        # tuned epoch. _epoch_tuned_seen tracks the up-freq-finish edge that
        # opens a new epoch; a restart (Elapsed regression) resets it too.
        self._epoch_write_used = False
        self._epoch_tuned_seen = False
        self._gate_last_suppression: tuple[str, str] | None = None
        # Notify-only regulation error (e.g. "collapse-observed-no-undo");
        # _regulation_error_tpw is the TH/W it self-clears above.
        self._regulation_error: str | None = None
        self._regulation_error_tpw: float | None = None
        # External derate: _limit_commanded is the last limit WE sent
        # (adjust_power_limit); the telemetry-adoption rule must never touch
        # it — adopting a derated report as ours would mask the derate.
        self._limit_commanded: int | None = None
        self._limit_commanded_at: float = 0.0
        self._derate_candidate_since: float | None = None
        self._derate_candidate_polls = 0
        self._derate_match_polls = 0
        self._external_derate = False
        self._error_pairs_prev: set[tuple[str, str]] = set()
        self._error_notify_keys: set[str] = set()
        # Fault memory (the v2.0 fault bounds; see _bounds). _hold_tainted
        # suppresses proven-hold witnessing for the run in progress when
        # fault evidence arrives; our next power command marks a clean run
        # boundary and clears it.
        self._crash_at: float | None = None
        self._crash_floor: int | None = None
        self._fault_at: float | None = None
        self._fault_power: int | None = None
        self._fault_reason: str | None = None
        self._proven_max: int | None = None
        self._proven_max_at: float | None = None
        self._hold_tainted = False
        # The hold in progress: power believed in force since _hold_since.
        self._hold_power: int | None = None
        self._hold_since: float | None = None
        self._cap_inversion_logged = False
        self._demand_summary: str = ds.UNKNOWN
        self._pid = PID(
            kp=self._kp,
            ki=self._ki,
            kd=self._kd,
            ke=self._ke,
            out_min=float(self._power_min),
            out_max=float(self._power_max),
            sampling_period=0,
        )
        self._pid_state.setdefault("lockout_latched", False)
        self._pid_state.setdefault("safety_engaged", False)
        if self._pid_state.get("target") is None:
            self._pid_state["target"] = self._default_target
        self._pid_state.update({"pid_kp": self._kp, "pid_ki": self._ki, "pid_kd": self._kd})
        self._publish_shutoff(ds.Decision(ds.NONE, "starting", (), self._shutoff))
        self._publish_freeze(None, None, None)
        self._publish_faults()
        self._publish_regulation()
        self._pid_state["control_mode"] = "idle"
        self._pid_state["pid_mode"] = "pinned" if self._pinned else "pid"

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
        # Fault memory. Tolerant of pre-2.0 payloads: the new fault_* keys are
        # preferred; the old proven_fault_* keys are honoured; floor_*,
        # daily_* and autotune keys are simply ignored (nowhere to land).
        try:
            if data.get("proven_max") is not None:
                self._proven_max = int(data["proven_max"])
            if data.get("proven_max_at") is not None:
                self._proven_max_at = float(data["proven_max_at"])
            fault_at = data.get("fault_at", data.get("proven_fault_at"))
            fault_power = data.get("fault_power", data.get("proven_fault_power"))
            if fault_at is not None:
                self._fault_at = float(fault_at)
            if fault_power is not None:
                self._fault_power = int(fault_power)
            self._fault_reason = data.get("fault_reason") or data.get("proven_fault_reason") or None
            if data.get("crash_floor") is not None:
                self._crash_floor = int(data["crash_floor"])
            if data.get("crash_at") is not None:
                self._crash_at = float(data["crash_at"])
        except (TypeError, ValueError):
            self._proven_max = self._proven_max_at = self._fault_at = self._fault_power = None
            self._fault_reason = None
            self._crash_at = self._crash_floor = None
        try:
            for attr, key in (
                ("_limit_w", "power_limit_w"),
                ("_op_target", "regulated_power_w"),
            ):
                value = data.get(key)
                setattr(self, attr, int(value) if value is not None and int(value) > 0 else None)
        except (TypeError, ValueError):
            self._limit_w = self._op_target = None
        _LOGGER.info(
            "Power regulation: set.miner.power (ceiling %s in force, Power Max %dW; "
            "operating power %s)",
            f"{self._limit_w}W" if self._limit_w is not None else "unknown", self._power_max,
            f"{self._op_target}W" if self._op_target is not None else "not yet commanded",
        )
        floor, ceiling = self._bounds(now)
        if self._crash_at is not None and now - self._crash_at < FAULT_TTL_S:
            _LOGGER.warning(
                "Crash floor %dW still armed from before restart (crash %s) — Power Min is %dW; "
                "Reset Fault Memory re-tests lower limits",
                floor, _iso(self._crash_at), self._power_min,
            )
        if self._fault_at is not None and now - self._fault_at < FAULT_TTL_S:
            _LOGGER.warning(
                "Fault ceiling still armed from before restart (fault: %s) — power commands "
                "cap at %dW",
                self._fault_reason or "unknown", ceiling,
            )
        self._publish_faults()
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
            "power_limit_w": self._limit_w,
            "regulated_power_w": self._op_target,
            "proven_max": self._proven_max,
            "proven_max_at": self._proven_max_at,
            "fault_at": self._fault_at,
            "fault_power": self._fault_power,
            "fault_reason": self._fault_reason,
            "crash_floor": self._crash_floor,
            "crash_at": self._crash_at,
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
                self._arm_soft_start("Mining Control on")
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

    async def async_reset_fault_memory(self) -> None:
        """Forget crash/fault evidence so the bounds open back out.

        For after the hardware has been serviced (reseated ribbon, busbar).
        There is no automatic downward re-test: boot survival at a marginal
        power is stochastic, so a timed re-probe would recreate the crash loop.
        The hold in progress is left alone but its taint is dropped so it may
        witness a new proven maximum.
        """
        async with self._step_lock:
            had_floor, had_ceiling = self._crash_floor, self._fault_at is not None
            self._crash_at = None
            self._crash_floor = None
            self._fault_at = None
            self._fault_power = None
            self._fault_reason = None
            self._proven_max = None
            self._proven_max_at = None
            self._hold_tainted = False
            self._external_derate = False
            self._derate_candidate_since = None
            self._derate_candidate_polls = 0
            self._derate_match_polls = 0
            await self._save()
        for key in ("crash_floor", "hardware_fault", "power_collapse", *self._error_notify_keys):
            persistent_notification.async_dismiss(self.hass, f"{DOMAIN}_{key}")
        self._error_notify_keys.clear()
        _LOGGER.warning(
            "Fault memory reset (crash floor was %s, fault ceiling %s) — the bounds are "
            "Power Min %dW to Power Max %dW again",
            f"{had_floor}W" if had_floor is not None else "not set",
            "armed" if had_ceiling else "not armed",
            self._power_min, self._power_max,
        )
        self._publish_faults()
        self.coordinator.async_update_listeners()

    # --------------------------------------------------------- coordinator

    @callback
    def _handle_coordinator_update(self) -> None:
        """Detect mining transitions then run a control step."""
        is_mining = bool(self.coordinator.data.get("is_mining"))
        now = time()
        if self.coordinator.last_update_success:
            try:
                # Error codes and the derate first: the taint they set must
                # block hold witnessing on this same poll.
                self._note_error_codes(now)
                self._note_derate(is_mining, now)
                self._note_uptime_and_crashes(is_mining, now)
                self._note_reported_limit(is_mining, now)
            except Exception:  # bookkeeping must never block edge handling or the tick
                _LOGGER.exception("Restart bookkeeping failed")
        if self._last_is_mining is None and is_mining:
            # First poll after (re)load with the miner already hashing: seed so
            # the first tick doesn't slam the power from Kp·error alone, and
            # hold actuation in case we restarted inside a boot window.
            seeded = self._seed_bumpless_transfer()
            self._note_btminer_restart(now)  # re-command the regulated power once tuned
            try:
                uptime = float(self.coordinator.data.get("uptime") or 0)
            except (TypeError, ValueError):
                uptime = 0.0
            if uptime < RESUME_BOOT_HOLD_S:
                self._resume_hold_until = max(
                    self._resume_hold_until, time() + RESUME_BOOT_HOLD_S - uptime
                )
            temp = self._current_temperature()
            if temp is not None and temp < self._target() - self._fine_step_band:
                self._arm_soft_start("controller started onto a cold loop")
            self._hold_power = self._power_in_force()
            self._hold_since = now
            _LOGGER.info("Controller started — seeded PID from current power (≈%dW)", seeded)
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
                    self._soft_start = False  # the start edge re-arms it
                    _LOGGER.info("Mining stopped — PID controller state reset")
                else:
                    # The restart every adjust_power_limit causes.
                    _LOGGER.info("Mining stopped after our limit change — throttle clock kept")
                # The hold that was running ended with the run.
                self._settle_hold(now)
                self._hold_power = None
                self._hold_since = None
            else:
                # Whatever started it (our resume, firmware auto-start, web
                # UI), it is booting now: no limit change for the boot hold.
                # A start we caused always arms it; an uncommanded start arms
                # it only while no crash episode is open (the first
                # spontaneous restart gets a hold, the crash loop that may
                # follow does not), so the hold expires at most
                # RESUME_BOOT_HOLD_S after the first start of an episode
                # however fast the miner loops.
                seeded = self._seed_bumpless_transfer()
                ours = self._claim_start(now)
                self._note_btminer_restart(now)
                if ours or not self._crash_episode_open(now):
                    self._resume_hold_until = max(self._resume_hold_until, now + RESUME_BOOT_HOLD_S)
                if self._recently_sent_limit() is None:
                    # Not the restart our own limit change causes (that one
                    # keeps whatever soft start was running).
                    self._arm_soft_start("mining resumed")
                self._settle_hold(now)
                self._hold_power = self._power_in_force()
                self._hold_since = now
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
            self._publish_regulation()
            self._publish_faults()
            # Entities rendered pid_state before this tick updated it.
            self.coordinator.async_update_listeners()

    # ------------------------------------------------------------- the tick

    async def _control_step(self) -> None:
        now = time()
        temp = self._current_temperature()
        fresh = bool(self.coordinator.last_update_success)
        self._note_supply(now, temp)
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
        self._demand_summary = summary
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
        self._supply_hot(now, temp)
        if await self._regulation_tick(now, caps, is_mining):
            return  # this tick's command went out (a clamp/reconciliation restart)
        await self._run_pid_step(temp, caps, summary, state)

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
                # Stop-class write: always exempt from the write gate.
                self._gate_write(time(), safety=True, kind="stop")
                await self.coordinator.api.power_off()
                self._note_control_path()
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
                    # Resume-edge power_on: always exempt from the write gate.
                    self._gate_write(time(), safety=True, kind="stop")
                    await self.coordinator.api.power_on()
                    self._note_control_path()
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
                # Stop-class write: always exempt from the write gate.
                self._gate_write(time(), safety=True, kind="stop")
                await self.coordinator.api.power_off()
                self._note_control_path()
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
        # Stop-class write (a latching safety stop): always exempt from the
        # write gate.
        self._gate_write(time(), safety=True, kind="stop")
        try:
            await self.coordinator.api.power_off()
            self._note_control_path()
            self._mark_actuation()
            self._last_commanded_power = None
            self._last_command_time = time()
        except Exception as err:
            _LOGGER.error("Failed to stop mining on supply-temp lockout: %s", err)

    def _note_control_path(self) -> None:
        """Notify while stops/starts fall back to v2 on firmware that has v3.

        v2 power_off does not hold on that firmware, so a stop that went out
        as v2 is one the lockout and demand shutoff can't count on.
        """
        fallback = getattr(self.coordinator.api, "v3_fallback", None)
        if fallback:
            self._v2_fallback_notified = True
            self._notify(
                "v2_fallback",
                "Miner command sent via v2 fallback",
                f"{fallback}. On this firmware v2 power_off may not keep the miner "
                "off; the controller re-sends the stop if hashing returns. Check "
                "that the miner's API (port 4433) is reachable.",
            )
        elif self._v2_fallback_notified:
            self._v2_fallback_notified = False
            persistent_notification.async_dismiss(self.hass, f"{DOMAIN}_v2_fallback")

    # ------------------------------------------------------------ safety caps

    def _evaluate_safety_caps(self, temp: float | None) -> frozenset[str]:
        """Return the engaged soft caps, logging only when the set changes.

        Chip-temp guards the *miner*; the supply cap guards the *plant* (a
        stagnant loop can trip the boiler's own high-limit even at power_min).
        Both force Power Min, never the (possibly higher) fault floor: see
        the lockout comment in _control_step.
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
                floor, _ = self._bounds(time())
                _LOGGER.warning(
                    "Safety cap engaged (chip %s°F / cap %.1f°F, supply %s°F / cap "
                    "%.1f°F) — forcing %dW%s",
                    f"{chip:.1f}" if chip is not None else "?",
                    self._chip_temp_safety_cap,
                    f"{temp:.1f}" if temp is not None else "?",
                    self._supply_temp_safety_cap,
                    self._cap_clamp(active),
                    f" (below the effective floor {floor}W)" if floor > self._cap_clamp(active) else "",
                )
            elif not active:
                _LOGGER.info("Safety caps cleared")
            self._caps_active = active
        return active

    def _cap_clamp(self, caps: frozenset[str]) -> int:
        """What an engaged cap forces: Power Min, never the fault floor."""
        return self._power_min

    def _chip_temp(self) -> float | None:
        temp = self.coordinator.data.get("temperature_avg")
        try:
            value = float(temp)
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None

    def _supply_hot(self, now: float, temp: float | None = None) -> bool:
        """True while restarts may be thermal, not crash evidence (see SUPPLY_HOT_MARGIN_F)."""
        hot = "supply" in self._caps_active or self._freeze_hold_over_lockout or (
            temp is not None and temp >= self._supply_temp_safety_cap - SUPPLY_HOT_MARGIN_F
        )
        if hot:
            self._supply_hot_at = now
            return True
        return self._supply_hot_at is not None and now - self._supply_hot_at < SUPPLY_HOT_WINDOW_S

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

    @property
    def pv_slope_enabled(self) -> bool:
        """EWMA supply-slope tracking is configured (tau > 0)."""
        return self._slope_ewma_tau_s > 0

    @property
    def demand_tracking_enabled(self) -> bool:
        """Thermostat/demand entities are configured."""
        return bool(self._demand_entities)

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

    # ------------------------------------------------------------- fault bounds

    def _bounds(self, now: float) -> tuple[int, int]:
        """The (floor, ceiling) every commanded power is clamped into. The one bounds source.

        Floor: Power Min, or the last uncommanded short run's power plus
        CRASH_STEP_W while that crash evidence is fresh (a crash says "the
        miner cannot hold this low a power"). Ceiling: Power Max, or —
        while fault evidence (error code, external derate, hashrate
        collapse) is fresh — the highest witnessed hold, else the power
        that faulted, else Power Min, plus PROVEN_MARGIN_W. Noting the
        derated-TO power (not the demanded one) is deliberate: the ceiling
        then converges onto what the hardware demonstrated
        (docs/v2.0-simplification.md, the 2026-10-17/18 livelock).

        On an inversion the ceiling wins down to Power Min: protect the
        hardware, and say so in the log.
        """
        ceiling = self._power_max
        if self._fault_at is not None and now - self._fault_at < FAULT_TTL_S:
            # A witness counts only if it postdates the fault: a run the miner
            # held BEFORE it faulted says nothing about what it can hold now
            # (2026-10-20 night: witness 2150 W from the evening outranked a
            # fresh 561, keeping the ceiling at 2400 W through a derate loop).
            witnessed_since_fault = (
                self._proven_max is not None
                and self._proven_max_at is not None
                and self._proven_max_at > self._fault_at
            )
            if witnessed_since_fault:
                ref = self._proven_max
            elif self._fault_power is not None:
                ref = self._fault_power
            else:
                ref = self._power_min
            ceiling = max(self._power_min, min(self._power_max, ref + PROVEN_MARGIN_W))
        floor = self._power_min
        if (
            self._crash_at is not None
            and now - self._crash_at < FAULT_TTL_S
            and self._crash_floor
        ):
            floor = min(max(self._power_min, self._crash_floor), self._power_max)
        if floor > ceiling:
            if not self._cap_inversion_logged:
                self._cap_inversion_logged = True
                _LOGGER.warning(
                    "Fault bounds inverted (floor %dW > ceiling %dW) — the ceiling wins "
                    "while the evidence lasts (fault: %s)",
                    floor, ceiling, self._fault_reason,
                )
            floor = ceiling
        else:
            self._cap_inversion_logged = False
        return floor, ceiling

    def _note_fault(self, now: float, power: int | None, reason: str) -> None:
        """Fault evidence (error code, external derate, collapse undo).

        Arms the fault ceiling for FAULT_TTL_S and taints the hold in
        progress: a hold already under way when the fault arrived says
        nothing about what the miner can still hold.
        """
        self._fault_at = now
        self._fault_power = power
        self._fault_reason = reason
        self._hold_tainted = True
        self._save_later()

    def _note_crash(self, power: int | None, now: float) -> None:
        """An uncommanded short run: the miner cannot hold down there."""
        if power is None or power <= 0:
            _LOGGER.info("Miner restarted on its own after a short run at an unknown power — logged only")
            return
        self._crash_at = now
        old = self._crash_floor
        self._crash_floor = max(old or 0, power + CRASH_STEP_W)
        _LOGGER.warning(
            "Miner restarted on its own after a short run at %dW — crash floor is now %dW "
            "(Power Min %dW)", power, self._crash_floor, self._power_min,
        )
        self._notify(
            "crash_floor",
            f"Miner cannot hold {power} W",
            f"The miner restarted on its own after a short run at {power} W without a command "
            f"from the controller — hashing never lasted {STABLE_RUN_S / 60:.0f} min. The "
            f"controller now treats {self._crash_floor} W as its lowest limit (Power Min is "
            f"{self._power_min} W) while the evidence is fresh. On the M64 this pattern can go "
            "with hashboard errors 560-563 (slot power/hashrate imbalance: reseat the "
            "adapter/ribbon, re-torque the busbar); check the miner web UI. After servicing, "
            "press Reset Fault Memory to re-test lower limits.",
        )
        self._save_later()

    def _crash_episode_open(self, now: float) -> bool:
        return self._crash_at is not None and now - self._crash_at < FAULT_TTL_S

    # ------------------------------------------------------- witness holds

    def _power_in_force(self) -> int | None:
        """What the miner is believed to run at (operating power, else the limit)."""
        if self._op_in_force is not None:
            return self._op_in_force
        return self._limit_in_force()

    def _settle_hold(self, now: float) -> None:
        """Close the current holding segment; credit it when it ran STABLE_RUN_S clean.

        Skipped for the run in progress when fault evidence arrived
        (_hold_tainted) and while the miner is externally derated (what it
        holds then is its choice, not our ask).
        """
        power, since = self._hold_power, self._hold_since
        if power is None or since is None:
            return
        if (
            now - since >= STABLE_RUN_S
            and not self._hold_tainted
            and not self._external_derate
            and (self._proven_max is None or power > self._proven_max)
        ):
            self._proven_max = power
            self._proven_max_at = now
            _LOGGER.info("Proven hold: the miner held %dW for a full stable run", power)
            self._save_later()

    def _command_boundary(self, now: float) -> None:
        """A command of ours went out: settle the hold, clear the taint, start a clean segment."""
        self._settle_hold(now)
        self._hold_power = self._power_in_force()
        self._hold_since = now
        self._hold_tainted = False

    def _note_hold(self, is_mining: bool, now: float) -> None:
        """Per-poll hold accounting: segment boundaries, and the freshness refresh."""
        power = self._power_in_force() if is_mining else None
        if power is None or power != self._hold_power:
            self._settle_hold(now)
            self._hold_power = power
            self._hold_since = now if power is not None else None
            return
        if self._hold_since is None:
            self._hold_since = now
            return
        # Still holding the proven power: keep the proof fresh (each write
        # is a save, so at most hourly — and only once the hold is stable).
        if (
            power == self._proven_max
            and now - self._hold_since >= STABLE_RUN_S
            and not self._hold_tainted
            and not self._external_derate
            and (self._proven_max_at is None or now - self._proven_max_at >= PROVEN_REFRESH_S)
        ):
            self._proven_max_at = now
            self._save_later()

    def _publish_faults(self) -> None:
        floor, ceiling = self._bounds(time())
        self._pid_state["external_derate"] = self._external_derate
        self._pid_state["faults"] = {
            "floor_w": floor,
            "ceiling_w": ceiling,
            "crash_floor_w": self._crash_floor,
            "crash_at": _iso(self._crash_at),
            "fault_at": _iso(self._fault_at),
            "fault_power_w": self._fault_power,
            "fault_reason": self._fault_reason,
            "proven_max_w": self._proven_max,
            "proven_max_at": _iso(self._proven_max_at),
        }

    # --------------------------------- error codes, derate, uptime tracking

    def _note_error_codes(self, now: float) -> None:
        """Firmware error codes (v2 get_error_code): notify new ones, arm the fault ceiling.

        A (code, timestamp) pair is notified once, when it first appears in
        the poll data; the firmware timestamps each occurrence, so a code
        that clears and returns notifies again. Every new pair is also fault
        evidence for the fault ceiling.
        """
        codes = self.coordinator.data.get("error_codes")
        if not isinstance(codes, dict):
            return
        pairs = {(str(c), str(t)) for c, t in codes.items()}
        new = sorted(pairs - self._error_pairs_prev)
        self._error_pairs_prev = pairs
        for code, ts in new:
            _LOGGER.warning("Miner firmware reports error %s (at %s)", code, ts)
            key = f"miner_error_{code}"
            self._error_notify_keys.add(key)
            self._notify(
                key,
                f"Miner reports error {code}",
                f"The miner firmware is reporting error code {code} (raised at {ts}). Look up the "
                "code in Whatsminer's API documentation or the miner web UI's error log, and see "
                "the Error Code sensor for everything currently active. The fault ceiling is "
                "armed: power commands now cap near what the miner has proven it can hold.",
            )
            self._note_fault(now, self._op_in_force or self._limit_in_force(), f"error code {code}")

    def _note_derate(self, is_mining: bool, now: float) -> None:
        """External-derate watch: reported limit well below what WE last sent.

        See DERATE_MIN_DELTA_W for the gates. The flag means "the miner runs
        under a limit nobody here commanded"; it clears after DERATE_POLLS
        polls back at the commanded value. When it fires the fault evidence
        notes the derated-TO power, so the ceiling converges onto what the
        hardware demonstrated.
        """
        try:
            reported = int(self.coordinator.data.get("wattage_limit") or 0)
        except (TypeError, ValueError):
            reported = 0
        commanded = self._limit_commanded
        if (
            not is_mining or commanded is None or reported <= 0
            or now - self._limit_commanded_at < DERATE_POST_WRITE_S
            or not self._tuned(now)
        ):
            self._derate_candidate_since = None
            self._derate_candidate_polls = 0
            return
        if reported < commanded - DERATE_MIN_DELTA_W:
            self._derate_match_polls = 0
            if self._derate_candidate_since is None:
                self._derate_candidate_since = now
            self._derate_candidate_polls += 1
            if (
                not self._external_derate
                and self._derate_candidate_polls >= DERATE_POLLS
                and now - self._derate_candidate_since >= DERATE_MIN_S
            ):
                self._external_derate = True
                _LOGGER.warning(
                    "Miner reports a %dW power limit but %dW was commanded and never re-sent — "
                    "the firmware derated itself (check the Error Code sensor); treating the "
                    "derated power as fault evidence", reported, commanded,
                )
                self._note_fault(now, reported, f"external derate to {reported}W (commanded {commanded}W)")
                self._publish_faults()
        else:
            self._derate_candidate_since = None
            self._derate_candidate_polls = 0
            if self._external_derate:
                self._derate_match_polls += 1
                if self._derate_match_polls >= DERATE_POLLS:
                    self._external_derate = False
                    self._derate_match_polls = 0
                    _LOGGER.info("Miner is back at the commanded %dW limit — external derate cleared", commanded)
                    self._publish_faults()

    def _note_uptime_and_crashes(self, is_mining: bool, now: float) -> None:
        """Track Elapsed; count confirmed uncommanded short runs as crashes.

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
                self._regression_power = None
        elif self._last_uptime is not None and uptime + 5 < self._last_uptime:
            self._pending_regression = _Regression(
                prev_uptime=self._last_uptime,
                seen_uptime=uptime,
                ours=self._pending_restart_is_ours(now),
                run_s=self._last_uptime - (self._idle_uptime or 0.0),
            )
            # The power the ending run held, before the restart re-points
            # _op_in_force at the limit: crash evidence refers to it.
            self._regression_power = self._power_in_force()
            self._settle_hold(now)
            self._note_btminer_restart(now)
        self._last_uptime = uptime
        if self._idle_uptime is not None and uptime < self._idle_uptime:
            self._idle_uptime = None  # btminer restarted since
        if not is_mining:
            self._idle_uptime = uptime if uptime > 0 else None
        self._note_hold(is_mining, now)

    def _count_restart(self, regression: _Regression, now: float) -> None:
        """A confirmed Elapsed regression: attribute it, and learn from it if a short run."""
        if regression.ours:
            self._pending_restart_at = None
        self._last_restart_ours = regression.ours
        power, self._regression_power = self._regression_power, None
        if power is None:
            power = self._power_in_force()
        if regression.ours or regression.run_s >= STABLE_RUN_S:
            # Longer runs end episodes naturally; our own restarts are not crashes.
            return
        if power is None or power <= 0:
            return  # no idea what it ran at: not evidence
        if "chip" in self._caps_active:
            # Restarts under chip over-temperature must not be answered with
            # more power.
            _LOGGER.warning(
                "Miner restarted on its own after hashing %.0fs at %dW while the chip-temp "
                "cap is active — not counted as a crash", regression.run_s, power,
            )
            return
        if self._supply_hot(now, self._current_temperature()):
            _LOGGER.info(
                "Miner restarted on its own after hashing %.0fs at %dW with the supply hot — "
                "not counted as a crash (may be thermal)", regression.run_s, power,
            )
            return
        if self._fault_at is not None and now - self._fault_at < FAULT_TTL_S:
            # Fault episode in progress: its restarts are the fault's story,
            # not floor evidence (2026-10-20 night: the 2320/561 derate loop
            # ratcheted the crash floor 1450 → 2241 W while evidence was armed).
            _LOGGER.warning(
                "Miner restarted on its own after hashing %.0fs at %dW during a fault episode "
                "(%s) — not counted toward the crash floor",
                regression.run_s, power, self._fault_reason,
            )
            return
        _LOGGER.warning(
            "Miner restarted on its own after hashing %.0fs at %dW — crash evidence",
            regression.run_s, power,
        )
        self._note_crash(power, now)

    # ----------------------------------------------------- restart plumbing

    def _mark_actuation(self) -> None:
        """We just sent a command that stops or restarts btminer."""
        self._pending_restart_at = time()

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

    # ------------------------------------------------------------------- PID

    def _null_pid_internals(self) -> None:
        self._pid_state.update(
            {
                "error": None, "proportional": None, "integral": None,
                "derivative": None, "external": None, "output": None,
                "requested_output": None, "supply_projected": None, "predictive": None,
            }
        )

    def _seed_bumpless_transfer(self) -> int:
        """Seed the PID integral so the first tick output ≈ current miner wattage.

        Under v3 the telemetry limit is the ceiling, not the power: seed from
        the power we regulate to (op_target). Unknown (first enable): the
        draw seeds the integral only, and the first command goes out
        unsuppressed; with no reading at all the integrator seeds from
        power_max (fail-warm) but the actuation reference stays unknown.
        """
        if self._op_target is not None:
            current_power, known = self._op_target, True
        else:
            try:
                draw = int(float(self.coordinator.data.get("wattage") or 0))
            except (TypeError, ValueError):
                draw = 0
            current_power, known = (min(draw, self._power_max) if draw > 0 else 0), False
        if not known and not current_power:
            current_power = self._power_max
        current_temp = self._current_temperature()
        target = self._target()
        first_tick_p = self._kp * (target - float(current_temp)) if current_temp is not None else 0.0
        self._pid.integral = float(current_power) - first_tick_p
        self._last_commanded_power = int(current_power) if known else None
        return int(current_power)

    def _reference(self, current_limit: int) -> int | None:
        """What we believe the miner is commanded to, or None when unknown."""
        if self._last_commanded_power is not None:
            return self._last_commanded_power
        return int(current_limit) if current_limit and current_limit > 0 else None

    def _draw_estimate(self) -> int | None:
        """With no commanded power yet: the measured draw.

        Only a starting point for the PID's base. Never a floor/crash
        belief: a draw sits a little under its target.
        """
        try:
            draw = int(float(self.coordinator.data.get("wattage") or 0))
        except (TypeError, ValueError):
            return None
        return min(draw, self._power_max) if draw > 0 else None

    def _fallback_power(self, summary: str, now: float) -> tuple[int | None, str]:
        """Open-loop power for when the supply probe is unavailable."""
        floor, ceiling = self._bounds(now)
        if summary == ds.ALL_IDLE:
            return floor, "no thermostat demand"
        outdoor = self._read_outdoor_fahrenheit()
        if outdoor is None or self._fallback_outdoor_warm <= self._fallback_outdoor_cold:
            return None, "no outdoor temperature"
        frac = (self._fallback_outdoor_warm - outdoor) / (
            self._fallback_outdoor_warm - self._fallback_outdoor_cold
        )
        frac = max(0.0, min(1.0, frac))
        return int(round(floor + (ceiling - floor) * frac)), f"outdoor {outdoor:.1f}°F"

    async def _run_fallback_step(self, caps: frozenset[str], summary: str, now: float) -> None:
        """Supply probe unavailable: run open-loop on outdoor temp and demand."""
        if not self._in_fallback:
            self._in_fallback = True
            _LOGGER.warning(
                "Supply probe %s unavailable — running on outdoor temperature and "
                "thermostat demand until it returns", self._external_sensor_id,
            )
        floor, ceiling = self._bounds(now)
        target, basis = self._fallback_power(summary, now)
        current_limit = self.coordinator.data.get("wattage_limit") or 0
        reference = self._reference(current_limit)
        if target is None and reference is None:
            self._pid_state["control_mode"] = "fallback"
            return  # nothing to go on and no known limit: hold whatever it is
        requested = target if target is not None else reference
        new_power = self._cap_clamp(caps) if caps else max(min(requested, ceiling), floor)
        demand_lockout = basis == "no thermostat demand"
        self._null_pid_internals()
        self._pid_state.update(
            {
                "requested_output": requested,
                "output": reference,
                "safety_engaged": bool(caps) or demand_lockout,
                "out_max_effective": ceiling,
                "out_min_effective": floor,
            }
        )
        self._pid_state["control_mode"] = (
            "safety_cap" if caps else ("demand_lockout" if demand_lockout else "fallback")
        )
        if reference is None:
            step_ok, interval_ok = True, True
        else:
            delta = abs(new_power - reference)
            interval = self._min_adjust_interval_increase if new_power > reference else self._min_adjust_interval
            step_ok = delta >= self._min_power_step
            interval_ok = now - self._last_command_time >= interval
        if not step_ok or not (interval_ok or caps):
            return
        if now < self._resume_hold_until and not caps:
            _LOGGER.debug("Fallback actuation held: miner booting after resume")
            return
        _LOGGER.info(
            "Fallback (%s): power %dW (was %s)", "safety cap" if caps else basis, new_power,
            f"{reference}W" if reference is not None else "unknown",
        )
        await self._command_power(new_power, safety=bool(caps), temp=None, caps=caps)

    # ------------------------------------------------------ power regulation

    def _limit_in_force(self) -> int | None:
        """The v2 limit (the ceiling): what we last sent, else the telemetry value."""
        if self._limit_w is not None:
            return self._limit_w
        try:
            limit = int(self.coordinator.data.get("wattage_limit") or 0)
        except (TypeError, ValueError):
            limit = 0
        return limit if limit > 0 else None

    def _tuned(self, now: float) -> bool:
        """up-freq-finish 1 on a fresh poll, and not just after our own write."""
        return (
            bool(self.coordinator.last_update_success)
            and self.coordinator.data.get("up_freq_finish") == 1
            and now >= self._tune_grace_until
        )

    def _v3_backed_off(self, now: float) -> bool:
        return now < self._v3_retry_at

    def _gate_write(self, now: float, *, safety: bool, kind: str) -> str | None:
        """The single write gate every power/service command passes through.

        Returns the suppression reason, or None when the write may go out.
        Non-safety writes are suppressed unless tuning has finished
        (up-freq-finish 1 on a fresh poll) and the one-non-safety-write
        budget of the current tuned epoch is unspent. Safety clamps and
        stop-class commands (power_off/lockout stops, resume-edge power_on)
        pass unconditionally — the safety paths stay exactly as capable as
        before the gate existed. Every suppression is INFO-logged (repeats
        of the same reason are DEBUG, to keep the 30 s poll quiet).
        """
        if safety or kind == "stop":
            return None
        data = self.coordinator.data
        if not self.coordinator.last_update_success or data.get("up_freq_finish") != 1:
            reason = f"{kind} suppressed: miner not tuned (up-freq-finish {data.get('up_freq_finish')})"
        elif self._epoch_write_used:
            reason = f"{kind} suppressed: the one non-safety write of this tuned epoch is already spent"
        else:
            return None
        if self._gate_last_suppression == (kind, reason):
            _LOGGER.debug("Write gate: %s", reason)
        else:
            self._gate_last_suppression = (kind, reason)
            _LOGGER.info("Write gate: %s", reason)
        return reason

    def _spend_epoch_write(self) -> None:
        """A non-safety write went out: the epoch's budget is spent."""
        self._epoch_write_used = True
        self._gate_last_suppression = None

    def _note_btminer_restart(self, now: float) -> None:
        """A start edge or Elapsed regression: the operating power is the limit until re-commanded."""
        self._op_established = False
        self._op_in_force = self._limit_in_force()
        self._op_in_force_at = now
        self._collapse_watch = None
        self._collapse_polls = 0
        # A new boot is a new tuned epoch: the write budget re-arms.
        self._epoch_write_used = False

    def _note_reported_limit(self, is_mining: bool, now: float) -> None:
        """Per fresh poll: the persistence rule for the telemetry limit, and tuning time."""
        data = self.coordinator.data
        try:
            reported = int(data.get("wattage_limit") or 0)
            uptime = float(data.get("uptime") or 0)
        except (TypeError, ValueError):
            reported, uptime = 0, 0.0
        if reported > 0 and reported == self._reported_limit_last:
            self._reported_limit_polls += 1
        else:
            self._reported_limit_last = reported
            self._reported_limit_polls = 1 if reported > 0 else 0
        if (
            self._limit_w is not None and reported > 0 and reported != self._limit_w
            and uptime >= BOOT_TRANSIENT_S and self._recently_sent_limit() is None
            # Should the v2 field ever mirror the operating power we set,
            # that is not a new limit: adopting it would re-raise the ceiling.
            and reported not in (self._op_target, self._op_in_force)
        ):
            self._limit_mismatch_polls += 1
            if self._limit_mismatch_polls >= LIMIT_PERSIST_POLLS:
                _LOGGER.info(
                    "Miner reports a %dW power limit for %d polls (we last sent %dW) — adopting it",
                    reported, self._limit_mismatch_polls, self._limit_w,
                )
                self._limit_w = reported
                self._limit_mismatch_polls = 0
                self._save_later()
        else:
            self._limit_mismatch_polls = 0
        if not is_mining or self._tuned(now):
            self._untuned_since = None
        elif self._untuned_since is None:
            self._untuned_since = now
        # Tuned-epoch edge: up-freq-finish leaving 1 (a new tune opened) and
        # coming back to 1 re-arms the one-non-safety-write budget of the
        # gate (_gate_write).
        if data.get("up_freq_finish") == 1:
            if not self._epoch_tuned_seen:
                self._epoch_tuned_seen = True
                self._epoch_write_used = False
        else:
            self._epoch_tuned_seen = False

    def _ceiling_ok(self, now: float, caps: frozenset[str], temp: float | None) -> str | None:
        """Why the ceiling may not be raised now (None: it may)."""
        if caps:
            return "a safety cap is engaged"
        if self._supply_hot(now, temp):
            return "the supply is hot"
        if self._v3_backed_off(now):
            return "set.miner.power is backed off after a rejection"
        if now < self._ceiling_hold_until:
            return "the ceiling is held down after a tuning timeout"
        if self._soft_start:
            return "the soft start owns increases"
        if (self._demand_entities and self._demand_summary == ds.ALL_IDLE) or self._shutoff.state == ds.DWELL:
            return "no zone is calling"
        return None

    async def _set_power_limit(
        self, new_power: int, why: str = "limit", op_target: int | None = None,
        safety: bool = False,
    ) -> bool:
        """v2 adjust_power_limit (restarts btminer). Returns True when sent.

        The limit is the ceiling in force (or the carrier of a safety clamp)
        and ``op_target`` the operating power the regulator wants (default:
        the limit itself). Any adjust_power_limit discards the set.miner.power
        record (live 2026-10-12), so the regulation layer is reset: the miner
        runs at the limit until _op_target is re-commanded once tuned. The
        write also resets the last-limit-we-sent derate reference and marks a
        clean run boundary for hold witnessing (settle the hold in progress,
        clear the fault taint).

        Every call passes the write gate (_gate_write): non-safety ceiling
        moves are suppressed while the miner tunes and after the epoch's one
        non-safety write; safety clamps (``safety=True``) always fly.
        """
        target = new_power if op_target is None else op_target
        if self._gate_write(time(), safety=safety, kind="adjust_power_limit") is not None:
            return False
        try:
            await self.coordinator.api.set_power_limit(new_power)
        except Exception as err:
            _LOGGER.error("Failed to set power limit to %dW: %s", new_power, err)
            return False
        if not safety:
            self._spend_epoch_write()
        self._mark_actuation()
        now = time()
        self._sent_limit = new_power
        self._sent_limit_at = now
        self._last_commanded_power = target
        self._last_command_time = now
        self._pid_state["output"] = target
        self._limit_w = new_power
        self._limit_mismatch_polls = 0
        self._limit_commanded = new_power
        self._limit_commanded_at = now
        self._derate_candidate_since = None
        self._derate_candidate_polls = 0
        self._derate_match_polls = 0
        self._op_target = target
        self._op_in_force = new_power
        self._op_in_force_at = now
        self._op_established = target >= new_power
        self._collapse_watch = None
        self._collapse_polls = 0
        self._command_boundary(now)
        self._last_power_command = f"adjust_power_limit {new_power} W ({why})"
        self._save_later()
        return True

    async def _command_power(
        self, new_power: int, safety: bool = False,
        temp: float | None = None, caps: frozenset[str] = frozenset(),
    ) -> None:
        """Deliver a power decision: set.miner.power, or a v2 ceiling move.

        The request is first clamped into the fault bounds. Then: above the
        limit in force, raise the ceiling to the bound ceiling when the loop
        can take a boot there (see _ceiling_ok) and deliver the power once
        tuned, else adjust_power_limit(new_power); still tuning: only safety
        clamps go out as adjust_power_limit, everything else is held as the
        target; tuned: set.miner.power, falling back to adjust_power_limit
        when the miner rejects it.
        """
        now = time()
        floor, ceiling = self._bounds(now)
        clamped = max(min(new_power, ceiling), floor)
        if clamped != new_power:
            _LOGGER.info(
                "Power command: %dW clamped to the %s bound %dW",
                new_power, "fault ceiling" if new_power > ceiling else "crash floor",
                ceiling if new_power > ceiling else floor,
            )
            new_power = clamped
        # The write gate owns both "still tuning" and the one-write-per-epoch
        # budget (this is the funnel for what used to be the not-tuned hold
        # below). A suppressed request is held as the target and delivered by
        # the re-establish path once the gate opens.
        if self._gate_write(now, safety=safety, kind="power") is not None:
            # Only the target moves: _last_commanded_power stays what was
            # delivered, so a safety clamp to the same value still fires.
            self._op_target = new_power
            if new_power != self._op_in_force:
                self._op_established = False
            return
        limit = self._limit_in_force()
        if limit is None or new_power > limit:
            why = self._ceiling_ok(now, caps, temp)
            if why is None and safety:
                # A safety clamp above a lower clamp in force: never answer it
                # with a boot toward the ceiling.
                why = "a safety clamp"
            if why is None:
                _LOGGER.info(
                    "Power regulation: %dW is above the %s limit — raising the ceiling to %dW "
                    "(one restart); %dW is commanded once tuning finishes",
                    new_power, f"{limit}W" if limit is not None else "unknown", ceiling, new_power,
                )
                await self._set_power_limit(ceiling, op_target=new_power, why="ceiling")
                return
            _LOGGER.info(
                "Power regulation: %dW is above the %s limit and the ceiling is not raised (%s) — "
                "adjust_power_limit", new_power, f"{limit}W" if limit is not None else "unknown", why,
            )
            await self._set_power_limit(new_power, why="increase", safety=safety)
            return
        if self._v3_backed_off(now):
            await self._set_power_limit(new_power, why="set.miner.power backed off", safety=safety)
            return
        if not self._tuned(now):
            # The gate already passed (tuned, budget unspent) but our own
            # write's grace window is still open: hold the target briefly.
            if safety:
                _LOGGER.info(
                    "Power regulation: %dW while the miner is still tuning — safety clamp via "
                    "adjust_power_limit (set.miner.power would only apply next boot)", new_power,
                )
                await self._set_power_limit(new_power, why="boot-window clamp", safety=True)
                return
            self._op_target = new_power
            if new_power != self._op_in_force:
                self._op_established = False
            return
        await self._send_v3_power(new_power, now, why="regulate")

    async def _send_v3_power(self, new_power: int, now: float, why: str) -> bool:
        """set.miner.power; on rejection fall back to adjust_power_limit. True when v3 carried it."""
        data = self.coordinator.data
        if self._gate_write(now, safety=False, kind="set.miner.power") is not None:
            return False  # held as the target; retried when the gate opens
        # Only a step between two regulated powers is watched for a collapse:
        # after a restart the "previous" power is the ceiling transient, and
        # undoing to it would pin the miner at power_max.
        prev = self._op_in_force if (why == "regulate" and self._op_established) else None
        try:
            await self.coordinator.api.set_miner_power(new_power)
        except Exception as err:  # noqa: BLE001 — every failure has a defined fallback
            code = getattr(err, "code", None)
            self._v3_error = f"{err}"
            if code is None:
                self._v3_transport_fails += 1
                if self._v3_transport_fails < V3_TRANSPORT_FAILS:
                    _LOGGER.error(
                        "set.miner.power %dW failed (%s) — retrying next tick (%d/%d before "
                        "falling back to adjust_power_limit)", new_power, err,
                        self._v3_transport_fails, V3_TRANSPORT_FAILS,
                    )
                    return False
            self._v3_transport_fails = 0
            self._v3_retry_at = now + V3_POWER_RETRY_S
            reason = f"set.miner.power {new_power} W failed ({err}); sent adjust_power_limit"
            _LOGGER.warning(
                "%s — the regulation layer is reset; set.miner.power is retried after %d min",
                reason, V3_POWER_RETRY_S // 60,
            )
            note = getattr(self.coordinator.api, "note_power_fallback", None)
            if note is not None:
                note(reason)
            if not self._power_fallback_notified:
                self._power_fallback_notified = True
                self._notify(
                    "power_fallback",
                    "Power change sent via adjust_power_limit",
                    f"{reason}. That command restarts mining. The controller tries "
                    f"set.miner.power again in {V3_POWER_RETRY_S / 60:.0f} min; check the API v3 "
                    "Super Password in Configure.",
                )
            await self._set_power_limit(new_power, why="fallback")
            return False
        self._v3_transport_fails = 0
        self._v3_error = None
        self._spend_epoch_write()
        if self._power_fallback_notified:
            self._power_fallback_notified = False
            persistent_notification.async_dismiss(self.hass, f"{DOMAIN}_power_fallback")
        self._tune_grace_until = now + V3_TUNE_GRACE_S
        self._op_target = new_power
        self._op_in_force = new_power
        self._op_in_force_at = now
        self._op_established = True
        self._last_commanded_power = new_power
        self._last_command_time = now
        self._pid_state["output"] = new_power
        self._command_boundary(now)
        self._last_power_command = f"set.miner.power {new_power} W ({why})"
        try:
            hashrate = float(data.get("hashrate_5s") or data.get("hashrate_1m") or 0)
            draw = float(data.get("wattage") or 0)
        except (TypeError, ValueError):
            hashrate = draw = 0.0
        if hashrate > 0 and draw > 0 and prev is not None and prev != new_power:
            self._collapse_watch = (now + V3_COLLAPSE_WATCH_S, hashrate / draw, int(prev), int(new_power))
            self._collapse_polls = 0
        _LOGGER.info("Power regulation: set.miner.power %dW (%s, no restart)", new_power, why)
        self._save_later()
        return True

    async def _regulation_tick(self, now: float, caps: frozenset[str], is_mining: bool) -> bool:
        """Hashing, fresh tick. True when it sent a restart-bearing command this tick.

        In order: the hashrate-collapse watch, the tuning timeout, a ceiling
        above the bound ceiling, and re-establishing the operating power
        after a start edge, a btminer restart or any adjust_power_limit.
        """
        try:
            data = self.coordinator.data
            if self._regulation_error is not None:
                # Self-clearing: the next successful stable read — hashing,
                # drawing power, hashrate per watt back above the threshold
                # the error was judged at.
                try:
                    err_h = float(data.get("hashrate_5s") or data.get("hashrate_1m") or 0)
                    err_w = float(data.get("wattage") or 0)
                except (TypeError, ValueError):
                    err_h = err_w = 0.0
                if is_mining and err_h > 0 and err_w > 0 and err_h / err_w >= (self._regulation_error_tpw or 0):
                    _LOGGER.info("Power regulation error cleared — hashrate per watt recovered")
                    self._regulation_error = None
                    self._regulation_error_tpw = None
            watch = self._collapse_watch
            if watch is not None:
                until, before_tpw, prev, stepped = watch
                try:
                    hashrate = float(data.get("hashrate_5s") or data.get("hashrate_1m") or 0)
                    draw = float(data.get("wattage") or 0)
                except (TypeError, ValueError):
                    hashrate = draw = 0.0
                if now > until:
                    self._collapse_watch = None
                elif is_mining and draw > 0:
                    if hashrate / draw < V3_COLLAPSE_FRAC * before_tpw:
                        self._collapse_polls += 1
                    else:
                        self._collapse_polls = 0
                    if self._collapse_polls >= V3_COLLAPSE_POLLS:
                        self._collapse_watch = None
                        self._collapse_polls = 0
                        # The collapse is crash evidence at the stepped-to
                        # power: that is the power the miner failed to hold.
                        self._note_crash(stepped, now)
                        # Notify-only: the step is NOT undone. An undoing
                        # adjust_power_limit would answer a documented side
                        # effect of one dynamic write (a tuning event) with a
                        # certain restart (synthesis: "setpoints in, overrides
                        # out" — derates/collapses are verdicts, not servo
                        # error). The operator sees the collapse and decides.
                        _LOGGER.warning(
                            "Hashrate collapsed after set.miner.power %dW (%.1f TH at %.0fW, under %.0f%% of "
                            "the %.3f TH/kW before) — observed, not undone (no adjust_power_limit)",
                            stepped, hashrate, draw, 100 * V3_COLLAPSE_FRAC, 1000 * before_tpw,
                        )
                        self._notify(
                            "power_collapse",
                            "Hashrate collapsed after a power reduction",
                            f"After set.miner.power lowered the miner from {prev} W to {stepped} W its "
                            f"hashrate fell to {hashrate:.1f} TH at {draw:.0f} W. The controller left "
                            f"the power in place (an undoing power-limit write would restart mining) "
                            f"and raised its fault floor to {self._crash_floor} W while the evidence "
                            "is fresh. Press Reset Fault Memory to re-test lower powers.",
                        )
                        self._regulation_error = "collapse-observed-no-undo"
                        self._regulation_error_tpw = V3_COLLAPSE_FRAC * before_tpw
                        self._publish_faults()
                        return False
            floor, ceiling = self._bounds(now)
            target = self._op_target
            tuned = self._tuned(now)
            if (
                not caps and self._untuned_since is not None
                and now - self._untuned_since >= V3_TUNE_TIMEOUT_S
                and target is not None and self._op_in_force is not None
                and target < self._op_in_force - self._min_power_step_fine
            ):
                # The limit write is a non-safety ceiling move: the write gate
                # holds it while the miner tunes (up-freq-finish != 1). When
                # gated, try again next tick — once tuning finishes, the
                # re-establish path delivers the wanted power anyway.
                if self._gate_write(now, safety=False, kind="adjust_power_limit") is not None:
                    return False
                _LOGGER.warning(
                    "Miner still tuning %.0f min after its last start or power change, running toward "
                    "%dW instead of %dW — sending %dW as the limit; the ceiling stays down for %.0f h",
                    (now - self._untuned_since) / 60, self._op_in_force, target, target,
                    V3_CEILING_BACKOFF_S / 3600,
                )
                self._untuned_since = None
                self._ceiling_hold_until = now + V3_CEILING_BACKOFF_S
                return await self._set_power_limit(target, why="tuning timeout")
            if not tuned or caps:
                return False  # caps: the PID step clamps this tick
            limit = self._limit_in_force()
            if limit is not None and limit > ceiling and now >= self._resume_hold_until:
                # Power Max (or an armed fault ceiling) was lowered: the old
                # ceiling would let every boot tune above it. Once,
                # deliberately, while the miner is tuned.
                want = min(target if target is not None else ceiling, ceiling)
                if ceiling < self._power_max:
                    _LOGGER.info(
                        "Power regulation: lowering the ceiling %dW to the fault ceiling %dW",
                        limit, ceiling,
                    )
                else:
                    _LOGGER.info("Power regulation: lowering the ceiling %dW to Power Max %dW", limit, ceiling)
                return await self._set_power_limit(ceiling, op_target=want, why="ceiling lowered")
            if self._op_established:
                return False
            if target is None:
                self._op_established = True  # nothing commanded yet: the PID's first command is the target
                return False
            target = max(min(target, ceiling), floor)
            if target == self._op_in_force:
                self._op_established = True  # already delivered since the last restart
                return False
            if self._pinned:
                # The believed operating point: the sent power, else the limit
                # in force (_power_in_force) — a fresh install knows the miner
                # only by its limit, and that is what gets reconciled.
                believed = self._power_in_force()
                if believed is not None and abs(target - believed) < PINNED_RECONCILE_W:
                    # Set-and-hold: inside the reconcile deadband the believed
                    # operating point is close enough to the pinned target — a
                    # write here would churn the tuner for <25 W.
                    self._op_established = True
                    return False
            if limit is not None and target >= limit:
                self._op_established = True  # it runs at the limit, which is what we want
                self._op_in_force = limit
                return False
            if self._v3_backed_off(now):
                return await self._set_power_limit(target, why="re-establish, set.miner.power backed off")
            _LOGGER.info("Power regulation: tuning finished — re-commanding %dW", target)
            await self._send_v3_power(target, now, why="re-establish")
            return False
        except Exception:  # never let regulation bookkeeping break the tick
            _LOGGER.exception("Power regulation tick failed")
            return False

    def _publish_regulation(self) -> None:
        now = time()
        self._pid_state["power_command"] = {
            "last_command": self._last_power_command,
            "up_freq_finish": self.coordinator.data.get("up_freq_finish"),
            "tuned": self._tuned(now),
            "limit_w": self._limit_in_force(),
            "regulated_power_w": self._op_target,
            "established": self._op_established,
            "v3_error": self._v3_error,
            "regulation_error": self._regulation_error,
            "v3_retry_at": _iso(self._v3_retry_at) if self._v3_backed_off(now) else None,
            "ceiling_held_until": _iso(self._ceiling_hold_until) if now < self._ceiling_hold_until else None,
        }

    # ------------------------------------------------------------ prediction

    def _note_supply(self, now: float, temp: float | None) -> None:
        samples = self._supply_samples
        if temp is not None:
            if samples and abs(float(temp) - samples[-1][1]) > SUPPLY_JUMP_F:
                samples.clear()  # probe glitch or reconnect, not a trend
            samples.append((now, float(temp)))
        while samples and now - samples[0][0] > SLOPE_WINDOW_S:
            samples.popleft()

    def _supply_slope(self) -> float | None:
        """Least-squares supply slope (°F/min) over SLOPE_WINDOW_S, or None if too few samples."""
        samples = self._supply_samples
        if len(samples) < 3 or samples[-1][0] - samples[0][0] < SLOPE_MIN_SPAN_S:
            return None
        n = len(samples)
        mean_t = sum(t for t, _ in samples) / n
        mean_v = sum(v for _, v in samples) / n
        var = sum((t - mean_t) ** 2 for t, _ in samples)
        if var <= 0:
            return None
        return 60.0 * sum((t - mean_t) * (v - mean_v) for t, v in samples) / var

    def _arm_soft_start(self, why: str) -> None:
        if not self._soft_start:
            _LOGGER.info("Soft start armed (%s) — increases stepped until the supply nears target", why)
        self._soft_start = True

    def _soft_start_power(
        self, base: int, error: float, slope: float | None, projected: float, target: float,
        boot_hold: bool, now: float,
    ) -> int:
        """The power the soft start allows this tick: hold base, or one step up."""
        # Spaced from our last ceiling write; after a resume the boot hold is
        # the wait. No step without a slope: we must see the supply isn't rising.
        spacing = max(PREDICT_HORIZON_S, float(self._min_adjust_interval_increase))
        if boot_hold or slope is None or projected >= target or now - self._sent_limit_at < spacing:
            return base
        span = max(0.0, self._pid.out_max - self._pid.out_min)
        step = min(SOFT_START_STEP_FRAC * span, max(self._kp * (target - projected), float(self._min_power_step)))
        return int(round(min(float(base) + step, self._pid.out_max)))

    async def _pinned_step(
        self, now: float, temp: float | None, caps: frozenset[str], summary: str, shutoff: ds.ShutoffState
    ) -> None:
        """Set-and-hold: the configured band collapsed below the fine step.

        The PID is bypassed entirely — inside a sub-step band it could only
        re-issue tune-churning writes for a few watts (the synthesis verdict:
        the worst point on the tradeoff curve). The fixed target is Power
        Min; the re-establish path in _regulation_tick reconciles toward it
        through the tuned v3 path, at most once per tuned epoch (the write
        gate) and only when the believed operating point differs by at least
        PINNED_RECONCILE_W. Safety keeps its authority: a cap clamp below
        the believed point still flies via adjust_power_limit with the same
        step rule as the PID path's safety fire (the fine step is the most
        permissive band, so it is at least as capable).
        """
        floor, ceiling = self._bounds(now)
        target = max(min(self._power_min, ceiling), floor)
        new_power = self._cap_clamp(caps) if caps else target
        safety_engaged = bool(caps)
        mode = "safety_cap" if caps else "pid"

        # Demand lockout / shutoff dwell keep their telemetry labels (and the
        # shutoff's own stops are unchanged); with the target already at
        # Power Min there is nothing left for them to clamp.
        if self._demand_entities and summary == ds.ALL_IDLE:
            safety_engaged = True
            if mode == "pid":
                mode = "demand_lockout"
            if not self._no_demand_logged:
                _LOGGER.warning(
                    "No demand from %s — pinned at %dW until a thermostat calls",
                    ", ".join(self._demand_entities), target,
                )
                self._no_demand_logged = True
        elif self._no_demand_logged:
            _LOGGER.info("Demand returned — releasing no-demand lockout")
            self._no_demand_logged = False
        if shutoff.state == ds.DWELL:
            mode = "dwell"
            safety_engaged = True
            new_power = min(floor, new_power) if caps else floor
        self._pid_state["control_mode"] = mode

        # Telemetry keeps publishing (pid_mode="pinned" marks the bypass).
        if temp is not None:
            if self._slope_last_pv is not None and self._slope_ewma_tau_s > 0:
                prev_t, prev_v = self._slope_last_pv
                dt = now - prev_t
                if dt > 0:
                    inst = (temp - prev_v) / (dt / 60.0)
                    alpha = 1 - math.exp(-dt / self._slope_ewma_tau_s)
                    self._slope_ewma = (
                        inst if self._slope_ewma is None else self._slope_ewma + alpha * (inst - self._slope_ewma)
                    )
            self._slope_last_pv = (now, temp)
        slope = self._supply_slope()
        self._pid_state.update(
            {
                "error": (self._target() - float(temp)) if temp is not None else None,
                "proportional": self._pid.proportional,
                "integral": self._pid.integral,
                "derivative": self._pid.derivative,
                "external": self._pid.external,
                "output": self._last_commanded_power if self._last_commanded_power is not None else new_power,
                "requested_output": target,
                "safety_engaged": safety_engaged,
                "pv_slope": self._slope_ewma,
                "supply_slope": None if slope is None else round(slope, 3),
                "supply_projected": None if temp is None or slope is None else round(
                    float(temp) + max(slope, 0.0) * PREDICT_HORIZON_S / 60.0, 2
                ),
                "predictive": None,
                "soft_start": False,
                "out_max_effective": ceiling,
                "out_min_effective": floor,
            }
        )

        # Hold the pinned target; _regulation_tick's re-establish path
        # delivers it once tuned (write gate: at most once per epoch). The
        # believed operating point is the sent power, else the limit in force
        # (_power_in_force) — with nothing sent yet, an unknown operating
        # point still warrants the one reconcile write.
        self._op_target = target
        believed = self._power_in_force()
        if believed is None or abs(target - believed) >= PINNED_RECONCILE_W:
            self._op_established = False

        # Safety clamp actuation — the PID path's safety fire, minus the PID.
        reference = self._reference(self.coordinator.data.get("wattage_limit") or 0)
        if reference is None:
            reference = -10_000  # no idea what it runs at: one command beats guessing
        if caps and abs(new_power - reference) >= self._min_power_step_fine:
            _LOGGER.info(
                "Pinned: safety cap (%s) — clamping to %dW (was %dW)",
                ", ".join(sorted(caps)), new_power, reference,
            )
            await self._command_power(new_power, safety=True, temp=temp, caps=caps)

    async def _run_pid_step(
        self, temp: float | None, caps: frozenset[str], summary: str, shutoff: ds.ShutoffState
    ) -> None:
        """Compute PID output and push to the miner if it changed meaningfully."""
        now = time()
        if self._pinned:
            await self._pinned_step(now, temp, caps, summary, shutoff)
            return
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

        # The fault bounds are the absolute rails. The price/surplus envelope
        # can only pull out_max further down. Order of precedence:
        # safety caps > demand lockout > tou/surplus envelope > PID output.
        floor, ceiling = self._bounds(now)
        self._pid.out_min = float(floor)
        self._pid.out_max = float(ceiling)
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
        except Exception as err:  # defensive — don't break the coordinator loop
            _LOGGER.exception("PID calculation failed: %s", err)
            return

        sat_tol = 1.0
        error = target - float(temp)
        on_low_rail = output <= float(floor) + sat_tol
        # Directional: freeze the integral only when it would wind further
        # into the rail the output already sits on.
        output_saturated = (output >= self._pid.out_max - sat_tol and error > 0) or (
            on_low_rail and error < 0
        )
        if self._integral_band > 0 and error_abs > self._integral_band and output_saturated:
            self._pid.integral = integral_snapshot
            output = self._pid.proportional + integral_snapshot + self._pid.derivative + self._pid.external
            output = max(min(output, self._pid.out_max), float(floor))
            self._pid._output = output
        elif on_low_rail and error > 0:
            # On the low rail with the supply below target. The vendored PID
            # never integrates while its last output sits on a rail, so with a
            # flat or rising supply nothing would ever lift it off. Nudge it
            # just above so the integral can run next tick (a sub-watt change
            # commands nothing).
            self._pid._output = float(floor) + sat_tol + 0.01

        self._last_input_time = now
        if not did_calc:
            return

        requested_power = int(round(output))
        new_power = requested_power
        current_limit = self.coordinator.data.get("wattage_limit") or 0
        reference = self._reference(current_limit)
        base = reference if reference is not None else (self._draw_estimate() or floor)
        boot_hold = now < self._resume_hold_until

        # Prediction and soft start (see PREDICT_HORIZON_S, SOFT_START_STEP_FRAC).
        slope = self._supply_slope()
        rising = slope is not None and slope >= PREDICT_MIN_SLOPE_F_MIN
        projected = float(temp) + (max(slope, 0.0) * PREDICT_HORIZON_S / 60.0 if slope is not None else 0.0)
        if self._soft_start and error <= self._fine_step_band:
            self._soft_start = False
            # Bumpless hand-back: the PID resumes from the power in force.
            self._pid.integral = float(base) - self._pid.proportional - self._pid.derivative - self._pid.external
            integral_snapshot = self._pid.integral
            output, requested_power, new_power = float(base), base, base
            _LOGGER.info("Soft start done at %.1f°F — PID resumes from %dW", temp, base)
        predictive = None
        stagnant = (bool(self._demand_entities) and summary == ds.ALL_IDLE) or shutoff.state == ds.DWELL
        if not caps:
            if rising and (projected >= self._supply_temp_safety_cap or (stagnant and projected >= target)):
                # Headed for the cap, or still climbing with no zone flow to
                # carry the heat away: the clamp below would wait out the
                # decrease interval with the heat already in the pipe.
                new_power, predictive = floor, "back_off"
            else:
                if self._soft_start:
                    new_power = self._soft_start_power(base, error, slope, projected, target, boot_hold, now)
                if rising and projected >= target:
                    # Cut Kp × the overshoot the horizon predicts from the
                    # power in force; never raise.
                    pred = int(round(base - self._kp * (projected - target)))
                    hold = min(base, max(pred, floor))
                    if hold < new_power:
                        new_power, predictive = hold, ("hold" if hold >= base else "pull_back")
                settle = self._fine_step_band / 2
                if (
                    predictive is None and not self._soft_start and not stagnant and reference is not None
                    and abs(error) <= settle and abs(target - projected) <= settle
                    and abs(new_power - base) < self._min_power_step_medium
                ):
                    # Settled: a fine nudge now is a command spent on ±1°F of
                    # probe dither. Hold, and hold the integral both ways.
                    new_power, predictive = base, "settled"

        safety_engaged = bool(caps)
        mode = "pid"
        if safety_engaged:
            new_power = self._cap_clamp(caps)
            mode = "safety_cap"

        # Demand lockout: no thermostat calling → zone pumps idle → stagnant
        # primary loop. Force the floor and engage safety. The shutoff dwell
        # runs on top of this clamp. Neither may lift output above an engaged
        # cap's clamp, and the fault floor can sit above Power Min.
        if self._demand_entities and summary == ds.ALL_IDLE:
            new_power = min(floor, new_power) if caps else floor
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
            new_power = min(floor, new_power) if caps else floor
        self._pid_state["control_mode"] = mode

        # Anti-windup. Under the soft start the PID tracks the power in force
        # (its integral, frozen near the floor since the resume, would
        # otherwise ask for less than the miner already runs at), so the
        # hand-back is bumpless. Otherwise no upward integration while
        # anything holds the output below what the PID asks (cap, lockout,
        # dwell, prediction) or the boot hold keeps the power from following
        # it; the integral may still fall.
        if self._soft_start:
            self._pid.integral = float(base) - self._pid.proportional - self._pid.derivative - self._pid.external
            self._pid._output = max(min(float(base), self._pid.out_max), float(floor) + sat_tol + 0.01)
        elif predictive == "settled" or (
            (new_power < requested_power or boot_hold) and self._pid.integral > integral_snapshot
        ):
            self._pid.integral = integral_snapshot
            output = self._pid.proportional + integral_snapshot + self._pid.derivative + self._pid.external
            output = max(min(output, self._pid.out_max), float(floor))
            self._pid._output = max(output, float(floor) + sat_tol + 0.01) if error > 0 else output
            requested_power = int(round(output))

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
                "supply_slope": None if slope is None else round(slope, 3),
                "supply_projected": round(projected, 2),
                "predictive": predictive,
                "soft_start": self._soft_start,
            }
        )

        # Actuation gate: magnitude, time, and the post-resume boot hold.
        # Safety caps and the predictive back-off bypass the time gates and
        # the boot hold (they fire too late otherwise).
        was = f"{reference}W" if reference is not None else "unknown"
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
        if predictive == "pull_back":
            # Replaces the larger cut the decrease interval would make later.
            effective_interval = min(effective_interval, self._min_adjust_interval_increase)
        interval_ok = elapsed >= effective_interval
        safety_fire = bool(caps) and step_ok
        backoff_fire = predictive == "back_off" and step_ok and new_power < reference

        if not safety_fire and not backoff_fire and (not (step_ok and interval_ok) or boot_hold):
            _LOGGER.debug(
                "PID actuation throttled: Δ=%dW (need %dW, %s band), elapsed=%.0fs (need %ds)%s",
                delta, effective_min_step, band_label, elapsed, effective_interval,
                ", boot hold" if boot_hold else "",
            )
            self._pid_state["output"] = self._last_commanded_power
            return

        if backoff_fire:
            _LOGGER.warning(
                "Supply %.1f°F rising %.2f°F/min projects %.1f°F ≥ cap %.1f°F within %d min — "
                "backing off to %dW now", temp, slope, projected, self._supply_temp_safety_cap,
                PREDICT_HORIZON_S // 60, new_power,
            )
            self._arm_soft_start("predictive back-off")
        _LOGGER.info(
            "PID: temp=%.1f°F target=%.1f°F → power %dW (was %s, err=%.2f)",
            temp, target, new_power, was, self._pid.error,
        )
        await self._command_power(new_power, safety=safety_fire or backoff_fire, temp=temp, caps=caps)


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")
