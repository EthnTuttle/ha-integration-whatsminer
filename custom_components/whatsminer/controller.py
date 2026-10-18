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
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import TemperatureConverter

from . import autotune as at
from . import demand_shutoff as ds
from .const import (
    CONF_CHIP_TEMP_SAFETY_CAP,
    CONF_CONTROL_STRATEGY,
    CONF_DAILY_PLAN_HOUR,
    CONF_EXTERNAL_TEMP_SENSOR,
    CONF_FREEZE_GUARD_FORECAST_HOURS,
    CONF_FREEZE_GUARD_SENSOR,
    CONF_FREEZE_GUARD_THRESHOLD,
    CONF_PID_AUTOTUNE_MODE,
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
    CONF_POWER_REGULATION,
    CONF_PROVEN_CEILING,
    CONTROL_STRATEGIES,
    DAILY_TRIM_BAND,
    DEFAULT_CHIP_TEMP_SAFETY_CAP,
    DEFAULT_CONTROL_STRATEGY,
    DEFAULT_DAILY_PLAN_HOUR,
    DEFAULT_FREEZE_GUARD_FORECAST_HOURS,
    DEFAULT_FREEZE_GUARD_THRESHOLD,
    DEFAULT_PID_AUTOTUNE_MODE,
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
    DEFAULT_POWER_REGULATION,
    DEFAULT_PROVEN_CEILING,
    DOMAIN,
    FREEZE_GUARD_RELEASE_HYSTERESIS,
    POWER_REGULATION_MODES,
    POWER_REGULATION_V3,
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
# Predictive limiting. Every limit change restarts btminer and the supply
# answers it late, so the actuation gate looks PREDICT_HORIZON_S ahead along
# the supply's least-squares slope over SLOPE_WINDOW_S (needs SLOPE_MIN_SPAN_S
# of samples). The horizon is the pessimistic miner-to-probe lag: a loop with
# zones calling answers a cut in 2-4 min, but zones satisfying mid-climb
# leave it stagnant while the heat already sent keeps arriving for ~10-15
# min (2026-10-08: resumes from ~88°F at 3.5-4.2 kW peaked at 124-130°F).
# Only a slope of at least PREDICT_MIN_SLOPE_F_MIN counts as rising, so probe
# quantisation (0.1125°F) near setpoint never projects anything.
#   rising, projection ≥ target: no increase; the PID re-run on the projected
#     error may pull back through the normal gates, on the shorter (increase)
#     interval since it replaces a larger cut later.
#   settled (error and projection both within half the fine band): no
#     fine-sized change at all, integral held; fine nudges there were
#     restarts spent on probe dither.
#   rising, projection ≥ supply cap, or ≥ target with every zone idle (or
#     the shutoff dwelling): command the floor now, bypassing the interval
#     and boot hold like a cap (the cap and the idle clamp fire too late for
#     the heat already in the pipe), and re-arm the soft start.
PREDICT_HORIZON_S = 720.0
SLOPE_WINDOW_S = 300.0
SLOPE_MIN_SPAN_S = 120.0
PREDICT_MIN_SLOPE_F_MIN = 0.2
# A change larger than this between two samples (the Scout reports once a
# minute; the fastest real climb is ~3.3°F/min) is a probe glitch or
# reconnect: restart the slope window instead of projecting it.
SUPPLY_JUMP_F = 5.0
# Soft start. After a start we did not cause with a limit change (demand-
# shutoff resume, Mining Control on, lockout reset, firmware restart), an HA
# restart onto a cold loop, or a predictive back-off, the soft start owns
# increases: hold the limit through the boot hold, then step up only while the
# projection is short of target, at most once per PREDICT_HORIZON_S (or the
# increase interval if longer), by Kp × that shortfall capped at
# SOFT_START_STEP_FRAC of the floor..power_max span. The integral is frozen meanwhile and re-seeded
# from the limit in force once the supply is within the fine band of target.
# Never forces a decrease and never blocks floor enforcement.
SOFT_START_STEP_FRAC = 0.5
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
# Hardware faults are not floor evidence. A short run at a limit at or above
# one proven holdable (a FLOOR_STABLE_S run) within FLOOR_PROVEN_TTL_S means
# the miner held as much or less power recently: the restart is intermittent
# or a hardware fault, and more power will not fix it. 2026-10-11: an M64 with
# hashboard codes 561/563 restarted every 4-9 min at every limit from 1200 to
# 3500 W, and learning read each pair of short runs as "unholdable", ratcheting
# the floor 1200 -> 2700 W. Error codes alone can't tell the cases apart: the
# genuinely too low 1000 W of 2026-10-07 also showed 560-563, and learning
# fixed it (2200 W then held 89 min). Such restarts are counted as fault short
# runs; FLOOR_SHORT_RUNS_TO_LEARN of them notify "hardware fault suspected"
# once and never move the floor. Above the ceiling the restart-loop report
# below already covers it. The proof's timestamp is refreshed at most every
# FLOOR_PROVEN_REFRESH_S while a run keeps holding (each refresh is a store
# write); a proof restored without one (pre-1.10) counts as stale.
#
# When learning would pass the ceiling, the episode has shown that more power
# does not help: the floor reverts to what it was before the episode's first
# raise, and a persisted hardware-fault flag keeps floor learning and floor
# enforcement inert until a run hashes for FLOOR_STABLE_S (the episode end).
FLOOR_PROVEN_TTL_S = 7 * 86400.0
FLOOR_PROVEN_REFRESH_S = 3600.0
# Re-send a floor enforcement command no sooner than this if the send failed.
FLOOR_FIRE_RETRY_S = 60.0
# Every safety cap (chip, supply, freeze-guard hold over the lockout) clamps
# to the configured power_min even when a higher floor has been learned: the
# alternative to an intermittent crash loop is up to FLOOR_MAX_RAISE_W more
# watts into chips over temperature or a loop already past its cap, and the
# crash loop dissipates less heat. 2026-10-07: a supply cap that clamped to the
# learned floor, fed by restarts counted at 123°F supply, drove the miner to
# 2500 W until the 140°F lockout tripped.
#
# Restarts while the supply is hot are not floor evidence (the miner may be
# restarting on heat, not on an unholdable limit). Hot: the supply cap or the
# freeze-guard hold over the lockout is engaged, the supply is within
# SUPPLY_HOT_MARGIN_F of the supply cap (the S-trigger dwell's release band),
# or it was within SUPPLY_HOT_WINDOW_S ago. The window equals FLOOR_STABLE_S,
# so a short run that ended inside it may have begun hot.
SUPPLY_HOT_MARGIN_F = 5.0
SUPPLY_HOT_WINDOW_S = FLOOR_STABLE_S
# Power Regulation set_miner_power (1.11.0; docs/v1.11-power-regulation-plan.md).
# v2 adjust_power_limit restarts btminer on every change; v3 set.miner.power
# moves the operating power with no restart, but only once frequency tuning
# has finished (summary up-freq-finish 1). Verified on the M64 2026-10-12:
# 1884 -> 1244 W in ~80 s, elapsed counting, up-freq-finish 0 within 16 s of
# the write (/home/ethan/network/power-test-20261012-220429.log).
#
# The limit becomes a ceiling at power_max. It is raised only when the
# regulator asks for more than the limit in force (the moment the legacy path
# would have restarted btminer anyway) and the loop can take a boot at the
# ceiling: no cap, supply not hot, some zone calling, no shutoff dwell, soft
# start finished, set.miner.power not in a rejection back-off. Otherwise that
# increase goes out as adjust_power_limit, exactly as before. A ceiling above
# power_max (power_max lowered) is lowered once the miner is tuned.
#
# Boot window (up-freq-finish != 1, unknown, or within V3_TUNE_GRACE_S of our
# own write): set.miner.power would only apply at the next boot, so the
# supply/chip caps and the predictive back-off keep the legacy limit clamp
# (their authority is unchanged); every other change, floor enforcement
# included, is held and delivered once tuned. Every mining start edge and
# btminer restart re-commands the desired operating power once tuned again
# (API < 3.0.3 resets it to the limit on restart; 3.0.3+ persists it; the
# re-send is right either way). Any adjust_power_limit discards the operating
# power, so after one the regulation layer is re-established the same way.
V3_TUNE_GRACE_S = 90.0
# Still tuning this long after a start or our write, with the miner above the
# power we want (it tunes toward the ceiling): give up on the ceiling, send the
# wanted power as the limit, and keep the ceiling down for V3_CEILING_BACKOFF_S.
V3_TUNE_TIMEOUT_S = 2700.0
V3_CEILING_BACKOFF_S = 6 * 3600.0
# set.miner.power answered a non-zero code after the unlock retry: that change
# goes out as adjust_power_limit, and v3 is retried after this long. Transport
# failures (4433 not answering) fall back after this many in a row.
V3_POWER_RETRY_S = 1800.0
V3_TRANSPORT_FAILS = 3
# Hashrate collapse after a set.miner.power step: hashrate per watt below this
# fraction of the pre-step value for V3_COLLAPSE_POLLS polls in a row, within
# V3_COLLAPSE_WATCH_S of the step. Live, a 1884 -> 1244 W step kept ~85% of
# the pre-step TH/W through the ramp. The step is undone with
# adjust_power_limit and, for a decrease, the pre-step power becomes the
# lowest power set.miner.power may command (cleared by Reset Learned Floor).
V3_COLLAPSE_FRAC = 0.5
V3_COLLAPSE_POLLS = 3
V3_COLLAPSE_WATCH_S = 1800.0
# The telemetry power limit is only believed when it persists: it differs from
# what we last sent for LIMIT_PERSIST_POLLS polls in a row and btminer has
# been up BOOT_TRANSIENT_S. 2026-10-12: ~40 s after an uncommanded restart the
# M64 reported a derated 1905 W under a 1950 W floor and floor enforcement
# answered with adjust_power_limit(1950), a restart mid-regulation.
LIMIT_PERSIST_POLLS = 3
BOOT_TRANSIENT_S = 120.0
# External-derate detection (1.14): the miner/firmware lowered the reported
# wattage limit without us commanding it (2026-10-17: firmware error 2320 and
# a silent derate to 1224 W while the plan believed 2649 W). Armed only when
# the gap is real (more than DERATE_MIN_DELTA_W below the last limit WE
# sent), not inside the DERATE_POST_WRITE_S window after our own write (a
# legacy write restarts btminer and the report lags), not while the miner is
# stopped (intentional stop) or still tuning, and sustained for DERATE_POLLS
# polls spanning at least DERATE_MIN_S. Clears after DERATE_POLLS polls back
# at the commanded limit.
DERATE_MIN_DELTA_W = 100
DERATE_POST_WRITE_S = 90.0
DERATE_MIN_S = 120.0
DERATE_POLLS = 3
# Proven ceiling (1.14; see CONF_PROVEN_CEILING): once armed by fault
# evidence, the plan and v2 ceiling raises cap at the highest witnessed hold
# plus PROVEN_MARGIN_W, or PROVEN_MARGIN_W under the fault power when no
# hold has been witnessed.
PROVEN_MARGIN_W = 250
# After a user's Mining Control OFF, ignore lingering "mining" readings for this
# long so the PID can't send a limit change to a miner that is powering down.
USER_OFF_GRACE_S = 180.0
# Hourly forecast cache (met.no updates hourly), and how long a stale cache is
# still used when the forecast service keeps failing.
FORECAST_CACHE_S = 900.0
FORECAST_STALE_MAX_S = 6 * 3600.0
# Hourly outdoor sample spacing for the centred 24 h mean.
OUTDOOR_SAMPLE_INTERVAL_S = 3600.0
# Daily strategy (CONF_CONTROL_STRATEGY). Once a day at the plan hour (local),
# and at startup without a valid plan, one power level is computed and held:
#
#   P_ff  = P_avg24 · (T_bal − T_fc24) / max(T_bal − T_out24, DAILY_MIN_SPAN_F)
#   P_fb  = DAILY_KP_W_PER_F · (target − S_avg24)
#   P_day = clamp(round(P_ff + P_fb, DAILY_ROUND_W), floor, power_max)
#           (the 1.10 ±750 W/day plan step was removed in 1.13.0)
#
#   P_avg24  mean miner input power over the past 24 h (not mining = 0 W)
#   T_out24  mean outdoor temperature over the past 24 h
#   T_fc24   mean hourly forecast temperature over the next 24 h
#   S_avg24  mean supply over the past 24 h while mining
#   T_bal    DAILY_BALANCE_F, the outdoor temperature needing no heat
#
# Yesterday's power, scaled by how much colder or warmer tomorrow is forecast
# relative to the house's balance point, is the feedforward; yesterday's mean
# supply error trims it. Without outdoor or forecast data (fewer than
# DAILY_MIN_HOURS of either) P_ff = P_avg24; without supply samples P_fb = 0.
# Samples are hourly aggregates (persisted). With fewer than DAILY_MIN_HOURS
# hours of power samples the plan is the limit in force (no change) and is
# retried hourly. The supply tops out near 115-119°F with the dry cooler
# holding the coolant at ~123°F, and heat over need goes to the garage, so a
# plan a little high costs nothing but power, while every change costs a
# btminer restart. A plan within DAILY_DEADBAND_W of the limit in force is not
# sent.
DAILY_BALANCE_F = 65.0
DAILY_MIN_SPAN_F = 5.0
DAILY_KP_W_PER_F = 60.0
DAILY_DEADBAND_W = 100
DAILY_ROUND_W = 50
DAILY_MIN_HOURS = 12
DAILY_WINDOW_S = 24 * 3600.0
DAILY_RETRY_S = 3600.0

STORE_VERSION = 1


class _Regression(NamedTuple):
    """An Elapsed regression awaiting confirmation by the next poll."""

    prev_uptime: float
    seen_uptime: float
    limit: int | None
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
        # Control strategy (see DAILY_BALANCE_F)
        strategy = str(g(CONF_CONTROL_STRATEGY, DEFAULT_CONTROL_STRATEGY))
        self._strategy = strategy if strategy in CONTROL_STRATEGIES else DEFAULT_CONTROL_STRATEGY
        try:
            self._daily_plan_hour = int(g(CONF_DAILY_PLAN_HOUR, DEFAULT_DAILY_PLAN_HOUR)) % 24
        except (TypeError, ValueError):
            self._daily_plan_hour = DEFAULT_DAILY_PLAN_HOUR
        regulation = str(g(CONF_POWER_REGULATION, DEFAULT_POWER_REGULATION))
        self._regulation = regulation if regulation in POWER_REGULATION_MODES else DEFAULT_POWER_REGULATION
        self._v3_power = self._regulation == POWER_REGULATION_V3
        self._proven_ceiling_on = bool(g(CONF_PROVEN_CEILING, DEFAULT_PROVEN_CEILING))

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
        # Elapsed at the last poll that was up but not hashing. A v3 service
        # stop keeps btminer (and Elapsed) running, so the run that follows a
        # v3 start is uptime minus this, not uptime.
        self._idle_uptime: float | None = None
        self._run_limit: int | None = None
        self._floor_short_runs = 0
        self._hot_short_runs = 0  # uncounted (supply-hot) short runs this episode
        self._floor_short_run_limit: int | None = None
        self._floor_learned: int | None = None
        self._floor_learned_at: float | None = None
        self._floor_learn_limit: int | None = None
        self._floor_proven_ok: int | None = None
        self._floor_proven_ok_at: float | None = None
        self._floor_exhausted = False
        # Hardware-fault handling (see FLOOR_PROVEN_TTL_S). The episode base is
        # (learned, learned_at, learn_limit) before the episode's first raise.
        self._fault_short_runs = 0
        self._fault_notified = False
        self._hw_fault = False
        self._floor_episode_base: tuple[int | None, float | None, int | None] | None = None
        self._restart_loop_notified = False
        self._floor_chip_notified = False
        self._last_floor_fire_at: float = 0.0
        self._supply_hot_at: float | None = None
        # Daily plan: hourly aggregates {"t": hour start, "n"/"p": power
        # samples/sum, "ns"/"s": supply while mining, "no"/"o": outdoor}, and
        # the plan in force (power_w, computed_at, next_at, diagnostics).
        self._daily_buckets: list[dict[str, float]] = []
        self._daily_plan: dict[str, Any] | None = None
        self._daily_applied_at: float | None = None  # computed_at of the plan acted on
        self._daily_pending = False
        # Power regulation (see V3_TUNE_GRACE_S). _limit_w is the last
        # adjust_power_limit we sent (kept in both modes; the ceiling under
        # set_miner_power). _op_target is the operating power the regulator
        # wants, _op_in_force what the miner is believed to run at (the limit
        # after any restart or adjust_power_limit), _op_established whether
        # _op_target has been delivered since the last restart.
        self._limit_w: int | None = None
        self._limit_mismatch_polls = 0
        self._reported_limit_last = 0
        self._reported_limit_polls = 0
        self._op_target: int | None = None
        self._op_in_force: int | None = None
        # Unknown history at startup: nothing is credited as held before a
        # full stable run has been watched.
        self._op_in_force_at: float = time()
        self._op_established = False
        self._tune_grace_until = 0.0
        self._untuned_since: float | None = None
        self._ceiling_hold_until = 0.0
        self._v3_retry_at = 0.0
        self._v3_transport_fails = 0
        self._v3_error: str | None = None
        self._power_fallback_notified = False
        self._regulation_min_w: int | None = None
        self._rollback_power: int | None = None  # see async_setup
        self._last_power_command: str | None = None
        # 1.14: miner error codes, external derate, and the proven ceiling.
        # _limit_commanded is the last limit WE sent (adjust_power_limit);
        # the v3 adoption rule (_note_regulation_poll) must never touch it —
        # adopting a derated report as ours would mask the derate.
        self._limit_commanded: int | None = None
        self._limit_commanded_at: float = 0.0
        self._derate_candidate_since: float | None = None
        self._derate_candidate_polls = 0
        self._derate_match_polls = 0
        self._external_derate = False
        self._error_pairs_prev: set[tuple[str, str]] = set()
        # _hold_tainted suppresses proven-hold witnessing for the run in
        # progress when fault evidence arrives; our next power command marks
        # a clean run boundary and clears it.
        self._hold_tainted = False
        self._proven_max: int | None = None
        self._proven_max_at: float | None = None
        self._fault_at: float | None = None
        self._fault_power: int | None = None
        self._fault_reason: str | None = None
        self._cap_inversion_logged = False
        # Hashrate-collapse watch after a set.miner.power step:
        # (until, pre-step TH per W, pre-step power, stepped-to power).
        self._collapse_watch: tuple[float, float, int, int] | None = None
        self._collapse_polls = 0
        self._demand_summary: str = ds.UNKNOWN
        self._pid = PID(
            kp=self._kp,
            ki=float(g(CONF_PID_KI, DEFAULT_PID_KI)),
            kd=float(g(CONF_PID_KD, DEFAULT_PID_KD)),
            ke=self._ke,
            out_min=float(self._power_min),
            out_max=float(self._power_max),
            sampling_period=0,
        )
        # Self-tuning (autotune.py). Observe mode only reads; the configured
        # gains stay the baseline and an active-mode overlay goes through
        # _apply_gains. Separate Store so its data can't break the one above.
        self._autotuner = at.Autotuner(
            at.TunerConfig(
                mode=str(g(CONF_PID_AUTOTUNE_MODE, DEFAULT_PID_AUTOTUNE_MODE)),
                kp=self._kp,
                ki=float(g(CONF_PID_KI, DEFAULT_PID_KI)),
                kd=float(g(CONF_PID_KD, DEFAULT_PID_KD)),
                supply_cap=self._supply_temp_safety_cap,
                interval_increase_s=float(self._min_adjust_interval_increase),
            )
        )
        self._autotune_store: Store = Store(hass, at.STORE_VERSION, f"{DOMAIN}.{entry.entry_id}.autotune")
        self._autotune_tick: tuple | None = None
        self._ki = self._autotuner.cfg.ki  # in force, published as pid_ki
        self._autotune_failed_logged = False
        self._pid_state.setdefault("lockout_latched", False)
        if self._pid_state.get("target") is None:
            self._pid_state["target"] = self._default_target
        self._publish_shutoff(ds.Decision(ds.NONE, "starting", (), self._shutoff))
        self._publish_freeze(None, None, None)
        self._publish_floor()
        self._publish_autotune()
        self._publish_daily()
        self._publish_regulation()
        self._publish_proven()
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
                proven_at = data.get("floor_proven_ok_at")
                self._floor_proven_ok_at = float(proven_at) if proven_at is not None else None
            base = data.get("floor_episode_base")
            if isinstance(base, (list, tuple)) and len(base) == 3:
                b_learned, b_at, b_limit = base
                self._floor_episode_base = (
                    int(b_learned) if b_learned is not None else None,
                    float(b_at) if b_at is not None else None,
                    int(b_limit) if b_limit is not None else None,
                )
        except (TypeError, ValueError):
            self._floor_learned = self._floor_learned_at = self._floor_learn_limit = None
            self._floor_proven_ok_at = self._floor_episode_base = None
        self._hw_fault = bool(data.get("floor_hw_fault", False))
        try:
            if data.get("proven_max") is not None:
                self._proven_max = int(data["proven_max"])
            if data.get("proven_max_at") is not None:
                self._proven_max_at = float(data["proven_max_at"])
            if data.get("proven_fault_at") is not None:
                self._fault_at = float(data["proven_fault_at"])
            if data.get("proven_fault_power") is not None:
                self._fault_power = int(data["proven_fault_power"])
            self._fault_reason = data.get("proven_fault_reason") or None
        except (TypeError, ValueError):
            self._proven_max = self._proven_max_at = self._fault_at = self._fault_power = None
            self._fault_reason = None
        try:
            for attr, key in (
                ("_limit_w", "power_limit_w"),
                ("_op_target", "regulated_power_w"),
                ("_regulation_min_w", "regulation_min_w"),
            ):
                value = data.get(key)
                setattr(self, attr, int(value) if value is not None and int(value) > 0 else None)
        except (TypeError, ValueError):
            self._limit_w = self._op_target = self._regulation_min_w = None
        if self._limit_w is not None and not self._v3_power:
            # adjust_power_limit-only history: the persisted limit is one we
            # sent (the telemetry-adoption rule is v3-only), so an external
            # derate can be judged against it from the first poll. Under v3
            # the persisted limit may itself be an adopted derate.
            self._limit_commanded = self._limit_w
            self._limit_commanded_at = now  # the post-write gate doubles as a boot one
        if self._v3_power:
            _LOGGER.info(
                "Power regulation: set.miner.power (limit %s as the ceiling, Power Max %dW; "
                "regulated power %s)",
                f"{self._limit_w}W" if self._limit_w is not None else "unknown", self._power_max,
                f"{self._op_target}W" if self._op_target is not None else "not yet commanded",
            )
        elif self._op_target is not None:
            # Rolled back from set_miner_power: the reported limit is the old
            # ceiling, not what the miner runs at. Believe the regulated power
            # until one adjust_power_limit puts the limit back in charge.
            self._rollback_power = self._op_target
            self._op_target = None
            _LOGGER.warning(
                "Power regulation is adjust_power_limit again (was set.miner.power at %dW under a %s "
                "limit) — the next tick sends %dW as the limit (one restart)",
                self._rollback_power, f"{self._limit_w}W" if self._limit_w is not None else "unknown",
                self._rollback_power,
            )
        if self._hw_fault:
            _LOGGER.warning(
                "Hardware fault flag still set from before restart — floor learning and floor "
                "enforcement stay off until a run hashes for %d min", FLOOR_STABLE_S // 60,
            )
        if self._floor_learned is not None:
            _LOGGER.warning(
                "Learned power floor %dW in effect (Power Min %dW) — the miner could not hold "
                "%sW before; press Reset Learned Floor to re-test lower limits",
                self._floor(), self._power_min, self._floor_learn_limit,
            )
        if self._fault_at is not None and now - self._fault_at < FLOOR_PROVEN_TTL_S:
            _LOGGER.warning(
                "Proven ceiling still armed from before restart (fault: %s) — the daily plan "
                "and ceiling raises cap at %dW",
                self._fault_reason or "unknown", self._ceiling_target(now),
            )
        self._publish_floor()
        self._restore_daily(data, now)
        self._publish_shutoff(ds.Decision(ds.NONE, "restored", (), self._shutoff))
        await self._restore_autotune()
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
        try:
            if self._autotuner.mode != "off":
                self._autotuner.close(time())
        except Exception:  # noqa: BLE001 — autotune must never block an unload
            _LOGGER.exception("Autotune close failed")
        await self._save_autotune()

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
            "floor_proven_ok_at": self._floor_proven_ok_at,
            "floor_hw_fault": self._hw_fault,
            "floor_episode_base": (
                list(self._floor_episode_base) if self._floor_episode_base is not None else None
            ),
            "daily_buckets": [dict(b) for b in self._daily_buckets],
            "daily_plan": dict(self._daily_plan) if self._daily_plan is not None else None,
            "power_limit_w": self._limit_w,
            "regulated_power_w": self._op_target,
            "regulation_min_w": self._regulation_min_w,
            "proven_max": self._proven_max,
            "proven_max_at": self._proven_max_at,
            "proven_fault_at": self._fault_at,
            "proven_fault_power": self._fault_power,
            "proven_fault_reason": self._fault_reason,
        }

    async def _save(self) -> None:
        try:
            await self._store.async_save(self._store_data())
        except Exception as err:  # never let persistence break control
            _LOGGER.error("Failed to persist controller state: %s", err)

    def _save_later(self) -> None:
        self._store.async_delay_save(self._store_data, 60)

    async def _save_autotune(self) -> None:
        try:
            await self._autotune_store.async_save(self._autotuner.to_dict())
            self._autotuner.dirty = False
        except Exception as err:  # never let persistence break control
            _LOGGER.error("Failed to persist autotune data: %s", err)

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
            self._floor_proven_ok_at = None
            self._floor_short_runs = 0
            self._hot_short_runs = 0
            self._fault_short_runs = 0
            self._floor_short_run_limit = None
            self._floor_exhausted = False
            self._restart_loop_notified = False
            self._floor_chip_notified = False
            self._fault_notified = False
            self._hw_fault = False
            self._floor_episode_base = None
            self._regulation_min_w = None
            await self._save()
        for key in (
            "floor_raised", "floor_ceiling", "restart_loop", "restart_loop_chip_cap", "hardware_fault",
            "power_collapse",
        ):
            persistent_notification.async_dismiss(self.hass, f"{DOMAIN}_{key}")
        _LOGGER.warning(
            "Learned power floor reset (was %s) — the effective minimum is Power Min %dW again",
            f"{had}W" if had is not None else "not set", self._power_min,
        )
        self._publish_floor()
        self.coordinator.async_update_listeners()

    async def async_reset_autotune(self) -> None:
        """Forget the learned models and run history and drop the gain overlay."""
        async with self._step_lock:
            had = self._autotuner.effective()
            self._autotuner.reset()
            self._apply_gains(self._autotuner.effective())
            await self._save_autotune()
        _LOGGER.warning(
            "Autotune reset — models and run history cleared; gains are the configured "
            "Kp %.4g / Ki %.4g (were %.4g / %.4g, %s)",
            self._kp, self._ki, had.kp, had.ki, had.source,
        )
        self._publish_autotune()
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
                # block _note_poll's hold witnessing on this same poll.
                self._note_error_codes(now)
                self._note_derate(is_mining, now)
                self._note_poll(is_mining, now)
                self._note_regulation_poll(is_mining, now)
            except Exception:  # bookkeeping must never block edge handling or the tick
                _LOGGER.exception("Restart bookkeeping failed")
        if self._last_is_mining is None and is_mining:
            # First poll after (re)load with the miner already hashing: seed so
            # the first tick doesn't slam the limit from Kp·error alone, and
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
                    self._soft_start = False  # the start edge re-arms it
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
                self._note_btminer_restart(now)
                if ours or (
                    self._floor_short_runs == 0
                    and self._hot_short_runs == 0
                    and self._fault_short_runs == 0
                ):
                    self._resume_hold_until = max(self._resume_hold_until, now + RESUME_BOOT_HOLD_S)
                if self._recently_sent_limit() is None:
                    # Not the restart our own limit change causes (that one
                    # keeps whatever soft start was running).
                    self._arm_soft_start("mining resumed")
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
            self._publish_proven()
            self._feed_autotune()
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
        if fresh:
            self._sample_daily(now, temp, is_mining)
        if self._daily_due(now):
            self._compute_daily_plan(now, forecast)
        summary, calling, unknown, demand_index = self._demand_snapshot(now)
        self._demand_summary = summary
        self._pid_state["demand_index"] = demand_index
        # Fed to the autotuner after this step has decided (_feed_autotune).
        self._autotune_tick = (now, temp, fresh, is_mining, len(calling))

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

        if latched or not is_mining or (state.state in (ds.STOPPED, ds.RESUMING) and not state.simulated):
            # Daily strategy: whatever stopped the miner, go back to the plan
            # (one command) once it hashes again.
            self._daily_pending = True
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
        self._supply_hot(now, temp)
        if self._v3_power:
            if await self._regulation_tick(now, caps, is_mining):
                return  # this tick's command went out (a fallback restart)
        elif self._rollback_power is not None and not caps and now >= self._resume_hold_until:
            # Back on adjust_power_limit after set.miner.power: put the limit
            # back in charge at the regulated power (one restart), or the next
            # boot tunes toward the old ceiling. A cap's clamp does it too.
            power = max(min(self._rollback_power, self._power_max), self._floor())
            _LOGGER.warning("Power regulation rollback: sending %dW as the limit", power)
            if await self._set_power_limit(power, why="rollback"):
                self._save_later()
                return
        if self._strategy == "daily":
            await self._run_daily_step(temp, caps, now)
            return
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
        if self.coordinator.data.get("miner_off") is True:
            # Stopped on purpose (v3 service stop keeps Elapsed counting):
            # not a boot, and a limit change could restart it.
            return
        believed = self._reference(limit)
        if self._v3_power:
            # Booting means the operating power is the limit, so the limit is
            # what to check. What we sent is believed; the telemetry value only
            # once it has persisted past the boot transient (the 1905 W derate
            # of 2026-10-12 lasted ~40 s).
            believed = self._limit_w
            if believed is None:
                if uptime < BOOT_TRANSIENT_S or self._reported_limit_polls < LIMIT_PERSIST_POLLS or limit <= 0:
                    return
                believed = limit
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
        Both force power_min, never the learned floor (see SUPPLY_HOT_MARGIN_F).
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
                    f" (below the learned floor {self._floor()}W)"
                    if self._floor() > self._cap_clamp(active) else "",
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
                "requested_output": None, "supply_projected": None, "predictive": None,
            }
        )

    def _seed_bumpless_transfer(self) -> int:
        """Seed the PID integral so the first tick output ≈ current miner wattage."""
        current_limit = self.coordinator.data.get("wattage_limit") or 0
        known = current_limit > 0
        sent = self._recently_sent_limit()
        if self._v3_power:
            # The telemetry limit is the ceiling, not the power: seed from the
            # power we regulate to. Unknown (first enable): the draw seeds the
            # integral only, and the first command goes out unsuppressed.
            if self._op_target is not None:
                current_limit, known = self._op_target, True
            else:
                try:
                    draw = int(float(self.coordinator.data.get("wattage") or 0))
                except (TypeError, ValueError):
                    draw = 0
                current_limit, known = (draw if draw > 0 else 0), False
        elif sent is not None:
            # The summary lags a limit change by a poll or two (and reads 0
            # mid-reboot); what we just sent is the better estimate.
            current_limit, known = sent, True
        elif self._rollback_power is not None:
            current_limit, known = self._rollback_power, True  # the limit is the old ceiling
        if not known and not current_limit:
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
        await self._command_power(new_power, floor_fire, safety=bool(caps), temp=None, caps=caps)

    def _reference(self, current_limit: int) -> int | None:
        """What we believe the miner's limit is, or None when unknown."""
        if self._last_commanded_power is not None:
            return self._last_commanded_power
        if self._v3_power:
            # The limit (sent or reported) is the ceiling, never the believed
            # operating power; unknown means the next command goes out.
            return None
        sent = self._recently_sent_limit()
        if sent is not None:
            return sent
        return int(current_limit) if current_limit and current_limit > 0 else None

    async def _set_power_limit(
        self, new_power: int, floor_fire: bool = False, op_target: int | None = None, why: str = "limit",
    ) -> bool:
        """v2 adjust_power_limit (restarts btminer). Returns True when sent.

        Under set_miner_power the limit is the ceiling (or a safety/fallback
        clamp) and ``op_target`` the operating power the regulator wants
        (default: the limit itself). Any adjust_power_limit discards the
        set.miner.power record (live 2026-10-12), so the regulation layer is
        reset: the miner runs at the limit until _op_target is re-commanded
        once tuned.
        """
        target = new_power if op_target is None else op_target
        try:
            await self.coordinator.api.set_power_limit(new_power)
            self._mark_actuation()
            now = time()
            self._sent_limit = new_power
            self._sent_limit_at = now
            self._run_limit = new_power
            self._last_commanded_power = target
            self._last_command_time = now
            self._pid_state["output"] = target
            self._limit_w = new_power
            self._limit_mismatch_polls = 0
            self._rollback_power = None
            # The last limit WE sent is the derate reference (not _limit_w,
            # which v3 telemetry adoption mutates), and a command starts a
            # clean run the proven-ceiling witness may credit again.
            self._limit_commanded = new_power
            self._limit_commanded_at = now
            self._derate_candidate_since = None
            self._derate_candidate_polls = 0
            self._derate_match_polls = 0
            self._hold_tainted = False
            self._last_power_command = f"adjust_power_limit {new_power} W ({why})"
        except Exception as err:
            _LOGGER.error("Failed to set power limit to %dW: %s", new_power, err)
            return False
        if self._v3_power:
            self._op_target = target
            self._op_in_force = new_power
            self._op_in_force_at = now
            self._op_established = target >= new_power
            self._collapse_watch = None
            self._save_later()
        try:
            if self._autotuner.mode != "off":
                self._autotuner.note_command(time(), float(target))
        except Exception:  # noqa: BLE001 — observation only
            self._autotune_failure()
        if floor_fire:
            # The raise was only scheduled for persistence (callback context);
            # make sure an HA crash right after this restart can't lose it.
            await self._save()
        return True

    # ------------------------------------------------------ power regulation

    def _draw_estimate(self) -> int | None:
        """Under set_miner_power with no commanded power yet: the measured draw.

        Only a starting point for the PID's base (legacy: None, so behaviour
        is unchanged). Never a floor-enforcement belief: a draw sits a little
        under its target and would re-fire the floor (and a re-tune) forever.
        """
        if not self._v3_power:
            return None
        try:
            draw = int(float(self.coordinator.data.get("wattage") or 0))
        except (TypeError, ValueError):
            return None
        return min(draw, self._power_max) if draw > 0 else None

    def _limit_in_force(self) -> int | None:
        """The v2 limit: what we last sent, else the telemetry value."""
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

    def _note_btminer_restart(self, now: float) -> None:
        """A start edge or Elapsed regression: the operating power is the limit until re-commanded."""
        if not self._v3_power:
            return
        self._op_established = False
        self._op_in_force = self._limit_in_force()
        self._op_in_force_at = now
        self._collapse_watch = None
        self._collapse_polls = 0

    def _note_regulation_poll(self, is_mining: bool, now: float) -> None:
        """Per fresh poll: the persistence rule for the telemetry limit, and tuning time."""
        if not self._v3_power:
            return
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

    async def _command_power(
        self, new_power: int, floor_fire: bool = False, safety: bool = False,
        temp: float | None = None, caps: frozenset[str] = frozenset(),
    ) -> None:
        """Deliver a power decision: adjust_power_limit, or set.miner.power (1.11).

        Legacy mode is exactly the old _set_power_limit. Under set_miner_power:
        - above the limit in force: raise the ceiling (power_max) when the loop
          can take it (see _ceiling_ok), else adjust_power_limit(new_power);
        - tuned: set.miner.power, falling back to adjust_power_limit when the
          miner rejects it;
        - boot window: safety clamps (caps, back-off) keep adjust_power_limit;
          anything else is held as the target and delivered once tuned.
        """
        if not self._v3_power:
            await self._set_power_limit(new_power, floor_fire)
            return
        now = time()
        ceiling = self._ceiling_target(now)
        if new_power > ceiling:
            # The proven ceiling is armed (fault evidence within 7 days):
            # don't ask the miner for more than it has proven it can hold.
            # Unarmed this is power_max, which PID output never exceeds.
            _LOGGER.info(
                "Power regulation: %dW request capped by the proven ceiling at %dW",
                new_power, ceiling,
            )
            new_power = ceiling
        limit = self._limit_in_force()
        if limit is None or new_power > limit:
            why = self._ceiling_ok(now, caps, temp)
            if why is None and (safety or floor_fire):
                # A clamp or floor enforcement above a lower clamp in force:
                # never answer it with a boot toward power_max.
                why = "a safety clamp" if safety else "floor enforcement"
            if why is None:
                _LOGGER.info(
                    "Power regulation: %dW is above the %s limit — raising the ceiling to %dW "
                    "(one restart); %dW is commanded once tuning finishes",
                    new_power, f"{limit}W" if limit is not None else "unknown", ceiling, new_power,
                )
                await self._set_power_limit(ceiling, floor_fire, op_target=new_power, why="ceiling")
                return
            _LOGGER.info(
                "Power regulation: %dW is above the %s limit and the ceiling is not raised (%s) — "
                "adjust_power_limit", new_power, f"{limit}W" if limit is not None else "unknown", why,
            )
            await self._set_power_limit(new_power, floor_fire, why="increase")
            return
        if self._v3_backed_off(now):
            await self._set_power_limit(new_power, floor_fire, why="set.miner.power backed off")
            return
        if not self._tuned(now):
            if safety:
                _LOGGER.info(
                    "Power regulation: %dW while the miner is still tuning — safety clamp via "
                    "adjust_power_limit (set.miner.power would only apply next boot)", new_power,
                )
                await self._set_power_limit(new_power, floor_fire, why="boot-window clamp")
                return
            if self._op_target != new_power:
                _LOGGER.info(
                    "Power regulation: miner still tuning (up-freq-finish %s) — %dW held until it finishes",
                    self.coordinator.data.get("up_freq_finish"), new_power,
                )
            # Only the target moves: _last_commanded_power stays what was
            # delivered, so a safety clamp to the same value still fires.
            self._op_target = new_power
            if new_power != self._op_in_force:
                self._op_established = False
            return
        await self._send_v3_power(new_power, floor_fire, now, why="regulate")

    async def _send_v3_power(self, new_power: int, floor_fire: bool, now: float, why: str) -> bool:
        """set.miner.power; on rejection fall back to adjust_power_limit. True when v3 carried it."""
        data = self.coordinator.data
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
                    "Super Password in Configure, or set Power Regulation back to adjust_power_limit.",
                )
            await self._set_power_limit(new_power, floor_fire, why="fallback")
            return False
        self._v3_transport_fails = 0
        self._v3_error = None
        if self._power_fallback_notified:
            self._power_fallback_notified = False
            persistent_notification.async_dismiss(self.hass, f"{DOMAIN}_power_fallback")
        self._tune_grace_until = now + V3_TUNE_GRACE_S
        self._op_target = new_power
        self._op_in_force = new_power
        self._op_in_force_at = now
        self._op_established = True
        self._hold_tainted = False  # a commanded power marks a clean run boundary
        self._last_commanded_power = new_power
        self._last_command_time = now
        self._pid_state["output"] = new_power
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
        try:
            if self._autotuner.mode != "off":
                self._autotuner.note_command(now, float(new_power))
        except Exception:  # noqa: BLE001 — observation only
            self._autotune_failure()
        self._save_later()
        if floor_fire:
            await self._save()
        return True

    async def _regulation_tick(self, now: float, caps: frozenset[str], is_mining: bool) -> bool:
        """Hashing, fresh tick under set_miner_power. True when it sent a restart this tick.

        In order: the hashrate-collapse watch, the tuning timeout, a ceiling
        above power_max, and re-establishing the operating power after a start
        edge, a btminer restart or any adjust_power_limit.
        """
        try:
            data = self.coordinator.data
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
                        back = max(prev, stepped)
                        _LOGGER.warning(
                            "Hashrate collapsed after set.miner.power %dW (%.1f TH at %.0fW, under %.0f%% of "
                            "the %.3f TH/kW before) — back to %dW with adjust_power_limit",
                            stepped, hashrate, draw, 100 * V3_COLLAPSE_FRAC, 1000 * before_tpw, back,
                        )
                        if stepped < prev:
                            self._regulation_min_w = max(self._regulation_min_w or 0, prev)
                            self._notify(
                                "power_collapse",
                                "Hashrate collapsed after a power reduction",
                                f"After set.miner.power lowered the miner from {prev} W to {stepped} W its "
                                f"hashrate fell to {hashrate:.1f} TH at {draw:.0f} W. The controller sent "
                                f"{back} W with adjust_power_limit (one restart) and will not regulate below "
                                f"{prev} W until Reset Learned Floor is pressed.",
                            )
                            self._publish_floor()
                            await self._save()
                        # The collapse is fault evidence at the stepped-to
                        # power; note it BEFORE the undo write so that
                        # write's clean-run boundary lets the recovery run
                        # witness a new proven hold.
                        self._note_fault(now, stepped, f"hashrate collapse at {stepped}W")
                        await self._set_power_limit(back, why="hashrate collapse")
                        return True
            target = self._op_target
            tuned = self._tuned(now)
            if (
                not caps and self._untuned_since is not None
                and now - self._untuned_since >= V3_TUNE_TIMEOUT_S
                and target is not None and self._op_in_force is not None
                and target < self._op_in_force - self._min_power_step_fine
            ):
                _LOGGER.warning(
                    "Miner still tuning %.0f min after its last start or power change, running toward "
                    "%dW instead of %dW — sending %dW as the limit; the ceiling stays down for %.0f h",
                    (now - self._untuned_since) / 60, self._op_in_force, target, target,
                    V3_CEILING_BACKOFF_S / 3600,
                )
                self._untuned_since = None
                self._ceiling_hold_until = now + V3_CEILING_BACKOFF_S
                await self._set_power_limit(target, why="tuning timeout")
                return True
            if not tuned or caps:
                return False  # caps: the strategy step clamps this tick
            limit = self._limit_in_force()
            ceiling = self._ceiling_target(now)
            if limit is not None and limit > ceiling and now >= self._resume_hold_until:
                # Power Max (or an armed proven ceiling) was lowered: the old
                # ceiling would let every boot tune above it. Once,
                # deliberately, while the miner is tuned.
                want = min(target if target is not None else ceiling, ceiling)
                if ceiling < self._power_max:
                    _LOGGER.info(
                        "Power regulation: lowering the ceiling %dW to the proven ceiling %dW",
                        limit, ceiling,
                    )
                else:
                    _LOGGER.info("Power regulation: lowering the ceiling %dW to Power Max %dW", limit, ceiling)
                await self._set_power_limit(ceiling, op_target=want, why="ceiling lowered")
                return True
            if self._op_established:
                return False
            if target is None:
                self._op_established = True  # nothing commanded yet: the PID's first command is the target
                return False
            target = max(min(target, self._power_max), self._floor())
            if target == self._op_in_force:
                self._op_established = True  # already delivered since the last restart
                return False
            if limit is not None and target >= limit:
                self._op_established = True  # it runs at the limit, which is what we want
                self._op_in_force = limit
                return False
            if self._v3_backed_off(now):
                await self._set_power_limit(target, why="re-establish, set.miner.power backed off")
                return True
            _LOGGER.info("Power regulation: tuning finished — re-commanding %dW", target)
            await self._send_v3_power(target, False, now, why="re-establish")
            return False
        except Exception:  # never let regulation bookkeeping break the tick
            _LOGGER.exception("Power regulation tick failed")
            return False

    def _publish_regulation(self) -> None:
        now = time()
        limit = self._limit_in_force() if self._v3_power else self._limit_w
        self._pid_state["power_regulation"] = {
            "mode": self._regulation,
            "path": getattr(self.coordinator.api, "power_api", None),
            "last_command": self._last_power_command,
            "up_freq_finish": self.coordinator.data.get("up_freq_finish"),
            "tuned": self._tuned(now) if self._v3_power else None,
            "limit_w": limit,
            "ceiling_in_force": bool(self._v3_power and limit is not None and limit >= self._power_max),
            "regulated_power_w": self._op_target if self._v3_power else None,
            "established": self._op_established if self._v3_power else None,
            "minimum_w": self._regulation_min_w if self._v3_power else None,
            "v3_error": self._v3_error,
            "v3_retry_at": _iso(self._v3_retry_at) if self._v3_backed_off(now) else None,
            "ceiling_held_until": _iso(self._ceiling_hold_until) if now < self._ceiling_hold_until else None,
        }

    # --------------------------------------------------------- learned floor

    def _floor(self) -> int:
        """The lowest power we will command: power_min, or the learned floor.

        Under set_miner_power also the power a hashrate collapse was undone
        to (see V3_COLLAPSE_FRAC).
        """
        floor = max(self._power_min, self._floor_learned or 0)
        if self._v3_power and self._regulation_min_w is not None:
            floor = max(floor, self._regulation_min_w)
        return max(self._power_min, min(floor, self._power_max - self._min_power_step))

    def _floor_ceiling(self) -> int:
        """The highest floor learning may reach; restarts above it are not floor evidence."""
        return min(self._power_min + FLOOR_MAX_RAISE_W, self._power_max - self._min_power_step)

    def _cap_clamp(self, caps: frozenset[str]) -> int:
        """What an engaged cap forces: power_min, never the learned floor."""
        return self._power_min

    def _supply_hot(self, now: float, temp: float | None = None) -> bool:
        """True while restarts may be thermal, not floor evidence (see SUPPLY_HOT_MARGIN_F)."""
        # The chip cap has its own handling in _consider_floor_raise.
        hot = "supply" in self._caps_active or self._freeze_hold_over_lockout or (
            temp is not None and temp >= self._supply_temp_safety_cap - SUPPLY_HOT_MARGIN_F
        )
        if hot:
            self._supply_hot_at = now
            return True
        return self._supply_hot_at is not None and now - self._supply_hot_at < SUPPLY_HOT_WINDOW_S

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
        any safety cap holds the miner at power_min, once learning is
        exhausted or a hardware fault is flagged, and when no floor has been learned (a limit merely below
        the configured power_min waits for the normal gates).
        """
        if self._floor_learned is None or self._floor_exhausted or self._hw_fault or caps:
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
                run_s=self._last_uptime - (self._idle_uptime or 0.0),
            )
            self._note_btminer_restart(now)
        self._last_uptime = uptime
        if self._idle_uptime is not None and uptime < self._idle_uptime:
            self._idle_uptime = None  # btminer restarted since
        if not is_mining:
            self._idle_uptime = uptime if uptime > 0 else None
        hashed_for = uptime - (self._idle_uptime or 0.0)
        sent = self._recently_sent_limit()
        if self._v3_power:
            # The floor learner's evidence is the operating power the miner
            # ran at (spec 4), not the limit: under set.miner.power the limit
            # is a ceiling it never reaches.
            run = self._op_in_force if self._op_in_force is not None else self._limit_in_force()
            if run:
                self._run_limit = run
            # A power change takes effect without a restart, so credit a
            # power only once it has been in force for a full stable run.
            creditable = now - self._op_in_force_at >= FLOOR_STABLE_S
        else:
            if sent is not None:
                self._run_limit = sent
            elif limit > 0:
                self._run_limit = limit
            creditable = sent is None
        if is_mining and hashed_for >= FLOOR_STABLE_S and self._run_limit:
            # This run held its limit: the episode (if any) is over.
            changed = bool(
                self._floor_short_runs or self._floor_exhausted or self._restart_loop_notified
                or self._hw_fault or self._floor_episode_base is not None
            )
            if self._hw_fault:
                _LOGGER.info(
                    "Miner held %dW for %.0f min — clearing the hardware-fault flag; floor "
                    "learning and enforcement resume", self._run_limit, hashed_for / 60,
                )
            self._floor_short_runs = 0
            self._hot_short_runs = 0
            self._fault_short_runs = 0
            self._floor_short_run_limit = None
            self._floor_exhausted = False
            self._restart_loop_notified = False
            self._floor_chip_notified = False
            self._fault_notified = False
            self._hw_fault = False
            self._floor_episode_base = None
            # A limit we sent during this run has not been held for a second
            # yet (its restart is still to come): credit only a limit in force
            # since the run began.
            if creditable:
                proven, proven_at = self._floor_proven_ok, self._floor_proven_ok_at
                if proven is None or not self._proven_recent(now) or self._run_limit < proven:
                    self._floor_proven_ok = self._run_limit
                    self._floor_proven_ok_at = now
                    changed = True
                elif (
                    self._run_limit == proven
                    and (proven_at is None or now - proven_at >= FLOOR_PROVEN_REFRESH_S)
                ):
                    self._floor_proven_ok_at = now  # still holding it: keep the proof fresh
                    changed = True
            if changed:
                self._save_later()
            self._witness_hold(now, creditable)
        self._publish_floor()

    def _proven_recent(self, now: float) -> bool:
        """True when proven_ok was demonstrated within FLOOR_PROVEN_TTL_S (legacy: stale)."""
        at = self._floor_proven_ok_at
        return self._floor_proven_ok is not None and at is not None and now - at < FLOOR_PROVEN_TTL_S

    # --------------------------------- error codes, derate, proven ceiling

    def _note_error_codes(self, now: float) -> None:
        """Firmware error codes (v2 get_error_code): notify new ones, arm the cap.

        A (code, timestamp) pair is notified once, when it first appears in
        the poll data; the firmware timestamps each occurrence, so a code
        that clears and returns notifies again. Every new pair is also fault
        evidence for the proven ceiling.
        """
        codes = self.coordinator.data.get("error_codes")
        if not isinstance(codes, dict):
            return
        pairs = {(str(c), str(t)) for c, t in codes.items()}
        new = sorted(pairs - self._error_pairs_prev)
        self._error_pairs_prev = pairs
        for code, ts in new:
            _LOGGER.warning("Miner firmware reports error %s (at %s)", code, ts)
            self._notify(
                f"miner_error_{code}",
                f"Miner reports error {code}",
                f"The miner firmware is reporting error code {code} (raised at {ts}). Look up the "
                "code in Whatsminer's API documentation or the miner web UI's error log, and see "
                "the Error Code sensor for everything currently active. The proven ceiling is "
                "armed: the daily plan and power ceiling now cap at what the miner has proven it "
                "can hold.",
            )
            limit = self._op_in_force if self._v3_power else None
            self._note_fault(now, limit or self._limit_in_force(), f"error code {code}")

    def _note_fault(self, now: float, power: int | None, reason: str) -> None:
        """Fault evidence (error code, external derate, collapse undo).

        Arms the proven ceiling for FLOOR_PROVEN_TTL_S and taints the run in
        progress: a hold that was already under way when the fault arrived
        says nothing about what the miner can still hold.
        """
        self._fault_at = now
        self._fault_power = power
        self._fault_reason = reason
        self._hold_tainted = True
        self._save_later()

    def _note_derate(self, is_mining: bool, now: float) -> None:
        """External-derate watch: reported limit well below what WE last sent.

        See DERATE_MIN_DELTA_W for the gates. The flag means "the miner runs
        under a limit nobody here commanded"; it clears after DERATE_POLLS
        polls back at the commanded value.
        """
        try:
            reported = int(self.coordinator.data.get("wattage_limit") or 0)
        except (TypeError, ValueError):
            reported = 0
        commanded = self._limit_commanded
        if (
            not is_mining or commanded is None or reported <= 0
            or now - self._limit_commanded_at < DERATE_POST_WRITE_S
            or (self._v3_power and not self._tuned(now))
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
                    "the firmware derated itself (check the Error Code sensor); treating this "
                    "as fault evidence", reported, commanded,
                )
                self._note_fault(now, commanded, f"external derate to {reported}W (commanded {commanded}W)")
                self._publish_proven()
        else:
            self._derate_candidate_since = None
            self._derate_candidate_polls = 0
            if self._external_derate:
                self._derate_match_polls += 1
                if self._derate_match_polls >= DERATE_POLLS:
                    self._external_derate = False
                    self._derate_match_polls = 0
                    _LOGGER.info("Miner is back at the commanded %dW limit — external derate cleared", commanded)
                    self._publish_proven()

    def _witness_hold(self, now: float, creditable: bool) -> None:
        """The run held FLOOR_STABLE_S: remember the highest power proven holdable.

        Skipped for the run in progress when fault evidence arrived
        (_hold_tainted), while the miner is externally derated (what it
        holds then is its choice, not our ask), and for powers a bare
        telemetry report cannot credit (``creditable``, the same rule the
        floor learner uses).
        """
        if not creditable or self._hold_tainted or self._external_derate or not self._run_limit:
            return
        limit = int(self._run_limit)
        if self._proven_max is None or limit > self._proven_max:
            self._proven_max = limit
            self._proven_max_at = now
            _LOGGER.info("Proven hold: the miner held %dW for a full stable run", limit)
            self._save_later()
        elif limit == self._proven_max and (
            self._proven_max_at is None or now - self._proven_max_at >= FLOOR_PROVEN_REFRESH_S
        ):
            self._proven_max_at = now  # still holding it: keep the proof fresh
            self._save_later()

    def _proven_cap(self, now: float) -> tuple[int, bool]:
        """(Highest power the plan/ceiling may target, armed?). See CONF_PROVEN_CEILING.

        Armed by fault evidence (error code, external derate, collapse undo)
        within FLOOR_PROVEN_TTL_S. With a witnessed hold the cap is
        proven_max + PROVEN_MARGIN_W; without one it is PROVEN_MARGIN_W
        under the power that faulted. On an inversion (that fallback sits
        under the effective floor) the floor wins, logged once.
        """
        armed = bool(
            self._proven_ceiling_on
            and self._fault_at is not None
            and now - self._fault_at < FLOOR_PROVEN_TTL_S
        )
        if not armed:
            return self._power_max, False
        if self._proven_max is not None:
            return min(self._power_max, self._proven_max + PROVEN_MARGIN_W), True
        if self._fault_power:
            wanted = self._fault_power - PROVEN_MARGIN_W
            floor = self._floor()
            if wanted < floor:
                if not self._cap_inversion_logged:
                    self._cap_inversion_logged = True
                    _LOGGER.info(
                        "Proven ceiling: the fault power %dW less the margin sits under the "
                        "effective floor %dW (fault: %s) — the floor stands",
                        self._fault_power, floor, self._fault_reason,
                    )
                return min(floor, self._power_max), True
            return min(wanted, self._power_max), True
        return self._power_max, True

    def _ceiling_target(self, now: float) -> int:
        """The highest power a daily plan or a v2 ceiling raise may target now."""
        return self._proven_cap(now)[0]

    def _publish_proven(self) -> None:
        cap, armed = self._proven_cap(time())
        self._pid_state["external_derate"] = self._external_derate
        self._pid_state["proven_ceiling"] = {
            "proven_max_w": self._proven_max,
            "proven_max_at": _iso(self._proven_max_at),
            "plan_cap": cap,
            "plan_cap_armed": armed,
        }

    def _count_restart(self, regression: _Regression, now: float) -> None:
        """A confirmed Elapsed regression: attribute it, and count it if it ended a short run."""
        if regression.ours:
            self._pending_restart_at = None
        self._last_restart_ours = regression.ours
        if regression.ours or regression.run_s >= FLOOR_STABLE_S or not regression.limit:
            return
        limit = regression.limit
        if self._supply_hot(now, self._current_temperature()):
            self._hot_short_runs += 1
            _LOGGER.info(
                "Miner restarted on its own after hashing %.0fs at %dW with the supply hot — "
                "not counted toward the learned floor", regression.run_s, limit,
            )
            return
        proven = self._floor_proven_ok if self._proven_recent(now) else None
        if self._hw_fault or (proven is not None and limit >= proven):
            # The miner recently held a limit at or below this one, so this is
            # not a power floor: a hashboard dropping out restarts btminer at
            # any limit. Never floor evidence. Above the ceiling the restart
            # loop path below still reports it.
            self._fault_short_runs += 1
            _LOGGER.warning(
                "Miner restarted on its own after hashing %.0fs at %dW, but it held %s within "
                "the last %d days — hardware fault suspected, not counted toward the learned "
                "floor (%d such restart%s)",
                regression.run_s, limit,
                f"{proven}W" if proven is not None else "a limit",
                int(FLOOR_PROVEN_TTL_S // 86400), self._fault_short_runs,
                "" if self._fault_short_runs == 1 else "s",
            )
            if limit <= self._floor_ceiling() or self._hw_fault:
                self._floor_short_runs = 0
                self._floor_short_run_limit = None
                self._notify_hardware_fault(limit, proven)
                self._publish_floor()
                return
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
            regression.run_s, limit, self._floor_short_runs,
            "" if self._floor_short_runs == 1 else "s",
        )
        self._consider_floor_raise(limit, now)

    def _notify_hardware_fault(self, limit: int, proven: int | None) -> None:
        """Once per episode, after FLOOR_SHORT_RUNS_TO_LEARN fault restarts."""
        if self._fault_notified or self._hw_fault:
            return
        if self._fault_short_runs < FLOOR_SHORT_RUNS_TO_LEARN:
            return
        self._fault_notified = True
        held = (
            f"it held {proven} W for {FLOOR_STABLE_S / 60:.0f} min or more on "
            f"{_iso(self._floor_proven_ok_at)}"
            if proven is not None else "it has held lower limits"
        )
        self._notify(
            "hardware_fault",
            "Miner hardware fault suspected",
            f"The miner restarted {self._fault_short_runs} times without a command from the "
            f"controller, most recently at {limit} W, yet {held}. Restarts at limits the miner "
            "has already held are not a power-floor problem, so the controller is not raising "
            "its floor. The integration cannot read the miner's error codes: check the miner "
            "web UI (or the dashboard's hashboard status) for errors 560-563 (slot "
            "power/hashrate imbalance). The usual fix is to reseat the hashboard adapter/ribbon "
            "and re-torque the busbar.",
        )

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
                # Restarting at every limit from the episode's start up to the
                # ceiling: that is a hardware fault, not a floor. Undo this
                # episode's raises and stop learning (and enforcing) until a run
                # holds FLOOR_STABLE_S.
                self._floor_exhausted = True
                self._hw_fault = True
                base = self._floor_episode_base
                if base is not None:
                    self._floor_learned, self._floor_learned_at, self._floor_learn_limit = base
                self._floor_episode_base = None
                reverted = self._floor()
                _LOGGER.critical(
                    "Miner cannot hold %dW and the floor may not exceed %dW (%d restarts) — "
                    "restarts at every limit: hardware fault, not a power floor. Floor reverted "
                    "to %dW; learning and enforcement suspended until a run holds %d min",
                    limit, ceiling, self._floor_short_runs, reverted, FLOOR_STABLE_S // 60,
                )
                persistent_notification.async_dismiss(self.hass, f"{DOMAIN}_floor_raised")
                self._notify(
                    "floor_ceiling",
                    "Miner restarts at every limit — hardware fault",
                    f"The miner keeps restarting at every limit up to {limit} W (the power floor "
                    f"may not exceed {ceiling} W). Restarts at every limit are not a power-floor "
                    f"problem, so the floor was reverted to {reverted} W and learning is "
                    "suspended until a run hashes for "
                    f"{FLOOR_STABLE_S / 60:.0f} min. Service the hashboards: on the M64 this "
                    "pattern goes with hashboard errors 560-563 (slot power/hashrate imbalance: "
                    "reseat the adapter/ribbon, re-torque the busbar); check the miner web UI or "
                    "the dashboard. Turn Mining Control off if you want it stopped.",
                )
                self._publish_floor()
                self._save_later()
            return
        if self._floor_episode_base is None:
            self._floor_episode_base = (
                self._floor_learned, self._floor_learned_at, self._floor_learn_limit,
            )
        self._floor_learned = new
        self._floor_learned_at = now
        self._floor_learn_limit = limit
        self._floor_exhausted = False
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
            "proven_ok_at": _iso(self._floor_proven_ok_at),
            "short_runs": self._floor_short_runs,
            "fault_short_runs": self._fault_short_runs,
            "hardware_fault_suspected": (
                self._hw_fault or self._fault_short_runs >= FLOOR_SHORT_RUNS_TO_LEARN
            ),
            "learning_suspended": self._hw_fault,
            "exhausted": self._floor_exhausted,
            "run_uptime_s": self._last_uptime,
        }

    # ---------------------------------------------------------- daily plan

    def _restore_daily(self, data: dict[str, Any], now: float) -> None:
        buckets = data.get("daily_buckets")
        if isinstance(buckets, list):
            keep: list[dict[str, float]] = []
            for b in buckets:
                try:
                    bucket = {k: float(b[k]) for k in ("t", "n", "p", "ns", "s", "no", "o")}
                except (KeyError, TypeError, ValueError):
                    continue
                if now - bucket["t"] < DAILY_WINDOW_S + 3600:
                    keep.append(bucket)
            self._daily_buckets = sorted(keep, key=lambda b: b["t"])
        plan = data.get("daily_plan")
        if isinstance(plan, dict) and plan.get("next_at") is not None:
            try:
                plan["next_at"] = float(plan["next_at"])
                plan["computed_at"] = float(plan["computed_at"])
                plan["power_w"] = int(plan["power_w"]) if plan.get("power_w") is not None else None
            except (KeyError, TypeError, ValueError):
                plan = None
            self._daily_plan = plan
        self._publish_daily()

    def _sample_daily(self, now: float, temp: float | None, is_mining: bool) -> None:
        """Fold one fresh poll into the hourly aggregates behind the daily plan."""
        hour = now - now % 3600
        if not self._daily_buckets or self._daily_buckets[-1]["t"] != hour:
            self._daily_buckets.append({"t": hour, "n": 0, "p": 0.0, "ns": 0, "s": 0.0, "no": 0, "o": 0.0})
            cutoff = now - DAILY_WINDOW_S - 3600
            self._daily_buckets = [b for b in self._daily_buckets if b["t"] > cutoff]
            self._save_later()  # once an hour, so the 60 s delayed save is never starved
        bucket = self._daily_buckets[-1]
        power = 0.0
        if is_mining:
            data = self.coordinator.data
            try:
                power = float(data.get("wattage") or data.get("wattage_limit") or 0)
            except (TypeError, ValueError):
                power = 0.0
            if temp is not None:
                bucket["ns"] += 1
                bucket["s"] += float(temp)
        bucket["n"] += 1
        bucket["p"] += power
        outdoor = self._read_outdoor_fahrenheit()
        if outdoor is not None:
            bucket["no"] += 1
            bucket["o"] += float(outdoor)

    def _next_plan_at(self, now: float) -> float:
        """The next occurrence of the plan hour, local time (DST-aware)."""
        local = dt_util.as_local(dt_util.utc_from_timestamp(now))
        nxt = local.replace(hour=self._daily_plan_hour, minute=0, second=0, microsecond=0)
        if nxt.timestamp() <= now:
            nxt = (nxt + timedelta(days=1)).replace(hour=self._daily_plan_hour)
        return nxt.timestamp()

    def _daily_due(self, now: float) -> bool:
        plan = self._daily_plan
        return (
            self._strategy != "pid"
            and (plan is None or plan.get("power_w") is None or now >= float(plan.get("next_at") or 0))
        )

    def _compute_daily_plan(self, now: float, forecast: list[tuple[float, float]]) -> None:
        window = [b for b in self._daily_buckets if now - b["t"] < DAILY_WINDOW_S]
        samples_h = sum(1 for b in window if b["n"] > 0)
        prev_plan = self._daily_plan or {}
        reference = self._reference(int(self.coordinator.data.get("wattage_limit") or 0))
        prev = prev_plan.get("power_w")
        if prev is None:
            prev = reference
        floor, top = self._floor(), self._ceiling_target(now)

        def mean(n_key: str, s_key: str, min_hours: int) -> float | None:
            hours = [b for b in window if b[n_key] > 0]
            if len(hours) < min_hours:
                return None
            return sum(b[s_key] / b[n_key] for b in hours) / len(hours)

        p_avg = mean("n", "p", DAILY_MIN_HOURS)
        t_out = mean("no", "o", DAILY_MIN_HOURS)
        ahead = [v for ts, v in forecast if now < ts <= now + DAILY_WINDOW_S]
        t_fc = sum(ahead) / len(ahead) if len(ahead) >= DAILY_MIN_HOURS else None
        supply = mean("ns", "s", 1)
        target = self._target()
        p_ff = p_fb = None
        if p_avg is None:
            power = prev
            reason = f"only {samples_h} h of samples (need {DAILY_MIN_HOURS}) — holding the current limit"
            next_at = min(self._next_plan_at(now), now + DAILY_RETRY_S)
        else:
            if t_out is not None and t_fc is not None:
                p_ff = p_avg * (DAILY_BALANCE_F - t_fc) / max(DAILY_BALANCE_F - t_out, DAILY_MIN_SPAN_F)
                reason = f"forecast {t_fc:.1f}°F vs last 24 h {t_out:.1f}°F"
            else:
                p_ff = p_avg
                reason = "no outdoor/forecast data — feedforward is the 24 h mean power"
            p_fb = DAILY_KP_W_PER_F * (target - supply) if supply is not None else 0.0
            if supply is not None:
                reason += f"; supply mean {supply:.1f}°F vs target {target:.1f}°F"
            raw = p_ff + p_fb
            power = int(round(raw / DAILY_ROUND_W) * DAILY_ROUND_W)
            if top < self._power_max and power > top:
                reason += f"; capped by proven max {top} W"
            power = max(floor, min(top, power))
            next_at = self._next_plan_at(now)
        self._daily_plan = {
            "power_w": None if power is None else int(power),
            "computed_at": now,
            "next_at": next_at,
            "p_avg24": None if p_avg is None else round(p_avg, 1),
            "t_out24": None if t_out is None else round(t_out, 2),
            "t_fc24": None if t_fc is None else round(t_fc, 2),
            "supply_mean24": None if supply is None else round(supply, 2),
            "p_ff": None if p_ff is None else round(p_ff, 1),
            "p_fb": None if p_fb is None else round(p_fb, 1),
            "samples_h": samples_h,
            "reason": reason,
        }
        _LOGGER.info(
            "Daily plan (%s): %sW — P_avg24=%s T_out24=%s T_fc24=%s supply24=%s P_ff=%s P_fb=%s "
            "(%d h of samples; %s); next plan %s",
            self._strategy, power, self._daily_plan["p_avg24"], self._daily_plan["t_out24"],
            self._daily_plan["t_fc24"], self._daily_plan["supply_mean24"], self._daily_plan["p_ff"],
            self._daily_plan["p_fb"], samples_h, reason, _iso(next_at),
        )
        self._publish_daily()
        self._save_later()

    def _trim_plan_valid(self) -> bool:
        """daily_trim: a computed plan is in force (not the <12 h hold)."""
        plan = self._daily_plan or {}
        return plan.get("power_w") is not None and plan.get("p_ff") is not None

    def _publish_daily(self) -> None:
        plan = self._daily_plan or {}
        self._pid_state["daily_plan"] = {
            "strategy": self._strategy,
            "power_w": plan.get("power_w"),
            "computed_at": _iso(plan.get("computed_at")),
            "next_at": _iso(plan.get("next_at")) if self._strategy != "pid" else None,
            "p_avg24": plan.get("p_avg24"),
            "t_out24": plan.get("t_out24"),
            "t_fc24": plan.get("t_fc24"),
            "supply_mean24": plan.get("supply_mean24"),
            "p_ff": plan.get("p_ff"),
            "p_fb": plan.get("p_fb"),
            "samples_h": plan.get("samples_h"),
            "trim_active": self._strategy == "daily_trim" and self._trim_plan_valid(),
            "reason": plan.get("reason") if self._strategy != "pid" else "strategy is pid",
        }

    # -------------------------------------------------------------- autotune

    async def _restore_autotune(self) -> None:
        tuner = self._autotuner
        try:
            note = tuner.load(await self._autotune_store.async_load())
            if note:
                _LOGGER.warning("Autotune: %s", note)
        except Exception:  # noqa: BLE001 — bad data must not stop the setup
            _LOGGER.exception("Autotune data could not be restored — starting fresh")
            tuner.reset()
        try:
            gains = tuner.effective()
            self._apply_gains(gains)
            if gains.source == "autotune":
                _LOGGER.warning(
                    "Autotune gains in effect: Kp %.4g / Ki %.4g (configured %.4g / %.4g) — "
                    "press Reset Autotune to return to the configured gains",
                    gains.kp, gains.ki, tuner.cfg.kp, tuner.cfg.ki,
                )
        except Exception:  # noqa: BLE001
            self._autotune_failure()
        self._publish_autotune()

    def _feed_autotune(self) -> None:
        """Hand this tick to the autotuner. Read-only unless active mode moves the gains."""
        tick, self._autotune_tick = self._autotune_tick, None
        tuner = self._autotuner
        try:
            if tuner.mode != "off" and tick is not None:
                now, temp, fresh, is_mining, calling = tick
                data = self.coordinator.data
                power = 0.0
                if is_mining:
                    # What the miner draws (heat in); the limit if it isn't reported.
                    draw = float(data.get("wattage") or 0)
                    power = draw if draw > 0 else float(data.get("wattage_limit") or 0)
                gains = tuner.observe(
                    at.Obs(
                        t=now, supply=temp, target=self._target(), fresh=fresh, mining=is_mining,
                        power_w=power, calling=calling, mode=str(self._pid_state.get("control_mode")),
                        soft_start=self._soft_start,
                    )
                )
                if gains is not None:
                    self._apply_gains(gains)
            if tuner.dirty:
                tuner.dirty = False
                self._autotune_store.async_delay_save(tuner.to_dict, 60)
            self._publish_autotune()
        except Exception:  # noqa: BLE001 — never let autotune touch control
            self._autotune_failure()

    def _apply_gains(self, gains: at.Gains) -> None:
        """Put kp/ki in force (overlay or configured), bumplessly.

        self._kp also sizes the bumpless seed, soft-start steps and the
        predictive pull-back, so it moves with the PID. The integral takes
        up the change in Kp·error so the next output is where this one was.
        kd is never changed.
        """
        kp, ki = float(gains.kp), float(gains.ki)
        if kp == self._kp and ki == self._ki:
            return
        old_kp = self._kp
        self._kp, self._ki = kp, ki
        self._pid.set_pid_param(kp=kp, ki=ki)
        try:
            self._pid.integral = float(self._pid.integral + (old_kp - kp) * float(self._pid.error or 0.0))
        except (TypeError, ValueError, AssertionError):
            pass
        _LOGGER.warning("PID gains now Kp %.4g / Ki %.4g (%s)", kp, ki, gains.source)
        self._publish_autotune()

    def _autotune_failure(self) -> None:
        if not self._autotune_failed_logged:
            self._autotune_failed_logged = True
            _LOGGER.exception("Autotune failed — control is unaffected; further failures logged at debug")
        else:
            _LOGGER.debug("Autotune failed again", exc_info=True)

    def _publish_autotune(self) -> None:
        self._pid_state["pid_kp"] = self._kp
        self._pid_state["pid_ki"] = self._ki
        self._pid_state["pid_kd"] = self._autotuner.cfg.kd
        try:
            snap = self._autotuner.snapshot()
            snap["last_fit_at"] = _iso(snap["last_fit_at"])
            for key in ("last_run", "last_fit", "last_reject", "last_event"):
                if snap.get(key):
                    item = dict(snap[key])
                    for ts in ("t", "start"):
                        if item.get(ts) is not None:
                            item[ts] = _iso(item[ts])
                    snap[key] = item
            self._pid_state["autotune"] = snap
        except Exception:  # noqa: BLE001
            self._autotune_failure()

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
        if self._strategy == "daily":
            return  # the plan is the setpoint; no ramp toward it
        if not self._soft_start:
            _LOGGER.info("Soft start armed (%s) — increases stepped until the supply nears target", why)
        self._soft_start = True

    def _soft_start_power(
        self, base: int, error: float, slope: float | None, projected: float, target: float,
        boot_hold: bool, now: float,
    ) -> int:
        """The limit the soft start allows this tick: hold base, or one step up."""
        # Spaced from our last limit change; after a resume the boot hold is
        # the wait. No step without a slope: we must see the supply isn't rising.
        spacing = max(PREDICT_HORIZON_S, float(self._min_adjust_interval_increase))
        if boot_hold or slope is None or projected >= target or now - self._sent_limit_at < spacing:
            return base
        span = max(0.0, self._pid.out_max - self._pid.out_min)
        step = min(SOFT_START_STEP_FRAC * span, max(self._kp * (target - projected), float(self._min_power_step)))
        return int(round(min(float(base) + step, self._pid.out_max)))

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
        # daily_trim: intersect the envelope with the plan band. The band can
        # only narrow; caps, lockouts and the floor clamp downstream exactly
        # as under pid. If the band leaves no overlap with the existing
        # bounds (a cap/envelope wholly below P_day - DAILY_TRIM_BAND), the
        # existing bounds win and the tick is what pid would do.
        if self._strategy == "daily_trim" and self._trim_plan_valid():
            planned = float(self._daily_plan["power_w"])
            lo = max(self._pid.out_min, planned - DAILY_TRIM_BAND)
            hi = min(self._pid.out_max, planned + DAILY_TRIM_BAND)
            if lo <= hi:
                self._pid.out_min, self._pid.out_max = lo, hi
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
        reference = self._reference(current_limit)
        base = reference if reference is not None else (self._draw_estimate() or floor)
        boot_hold = now < self._resume_hold_until

        # Prediction and soft start (see PREDICT_HORIZON_S, SOFT_START_STEP_FRAC).
        slope = self._supply_slope()
        rising = slope is not None and slope >= PREDICT_MIN_SLOPE_F_MIN
        projected = float(temp) + (max(slope, 0.0) * PREDICT_HORIZON_S / 60.0 if slope is not None else 0.0)
        if self._soft_start and error <= self._fine_step_band:
            self._soft_start = False
            # Bumpless hand-back: the PID resumes from the limit in force.
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
                    # limit in force; never raise.
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
                    # Settled: a fine nudge now is a restart for ±1°F of
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
        # cap's clamp (the floor can be a learned 2500 W).
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

        # Anti-windup. Under the soft start the PID tracks the limit in force
        # (its integral, frozen near the floor since the resume, would
        # otherwise ask for less than the miner already runs at), so the
        # hand-back is bumpless. Otherwise no upward integration while
        # anything holds the output below what the PID asks (cap, lockout,
        # dwell, prediction) or the boot hold keeps the limit from following
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

        # Actuation gate: magnitude, time, and the post-resume boot hold. Each
        # adjust_power_limit restarts mining. Safety caps and floor enforcement
        # (believed limit below a floor the miner cannot hold) bypass the time
        # gates and the boot hold.
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
        if predictive == "pull_back":
            # Replaces the larger cut the decrease interval would make later.
            effective_interval = min(effective_interval, self._min_adjust_interval_increase)
        interval_ok = elapsed >= effective_interval
        safety_fire = bool(caps) and step_ok
        backoff_fire = predictive == "back_off" and step_ok and new_power < reference

        if not safety_fire and not floor_fire and not backoff_fire and (not (step_ok and interval_ok) or boot_hold):
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
            "PID: temp=%.1f°F target=%.1f°F → power %dW (was %dW, err=%.2f)",
            temp, target, new_power, reference, self._pid.error,
        )
        await self._command_power(
            new_power, floor_fire, safety=safety_fire or backoff_fire, temp=temp, caps=caps,
        )

    async def _run_daily_step(self, temp: float | None, caps: frozenset[str], now: float) -> None:
        """Strategy "daily": hold the plan; only the safety layers move the limit.

        One command per plan (skipped inside DAILY_DEADBAND_W of the limit in
        force) and one to return to the plan when a cap releases. Caps clamp
        exactly as under the PID; floor enforcement bypasses the gates.
        """
        floor = self._floor()
        current_limit = self.coordinator.data.get("wattage_limit") or 0
        reference = self._reference(current_limit)
        plan = self._daily_plan or {}
        planned = plan.get("power_w")
        if planned is None:
            planned = reference if reference is not None else (self._draw_estimate() or floor)
        planned = max(floor, min(self._power_max, int(planned)))
        self._null_pid_internals()
        self._soft_start = False
        if caps:
            new_power, mode = self._cap_clamp(caps), "safety_cap"
            self._daily_pending = True  # return to the plan when the cap releases
        else:
            new_power, mode = planned, "daily"
            if plan.get("computed_at") != self._daily_applied_at:
                self._daily_pending = True
        self._pid_state.update(
            {
                "output": new_power,
                "requested_output": planned,
                "safety_engaged": bool(caps),
                "out_min_effective": floor,
                "out_max_effective": self._power_max,
                "soft_start": False,
                "control_mode": mode,
            }
        )
        floor_fire = self._floor_fire(reference, now, caps)
        if floor_fire:
            new_power = max(floor, new_power)
        if caps:
            if reference is not None and new_power == reference:
                return
        elif not floor_fire:
            if not self._daily_pending:
                return
            if reference is not None and abs(new_power - reference) <= DAILY_DEADBAND_W:
                _LOGGER.debug(
                    "Daily plan %dW is within %dW of the limit in force %dW — no command",
                    new_power, DAILY_DEADBAND_W, reference,
                )
                self._daily_pending = False
                self._daily_applied_at = plan.get("computed_at")
                return
            interval = (
                self._min_adjust_interval_increase
                if reference is None or new_power > reference
                else self._min_adjust_interval
            )
            if now < self._resume_hold_until or now - self._last_command_time < interval:
                self._pid_state["output"] = self._last_commanded_power
                return
            self._daily_pending = False
            self._daily_applied_at = plan.get("computed_at")
        _LOGGER.info(
            "Daily: %s → power %dW (was %s, plan %dW)",
            "safety cap" if caps else ("floor enforcement" if floor_fire else "plan"),
            new_power, f"{reference}W" if reference is not None else "unknown", planned,
        )
        await self._command_power(new_power, floor_fire, safety=bool(caps), temp=temp, caps=caps)


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")
