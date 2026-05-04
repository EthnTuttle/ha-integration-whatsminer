"""Support for Whatsminer switches."""
from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta
from time import time
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util.unit_conversion import TemperatureConverter

from .const import (
    CONF_CHIP_TEMP_SAFETY_CAP,
    CONF_DEFAULT_POWER_LIMIT,
    CONF_EXTERNAL_TEMP_SENSOR,
    CONF_PID_INTEGRAL_BAND,
    CONF_PID_KD,
    CONF_PID_KI,
    CONF_PID_KP,
    CONF_PID_COARSE_STEP_BAND,
    CONF_PID_DEMAND_ENTITIES,
    CONF_PID_DEMAND_MODE,
    CONF_PID_DEMAND_FLOOR_FRAC,
    CONF_PID_DEMAND_CEILING_FRAC,
    CONF_PID_DEMAND_WEIGHT_BY_ERROR,
    CONF_PID_FINE_STEP_BAND,
    CONF_PID_MIN_ADJUST_INTERVAL,
    CONF_PID_MIN_ADJUST_INTERVAL_INCREASE,
    CONF_PID_MIN_POWER_STEP,
    CONF_PID_MIN_POWER_STEP_FINE,
    CONF_PID_MIN_POWER_STEP_MEDIUM,
    CONF_PID_PRICE_HIGH,
    CONF_PID_PRICE_LOW,
    CONF_PID_PRICE_SENSOR,
    CONF_PID_SETPOINT_RAMP_RATE,
    CONF_PID_SUPPLY_TEMP_LOCKOUT,
    CONF_PID_SUPPLY_TEMP_SAFETY_CAP,
    CONF_PID_SURPLUS_DEFICIT,
    CONF_PID_SURPLUS_FULL,
    CONF_PID_SURPLUS_SENSOR,
    CONF_PID_TARGET_TEMP,
    CONF_PID_WEATHER_ENTITY,
    CONF_PID_FORECAST_LOOKAHEAD_MIN,
    CONF_PID_FORECAST_BLEND,
    CONF_POWER_MAX,
    CONF_POWER_MIN,
    DEFAULT_CHIP_TEMP_SAFETY_CAP,
    DEFAULT_DEFAULT_POWER_LIMIT,
    DEFAULT_PID_INTEGRAL_BAND,
    DEFAULT_PID_KD,
    DEFAULT_PID_KI,
    DEFAULT_PID_KP,
    DEFAULT_PID_COARSE_STEP_BAND,
    DEFAULT_PID_DEMAND_ENTITIES,
    DEFAULT_PID_DEMAND_MODE,
    DEFAULT_PID_DEMAND_FLOOR_FRAC,
    DEFAULT_PID_DEMAND_CEILING_FRAC,
    DEFAULT_PID_DEMAND_WEIGHT_BY_ERROR,
    DEFAULT_PID_FINE_STEP_BAND,
    DEFAULT_PID_MIN_ADJUST_INTERVAL,
    DEFAULT_PID_MIN_ADJUST_INTERVAL_INCREASE,
    DEFAULT_PID_MIN_POWER_STEP,
    DEFAULT_PID_MIN_POWER_STEP_FINE,
    DEFAULT_PID_MIN_POWER_STEP_MEDIUM,
    DEFAULT_PID_SETPOINT_RAMP_RATE,
    DEFAULT_PID_SUPPLY_TEMP_LOCKOUT,
    DEFAULT_PID_SUPPLY_TEMP_SAFETY_CAP,
    DEFAULT_PID_TARGET_TEMP,
    DEFAULT_PID_WEATHER_ENTITY,
    DEFAULT_PID_FORECAST_LOOKAHEAD_MIN,
    DEFAULT_PID_FORECAST_BLEND,
    DEFAULT_POWER_MAX,
    DEFAULT_POWER_MIN,
    DOMAIN,
)
from .coordinator import WhatsminerCoordinator
from .pid_controller import PID

_LOGGER = logging.getLogger(__name__)

# Grace period to wait for miner to change state before trusting reported state
OPTIMISTIC_STATE_TIMEOUT = timedelta(minutes=3)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Whatsminer switches from a config entry."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: WhatsminerCoordinator = data["coordinator"]
    config = data["config"]
    pid_state: dict = data["pid_state"]

    entities = [
        WhatsminerMiningSwitch(coordinator),
        WhatsminerPIDSwitch(
            coordinator=coordinator,
            pid_state=pid_state,
            power_min=config.get(CONF_POWER_MIN, DEFAULT_POWER_MIN),
            power_max=config.get(CONF_POWER_MAX, DEFAULT_POWER_MAX),
            kp=config.get(CONF_PID_KP, DEFAULT_PID_KP),
            ki=config.get(CONF_PID_KI, DEFAULT_PID_KI),
            kd=config.get(CONF_PID_KD, DEFAULT_PID_KD),
            ke=config.get(CONF_PID_KE, DEFAULT_PID_KE),
            default_target=config.get(CONF_PID_TARGET_TEMP, DEFAULT_PID_TARGET_TEMP),
            external_sensor_id=config.get(CONF_EXTERNAL_TEMP_SENSOR) or None,
            outdoor_temp_sensor_id=config.get(CONF_PID_OUTDOOR_TEMP_SENSOR) or None,
            default_power_limit=config.get(
                CONF_DEFAULT_POWER_LIMIT, DEFAULT_DEFAULT_POWER_LIMIT
            ),
            min_power_step=config.get(
                CONF_PID_MIN_POWER_STEP, DEFAULT_PID_MIN_POWER_STEP
            ),
            min_power_step_medium=config.get(
                CONF_PID_MIN_POWER_STEP_MEDIUM, DEFAULT_PID_MIN_POWER_STEP_MEDIUM
            ),
            min_power_step_fine=config.get(
                CONF_PID_MIN_POWER_STEP_FINE, DEFAULT_PID_MIN_POWER_STEP_FINE
            ),
            coarse_step_band=config.get(
                CONF_PID_COARSE_STEP_BAND, DEFAULT_PID_COARSE_STEP_BAND
            ),
            fine_step_band=config.get(
                CONF_PID_FINE_STEP_BAND, DEFAULT_PID_FINE_STEP_BAND
            ),
            min_adjust_interval=config.get(
                CONF_PID_MIN_ADJUST_INTERVAL, DEFAULT_PID_MIN_ADJUST_INTERVAL
            ),
            min_adjust_interval_increase=config.get(
                CONF_PID_MIN_ADJUST_INTERVAL_INCREASE,
                DEFAULT_PID_MIN_ADJUST_INTERVAL_INCREASE,
            ),
            chip_temp_safety_cap=config.get(
                CONF_CHIP_TEMP_SAFETY_CAP, DEFAULT_CHIP_TEMP_SAFETY_CAP
            ),
            supply_temp_safety_cap=config.get(
                CONF_PID_SUPPLY_TEMP_SAFETY_CAP, DEFAULT_PID_SUPPLY_TEMP_SAFETY_CAP
            ),
            supply_temp_lockout=config.get(
                CONF_PID_SUPPLY_TEMP_LOCKOUT, DEFAULT_PID_SUPPLY_TEMP_LOCKOUT
            ),
            demand_entities=config.get(
                CONF_PID_DEMAND_ENTITIES, DEFAULT_PID_DEMAND_ENTITIES
            ),
            demand_mode=config.get(
                CONF_PID_DEMAND_MODE, DEFAULT_PID_DEMAND_MODE
            ),
            demand_floor_frac=config.get(
                CONF_PID_DEMAND_FLOOR_FRAC, DEFAULT_PID_DEMAND_FLOOR_FRAC
            ),
            demand_ceiling_frac=config.get(
                CONF_PID_DEMAND_CEILING_FRAC, DEFAULT_PID_DEMAND_CEILING_FRAC
            ),
            demand_weight_by_error=config.get(
                CONF_PID_DEMAND_WEIGHT_BY_ERROR, DEFAULT_PID_DEMAND_WEIGHT_BY_ERROR
            ),
            integral_band=config.get(
                CONF_PID_INTEGRAL_BAND, DEFAULT_PID_INTEGRAL_BAND
            ),
            setpoint_ramp_rate=config.get(
                CONF_PID_SETPOINT_RAMP_RATE, DEFAULT_PID_SETPOINT_RAMP_RATE
            ),
            slope_ewma_tau_s=config.get(
                CONF_PID_SLOPE_EWMA_TAU_S, DEFAULT_PID_SLOPE_EWMA_TAU_S
            ),
            price_sensor_id=config.get(CONF_PID_PRICE_SENSOR) or None,
            price_high=config.get(CONF_PID_PRICE_HIGH, 0.0),
            price_low=config.get(CONF_PID_PRICE_LOW, 0.0),
            surplus_sensor_id=config.get(CONF_PID_SURPLUS_SENSOR) or None,
            surplus_deficit=config.get(CONF_PID_SURPLUS_DEFICIT, 0.0),
            surplus_full=config.get(CONF_PID_SURPLUS_FULL, 0.0),
            weather_entity_id=config.get(CONF_PID_WEATHER_ENTITY) or None,
            forecast_lookahead_min=config.get(
                CONF_PID_FORECAST_LOOKAHEAD_MIN, DEFAULT_PID_FORECAST_LOOKAHEAD_MIN
            ),
            forecast_blend=config.get(
                CONF_PID_FORECAST_BLEND, DEFAULT_PID_FORECAST_BLEND
            ),
        ),
    ]

    async_add_entities(entities)


class WhatsminerMiningSwitch(CoordinatorEntity, SwitchEntity):
    """Representation of Whatsminer mining control switch.

    Uses optimistic updates to handle the miner's slow state transitions.
    When a command is sent, the switch immediately reflects the target state
    and ignores the reported state for a grace period while the miner transitions.
    """

    _attr_icon = "mdi:power"
    _attr_has_entity_name = True

    def __init__(self, coordinator: WhatsminerCoordinator) -> None:
        """Initialize the switch."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.data['mac']}_mining_control"
        self._attr_name = "Mining Control"
        # Optimistic state tracking
        self._assumed_state: bool | None = None
        self._assumed_state_time: datetime | None = None

    @property
    def device_info(self) -> entity.DeviceInfo:
        """Return device info."""
        return entity.DeviceInfo(
            identifiers={(DOMAIN, self.coordinator.data["mac"])},
            name=self.coordinator.name,
            manufacturer=self.coordinator.data.get("make", "Whatsminer"),
            model=self.coordinator.data.get("model", "Unknown"),
            sw_version=self.coordinator.data.get("fw_ver"),
            configuration_url=f"http://{self.coordinator.data['ip']}",
        )

    @property
    def is_on(self) -> bool:
        """Return true if the miner is mining.

        Uses optimistic state if within the grace period after a command,
        otherwise falls back to the actual reported state from the miner.
        If the actual state matches the assumed state, clears the assumed state
        early since the transition is complete.
        """
        actual_state = self.coordinator.data.get("is_mining", False)

        # Check if we have an assumed state
        if self._assumed_state is not None and self._assumed_state_time is not None:
            # If actual state now matches assumed state, transition is complete
            if actual_state == self._assumed_state:
                self._assumed_state = None
                self._assumed_state_time = None
                return actual_state

            # Check if still within grace period
            time_since_command = datetime.now() - self._assumed_state_time
            if time_since_command < OPTIMISTIC_STATE_TIMEOUT:
                # Still within grace period - use assumed state
                return self._assumed_state

            # Grace period expired - clear assumed state and use actual
            self._assumed_state = None
            self._assumed_state_time = None

        # Use actual reported state from coordinator
        return actual_state

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return self.coordinator.available and self.coordinator.last_update_success

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on mining (power on hashboards)."""
        try:
            _LOGGER.info(f"Powering on hashboards on {self.coordinator.miner_ip}")
            result = await self.coordinator.api.power_on()
            _LOGGER.info(f"Power on command sent to {self.coordinator.miner_ip}: {result}")

            # Set optimistic state immediately
            self._assumed_state = True
            self._assumed_state_time = datetime.now()
            self.async_write_ha_state()

        except Exception as err:
            _LOGGER.error(f"Failed to power on {self.coordinator.miner_ip}: {err}")
            # Clear assumed state on error
            self._assumed_state = None
            self._assumed_state_time = None
            raise

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off mining (power off hashboards)."""
        try:
            _LOGGER.info(f"Powering off hashboards on {self.coordinator.miner_ip}")
            result = await self.coordinator.api.power_off()
            _LOGGER.info(f"Power off command sent to {self.coordinator.miner_ip}: {result}")

            # Set optimistic state immediately
            self._assumed_state = False
            self._assumed_state_time = datetime.now()
            self.async_write_ha_state()

        except Exception as err:
            _LOGGER.error(f"Failed to power off {self.coordinator.miner_ip}: {err}")
            # Clear assumed state on error
            self._assumed_state = None
            self._assumed_state_time = None
            raise


class WhatsminerPIDSwitch(CoordinatorEntity, SwitchEntity, RestoreEntity):
    """PID Mode on/off. When on, modulates miner power to hold a target temp."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:thermostat-auto"

    def __init__(
        self,
        coordinator: WhatsminerCoordinator,
        pid_state: dict,
        power_min: int,
        power_max: int,
        kp: float,
        ki: float,
        kd: float,
        ke: float,
        default_target: float,
        external_sensor_id: str | None,
        outdoor_temp_sensor_id: str | None,
        default_power_limit: int,
        min_power_step: int,
        min_adjust_interval: int,
        chip_temp_safety_cap: float,
        integral_band: float = DEFAULT_PID_INTEGRAL_BAND,
        setpoint_ramp_rate: float = DEFAULT_PID_SETPOINT_RAMP_RATE,
        min_adjust_interval_increase: int = DEFAULT_PID_MIN_ADJUST_INTERVAL_INCREASE,
        min_power_step_medium: int = DEFAULT_PID_MIN_POWER_STEP_MEDIUM,
        min_power_step_fine: int = DEFAULT_PID_MIN_POWER_STEP_FINE,
        coarse_step_band: float = DEFAULT_PID_COARSE_STEP_BAND,
        fine_step_band: float = DEFAULT_PID_FINE_STEP_BAND,
        supply_temp_safety_cap: float = DEFAULT_PID_SUPPLY_TEMP_SAFETY_CAP,
        supply_temp_lockout: float = DEFAULT_PID_SUPPLY_TEMP_LOCKOUT,
        demand_entities: list[str] | None = None,
        demand_mode: str = DEFAULT_PID_DEMAND_MODE,
        demand_floor_frac: float = DEFAULT_PID_DEMAND_FLOOR_FRAC,
        demand_ceiling_frac: float = DEFAULT_PID_DEMAND_CEILING_FRAC,
        demand_weight_by_error: bool = DEFAULT_PID_DEMAND_WEIGHT_BY_ERROR,
        slope_ewma_tau_s: float = 0.0,
        price_sensor_id: str | None = None,
        price_high: float = 0.0,
        price_low: float = 0.0,
        surplus_sensor_id: str | None = None,
        surplus_deficit: float = 0.0,
        surplus_full: float = 0.0,
        weather_entity_id: str | None = None,
        forecast_lookahead_min: int = DEFAULT_PID_FORECAST_LOOKAHEAD_MIN,
        forecast_blend: float = DEFAULT_PID_FORECAST_BLEND,
    ) -> None:
        """Initialize the PID switch."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.data['mac']}_pid_mode"
        self._attr_name = "PID Mode"
        self._pid_state = pid_state
        self._power_min = power_min
        self._power_max = power_max
        self._kp = kp  # kept for bumpless-transfer seed in async_turn_on
        self._ke = ke  # outdoor temp compensation coefficient (W/°F offset)
        self._default_target = default_target
        self._external_sensor_id = external_sensor_id
        self._outdoor_temp_sensor_id = outdoor_temp_sensor_id
        self._default_power_limit = default_power_limit
        self._min_power_step = min_power_step
        self._min_power_step_medium = min_power_step_medium
        self._min_power_step_fine = min_power_step_fine
        self._coarse_step_band = float(coarse_step_band)
        self._fine_step_band = float(fine_step_band)
        self._min_adjust_interval = min_adjust_interval
        self._min_adjust_interval_increase = min_adjust_interval_increase
        self._chip_temp_safety_cap = chip_temp_safety_cap
        self._supply_temp_safety_cap = float(supply_temp_safety_cap)
        self._supply_temp_lockout = float(supply_temp_lockout)
        self._demand_entities: list[str] = list(demand_entities or [])
        self._demand_mode = demand_mode
        self._demand_floor_frac = float(demand_floor_frac)
        self._demand_ceiling_frac = float(demand_ceiling_frac)
        self._demand_weight_by_error = bool(demand_weight_by_error)
        self._no_demand_logged = False
        self._integral_band = float(integral_band)
        self._setpoint_ramp_rate = float(setpoint_ramp_rate)
        self._slope_ewma_tau_s = float(slope_ewma_tau_s)
        self._slope_ewma: float | None = None
        self._slope_last_pv: tuple[float, float] | None = None
        # Price/surplus envelope parameters
        self._price_sensor_id = price_sensor_id
        self._price_high = float(price_high)
        self._price_low = float(price_low)
        self._surplus_sensor_id = surplus_sensor_id
        self._surplus_deficit = float(surplus_deficit)
        self._surplus_full = float(surplus_full)
        self._price_unavail_logged = False
        self._surplus_unavail_logged = False
        # Weather/forecast parameters
        self._weather_entity_id = weather_entity_id
        self._forecast_lookahead_min = forecast_lookahead_min
        self._forecast_blend = float(forecast_blend)
        self._forecast_cache: float | None = None
        self._forecast_cache_time: float | None = None
        # Effective (possibly ramped) setpoint the PID actually sees. None until
        # the first tick seeds it from the current PV.
        self._ramped_target: float | None = None
        self._external_unavail_logged = False
        self._outdoor_unavail_logged = False
        self._last_input_time: float | None = None
        self._last_commanded_power: int | None = None
        # Tracks mining on↔off transitions detected in _handle_coordinator_update
        # so we can reset controller state when the miner auto-stops.
        self._last_is_mining: bool | None = None
        # Monotonic timestamp of the last adjust_power_limit call. 0 means
        # "never commanded in this process" — first PID tick may fire immediately.
        self._last_command_time: float = 0.0
        self._pid = PID(
            kp=kp,
            ki=ki,
            kd=kd,
            ke=ke,
            out_min=float(power_min),
            out_max=float(power_max),
            sampling_period=0,
        )
        # Seed target so the pid_target_temp sensor and number entity have a
        # starting value before the user opens the UI.
        if self._pid_state.get("target") is None:
            self._pid_state["target"] = default_target

    async def async_added_to_hass(self) -> None:
        """Restore previous on/off state across HA restarts."""
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is None:
            return
        if last_state.state == STATE_ON:
            if self._external_sensor_id is None:
                _LOGGER.warning(
                    "PID Mode was ON before restart but no external temperature "
                    "sensor is configured — leaving PID off. Open the integration's "
                    "Configure dialog to pick a sensor, then re-enable PID Mode."
                )
                return
            # Clear PID samples so we don't carry stale derivative across a
            # restart (could be hours later).
            self._pid.clear_samples()
            self._pid.integral = 0.0
            self._pid_state["enabled"] = True

    @property
    def device_info(self) -> entity.DeviceInfo:
        """Return device info — attach to the same device as other entities."""
        return entity.DeviceInfo(
            identifiers={(DOMAIN, self.coordinator.data["mac"])},
            name=self.coordinator.name,
            manufacturer=self.coordinator.data.get("make", "Whatsminer"),
            model=self.coordinator.data.get("model", "Unknown"),
            sw_version=self.coordinator.data.get("fw_ver"),
            configuration_url=f"http://{self.coordinator.data['ip']}",
        )

    @property
    def is_on(self) -> bool:
        """Return True if PID Mode is active."""
        return bool(self._pid_state.get("enabled"))

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return self.coordinator.available and self.coordinator.last_update_success

    def _seed_bumpless_transfer(self) -> int:
        """Seed the PID integral so the first tick output ≈ current miner wattage.

        Without this, output = Kp*error on tick 1, which can slam the miner down
        to power_min when the system is already happily mining at a useful
        wattage. Used both when PID Mode is toggled on and when the miner comes
        back from an auto-shutoff while PID was already enabled.

        Returns the current_limit value used for seeding (for logging).
        """
        current_limit = self.coordinator.data.get("wattage_limit") or 0
        if current_limit <= 0:
            current_limit = self._default_power_limit
        current_temp = self._current_temperature()
        target = self._pid_state.get("target") or self._default_target
        if current_temp is not None:
            first_tick_p = self._kp * (float(target) - float(current_temp))
        else:
            first_tick_p = 0.0
        self._pid.integral = float(current_limit) - first_tick_p
        self._last_commanded_power = int(current_limit)
        return int(current_limit)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable PID Mode with bumpless transfer from the current power limit."""
        if self._pid_state.get("enabled"):
            return
        if self._external_sensor_id is None:
            raise HomeAssistantError(
                "PID Mode requires an external temperature sensor. "
                "Open the integration's Configure dialog and set External "
                "Temperature Sensor, then retry."
            )
        self._pid.clear_samples()
        self._last_input_time = None
        # Clear so the first PID tick after enable isn't blocked by a throttle
        # timer carried over from a prior PID-on session.
        self._last_command_time = 0.0
        # Let the ramp re-seed from current PV on the first tick.
        self._ramped_target = None

        seeded_limit = self._seed_bumpless_transfer()

        self._pid_state["enabled"] = True
        self.async_write_ha_state()
        _LOGGER.info(
            "PID Mode enabled on %s — seeded integral so first output ≈ %dW",
            self.coordinator.miner_ip,
            seeded_limit,
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable PID Mode and revert the miner to the default power limit."""
        if not self._pid_state.get("enabled"):
            return
        # Null PID internals so diagnostic sensors gap while paused; keep
        # "target" populated so the setpoint sensor keeps rendering.
        self._pid_state.update(
            {
                "error": None,
                "proportional": None,
                "integral": None,
                "derivative": None,
                "external": None,
                "output": None,
                "requested_output": None,
                "enabled": False,
            }
        )
        self._last_input_time = None
        self.async_write_ha_state()

        # Revert power to the configured default so the miner doesn't sit at
        # whatever wattage the PID last commanded. Best-effort — failing to
        # revert shouldn't undo the switch toggle.
        try:
            await self.coordinator.api.set_power_limit(self._default_power_limit)
            self._last_commanded_power = self._default_power_limit
            _LOGGER.info(
                "PID Mode disabled on %s — reverted power limit to %dW",
                self.coordinator.miner_ip,
                self._default_power_limit,
            )
        except Exception as err:
            _LOGGER.error(
                "Failed to revert power limit to %dW on PID off: %s",
                self._default_power_limit,
                err,
            )

    @callback
    def _handle_coordinator_update(self) -> None:
        """Detect mining transitions then run a PID step when enabled."""
        is_mining = bool(self.coordinator.data.get("is_mining"))
        if self._last_is_mining is not None and is_mining != self._last_is_mining:
            if not is_mining:
                # Miner just stopped (auto-shutoff, firmware thermal cutback,
                # manual, network drop, etc.). Clear controller state so we
                # don't resume later with hours of stale integral and a dead
                # throttle clock — that produced a 30°C probe overshoot in
                # capture data.
                self._pid.clear_samples()
                self._pid.integral = 0.0
                self._last_input_time = None
                self._last_commanded_power = None
                self._last_command_time = 0.0
                self._ramped_target = None
                _LOGGER.info("Mining stopped — PID controller state reset")
            elif self._pid_state.get("enabled"):
                # Miner came back while PID was on — re-seed bumpless so the
                # first tick matches the miner's current wattage.
                seeded = self._seed_bumpless_transfer()
                _LOGGER.info(
                    "Mining resumed — re-seeded PID for bumpless transfer (≈%dW)",
                    seeded,
                )
        self._last_is_mining = is_mining

        if self._pid_state.get("enabled"):
            self.hass.async_create_task(self._run_pid_step())
        super()._handle_coordinator_update()

    def _chip_temp(self) -> float | None:
        """Return the miner's chip-temp average, or None when not reported.

        Used only for the safety cap veto — the PID loop does NOT feed on chip
        temp. Firmware already manages thermals; this is an HA-visible
        belt-and-suspenders so a stuck/drifting external sensor can't cook the
        miner silently.
        """
        temp = self.coordinator.data.get("temperature_avg")
        try:
            value = float(temp)
        except (TypeError, ValueError):
            return None
        if value <= 0:
            return None
        return value

    def _read_external_sensor_fahrenheit(self) -> float | None:
        """Read the user-selected temperature sensor, converting to °F if needed."""
        state = self.hass.states.get(self._external_sensor_id)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN, None):
            if not self._external_unavail_logged:
                _LOGGER.warning(
                    "External temp sensor %s unavailable — PID will pause until it returns",
                    self._external_sensor_id,
                )
                self._external_unavail_logged = True
            return None
        try:
            value = float(state.state)
        except (TypeError, ValueError):
            _LOGGER.warning(
                "External temp sensor %s returned non-numeric state %r",
                self._external_sensor_id,
                state.state,
            )
            return None
        self._external_unavail_logged = False
        unit = state.attributes.get("unit_of_measurement")
        if unit and unit != UnitOfTemperature.FAHRENHEIT:
            try:
                value = TemperatureConverter.convert(
                    value, unit, UnitOfTemperature.FAHRENHEIT
                )
            except Exception as err:
                _LOGGER.warning(
                    "Could not convert %s from %s to °F: %s",
                    self._external_sensor_id,
                    unit,
                    err,
                )
                return None
        return value

    def _read_outdoor_sensor_fahrenheit(self) -> float | None:
        """Read the user-selected outdoor temperature sensor, converting to °F if needed."""
        state = self.hass.states.get(self._outdoor_temp_sensor_id)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN, None):
            if not self._outdoor_unavail_logged:
                _LOGGER.warning(
                    "Outdoor temp sensor %s unavailable — PID will not use feedforward",
                    self._outdoor_temp_sensor_id,
                )
                self._outdoor_unavail_logged = True
            return None
        try:
            value = float(state.state)
        except (TypeError, ValueError):
            _LOGGER.warning(
                "Outdoor temp sensor %s returned non-numeric state %r",
                self._outdoor_temp_sensor_id,
                state.state,
            )
            return None
        self._outdoor_unavail_logged = False
        unit = state.attributes.get("unit_of_measurement")
        if unit and unit != UnitOfTemperature.FAHRENHEIT:
            try:
                value = TemperatureConverter.convert(
                    value, unit, UnitOfTemperature.FAHRENHEIT
                )
            except Exception as err:
                _LOGGER.warning(
                    "Could not convert %s from %s to °F: %s",
                    self._outdoor_temp_sensor_id,
                    unit,
                    err,
                )
                return None
        return value

    def _read_price_sensor(self) -> float | None:
        """Read the price sensor (e.g., energy price in $/kWh)."""
        if self._price_sensor_id is None:
            return None
        state = self.hass.states.get(self._price_sensor_id)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN, None):
            if not self._price_unavail_logged:
                _LOGGER.warning(
                    "Price sensor %s unavailable — price envelope disabled",
                    self._price_sensor_id,
                )
                self._price_unavail_logged = True
            return None
        try:
            value = float(state.state)
        except (TypeError, ValueError):
            _LOGGER.warning(
                "Price sensor %s returned non-numeric state %r",
                self._price_sensor_id,
                state.state,
            )
            return None
        self._price_unavail_logged = False
        return value

    def _read_surplus_sensor(self) -> float | None:
        """Read the surplus sensor (e.g., solar production in W, positive = excess)."""
        if self._surplus_sensor_id is None:
            return None
        state = self.hass.states.get(self._surplus_sensor_id)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN, None):
            if not self._surplus_unavail_logged:
                _LOGGER.warning(
                    "Surplus sensor %s unavailable — surplus envelope disabled",
                    self._surplus_sensor_id,
                )
                self._surplus_unavail_logged = True
            return None
        try:
            value = float(state.state)
        except (TypeError, ValueError):
            _LOGGER.warning(
                "Surplus sensor %s returned non-numeric state %r",
                self._surplus_sensor_id,
                state.state,
            )
            return None
        self._surplus_unavail_logged = False
        return value

    def _blended_outdoor_temp(self, now: float) -> float | None:
        """Return outdoor temp with optional forecast blend.

        If weather entity is configured, blends current outdoor temp with forecast
        temperature at lookahead_min. Otherwise returns current outdoor temp.

        Cache duration: 120 seconds (fixed).
        """
        outdoor = self._read_outdoor_sensor_fahrenheit()
        if outdoor is None:
            return None
        if self._weather_entity_id is None:
            return outdoor
        if self._forecast_lookahead_min <= 0:
            return outdoor
        if self._forecast_blend <= 0:
            return outdoor
        if self._forecast_blend >= 1:
            forecast_only = True
            blend = 1.0
        else:
            forecast_only = False
            blend = self._forecast_blend
        cache_duration = 120.0
        if (
            self._forecast_cache is not None
            and self._forecast_cache_time is not None
            and now - self._forecast_cache_time < cache_duration
        ):
            cached_forecast = self._forecast_cache
        else:
            cached_forecast = None
            try:
                forecasts = self.hass.services.async_call(
                    "weather",
                    "get_forecasts",
                    {"entity_id": self._weather_entity_id},
                    blocking=True,
                    return_response=True,
                )
            except Exception as err:
                _LOGGER.debug(
                    "Could not get forecast from %s: %s",
                    self._weather_entity_id,
                    err,
                )
                forecasts = None
            if forecasts:
                entity_forecasts = forecasts.get(self._weather_entity_id, {})
                forecast_list = entity_forecasts.get("forecast", [])
                if forecast_list:
                    target_time = now + self._forecast_lookahead_min * 60
                    best_forecast = None
                    for entry in forecast_list:
                        forecast_time = entry.get("datetime")
                        if forecast_time:
                            if isinstance(forecast_time, str):
                                from datetime import datetime
                                try:
                                    forecast_time = datetime.fromisoformat(
                                        forecast_time.replace("Z", "+00:00")
                                    ).timestamp()
                                except Exception:
                                    continue
                            if forecast_time >= target_time:
                                best_forecast = entry
                                break
                    if best_forecast:
                        temp = best_forecast.get("temperature")
                        if temp is not None:
                            unit = best_forecast.get("temperature_unit")
                            if unit and unit != UnitOfTemperature.FAHRENHEIT:
                                try:
                                    temp = TemperatureConverter.convert(
                                        temp, unit, UnitOfTemperature.FAHRENHEIT
                                    )
                                except Exception:
                                    pass
                            cached_forecast = float(temp)
            if cached_forecast is not None:
                self._forecast_cache = cached_forecast
                self._forecast_cache_time = now
        if cached_forecast is None:
            return outdoor
        if forecast_only:
            return cached_forecast
        blended = (1.0 - blend) * outdoor + blend * cached_forecast
        return blended

    def _current_temperature(self) -> float | None:
        """Return the regulated variable read from the external sensor.

        The miner's own chip temperature is deliberately not used — it's noisy
        and the miner firmware already manages its own thermal envelope. Returns
        None if no external sensor is configured or the sensor is unavailable.
        """
        if self._external_sensor_id is None:
            return None
        return self._read_external_sensor_fahrenheit()

    def _demand_index(self) -> float | None:
        """Return demand index (0.0-1.0) based on configured climate entities.

        Used to scale PID output bounds. Returns:
          None  — feature disabled (no demand entities configured).
          0.0   — no heating demand (all idle/off/unavailable).
          1.0   — full heating demand.
          float — weighted average across known entities.
        """
        if not self._demand_entities:
            return None
        weights: list[float] = []
        for eid in self._demand_entities:
            state = self.hass.states.get(eid)
            if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
                continue
            action = state.attributes.get("hvac_action")
            if action in (None, "unavailable"):
                continue
            if action == "heating":
                if self._demand_weight_by_error:
                    cur = state.attributes.get("current_temperature")
                    sp = state.attributes.get("temperature")
                    if cur is not None and sp is not None:
                        try:
                            w = max(0.0, min((float(sp) - float(cur)) / 2.0, 1.0))
                        except (TypeError, ValueError):
                            w = 1.0
                    else:
                        w = 1.0
                else:
                    w = 1.0
                weights.append(w)
            elif self._demand_weight_by_error and action in ("idle", "off"):
                weights.append(0.0)
            elif not self._demand_weight_by_error and action != "heating":
                weights.append(0.0)
        if not weights:
            _LOGGER.warning(
                "All demand entities (%s) are unavailable — failing safe to no-demand",
                ", ".join(self._demand_entities),
            )
            return 0.0
        return sum(weights) / len(weights)

    def _effective_out_min(self) -> float:
        """Return effective output minimum based on demand index.

        In lockout mode: returns power_min when index == 0.
        In envelope mode: scales by floor_frac * index.
        """
        if self._demand_mode != "envelope":
            return float(self._power_min)
        index = self._demand_index()
        if index is None:
            return float(self._power_min)
        return float(self._power_min) + (float(self._power_max) - float(self._power_min)) * self._demand_floor_frac * index

    def _effective_out_max(self) -> float:
        """Return effective output maximum based on demand index.

        In envelope mode: scales ceiling by (1 - (1-ceiling_frac) * (1-index)).
        """
        if self._demand_mode != "envelope":
            return float(self._power_max)
        index = self._demand_index()
        if index is None:
            return float(self._power_max)
        return float(self._power_min) + (float(self._power_max) - float(self._power_min)) * (1.0 - (1.0 - self._demand_ceiling_frac) * (1.0 - index))

    async def _run_pid_step(self) -> None:
        """Compute PID output and push to the miner if it changed meaningfully."""
        temp = self._current_temperature()
        if temp is None:
            return
        # If the miner is off we have nothing to regulate — don't burn a token.
        if not self.coordinator.data.get("is_mining"):
            return

        now = time()
        if self._slope_last_pv is not None and self._slope_ewma_tau_s > 0:
            prev_t, prev_v = self._slope_last_pv
            dt = now - prev_t
            if dt > 0:
                inst = (temp - prev_v) / (dt / 60.0)
                alpha = 1 - math.exp(-dt / self._slope_ewma_tau_s)
                self._slope_ewma = (
                    inst if self._slope_ewma is None
                    else self._slope_ewma + alpha * (inst - self._slope_ewma)
                )
        self._slope_last_pv = (now, temp)

        user_target = self._pid_state.get("target")
        if user_target is None:
            user_target = self._default_target
        user_target = float(user_target)

        now = time()
        last = self._last_input_time or now

        # Setpoint ramp: when enabled, move the effective setpoint toward the
        # user target at ≤ ramp_rate °F/min. Seeded from current PV on first
        # tick so a cold plant never sees a huge step error.
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
            # No ramping — hand the raw user setpoint to the PID. Keep
            # _ramped_target in sync so re-enabling ramp later starts cleanly.
            self._ramped_target = user_target
            target = user_target

        # Integral band: only freeze accumulation when we're far from SP AND
        # the output has hit a saturation rail. Far-from-SP-with-headroom is
        # the disturbance-recovery case where the integrator must push — an
        # earlier "always freeze when far from SP" version stalled recovery
        # with PV well below target and the integrator drained to ~0.
        error_abs = abs(target - float(temp))
        # Snapshot before calc() so we can rewind without copying calc()'s
        # internal logic. Cheap; only restored when we decide to freeze.
        integral_snapshot = self._pid.integral

        # Read outdoor temperature for feedforward compensation. If sensor is
        # not configured or unavailable, pass None (leaves _dext=0, _external=0).
        # Uses blended forecast temperature when weather entity is configured.
        outdoor_temp = None
        if self._outdoor_temp_sensor_id is not None and self._ke > 0:
            outdoor_temp = self._blended_outdoor_temp(now)

        # Envelope mode: apply demand-scaled output bounds before calc() so the
        # integrator sees the real operating range. Lockout mode uses binary
        # logic after calc() and ignores these bounds.
        if self._demand_mode == "envelope" and self._demand_entities:
            self._pid.out_min = self._effective_out_min()
            self._pid.out_max = self._effective_out_max()

        # Price/surplus envelope: further constrain out_min/out_max based on
        # time-of-use price and solar/battery surplus. Applied after demand
        # envelope, so order of precedence is:
        # safety_caps > demand_envelope > tou/surplus_envelope > pid_output
        price_score = 1.0
        surplus_score = 1.0
        if self._price_sensor_id is not None or self._surplus_sensor_id is not None:
            current_out_min = self._pid.out_min
            current_out_max = self._pid.out_max

            if self._price_sensor_id is not None and self._price_high > self._price_low:
                price = self._read_price_sensor()
                if price is not None:
                    price_score = (self._price_high - price) / (self._price_high - self._price_low)
                    price_score = max(0.0, min(1.0, price_score))
                    price_score = round(price_score / 0.05) * 0.05

            if self._surplus_sensor_id is not None and self._surplus_full > self._surplus_deficit:
                surplus = self._read_surplus_sensor()
                if surplus is not None:
                    surplus_score = (surplus - self._surplus_deficit) / (self._surplus_full - self._surplus_deficit)
                    surplus_score = max(0.0, min(1.0, surplus_score))
                    surplus_score = round(surplus_score / 0.05) * 0.05

            power_range = current_out_max - current_out_min
            multiplier = min(price_score, surplus_score, 1.0)
            self._pid.out_max = current_out_min + power_range * multiplier
            self._pid.out_min = current_out_min

        # Store effective bounds for diagnostic sensors
        self._pid_state["out_max_effective"] = int(self._pid.out_max)
        self._pid_state["out_min_effective"] = int(self._pid.out_min)

        try:
            output, did_calc = self._pid.calc(
                input_val=float(temp),
                set_point=float(target),
                input_time=now,
                last_input_time=last,
                ext_temp=outdoor_temp,
            )
        except Exception as err:  # defensive — don't break coordinator loop
            _LOGGER.exception("PID calculation failed: %s", err)
            return

        sat_tol = 1.0
        output_saturated = (
            output >= float(self._power_max) - sat_tol
            or output <= float(self._power_min) + sat_tol
        )
        integral_frozen = (
            self._integral_band > 0
            and error_abs > self._integral_band
            and output_saturated
        )
        if integral_frozen:
            # Rewind the integrator and recompute output without its accumulated
            # growth. Update _pid._output too so next tick's anti-windup guard
            # (which reads _last_output) sees the clamped value we actually used.
            self._pid.integral = integral_snapshot
            output = (
                self._pid.proportional
                + integral_snapshot
                + self._pid.derivative
                + self._pid.external
            )
            output = max(min(output, float(self._power_max)), float(self._power_min))
            self._pid._output = output

        self._last_input_time = now
        if not did_calc:
            return

        requested_power = int(round(output))
        new_power = requested_power
        current_limit = self.coordinator.data.get("wattage_limit") or 0

        # Safety cap veto: chip-temp guards the *miner*; supply-temp caps guard
        # the *plant* (boiler heat exchanger sees a stagnant loop and can trip
        # its own high-limit even at miner power_min). All three force
        # power_min and engage the safety binary sensor; the supply lockout
        # additionally calls power_off() to latch mining off until the
        # operator reviews and toggles Mining Control back on.
        safety_engaged = False
        supply_lockout = False
        chip = self._chip_temp()
        if chip is not None and chip >= self._chip_temp_safety_cap:
            new_power = self._power_min
            safety_engaged = True
            _LOGGER.warning(
                "Chip temp %.1f°F ≥ cap %.1f°F — forcing %dW (PID override)",
                chip,
                self._chip_temp_safety_cap,
                new_power,
            )
        if temp >= self._supply_temp_lockout:
            new_power = self._power_min
            safety_engaged = True
            supply_lockout = True
            _LOGGER.critical(
                "Supply temp %.1f°F ≥ lockout %.1f°F — stopping mining (latched)",
                temp,
                self._supply_temp_lockout,
            )
        elif temp >= self._supply_temp_safety_cap:
            new_power = self._power_min
            safety_engaged = True
            _LOGGER.warning(
                "Supply temp %.1f°F ≥ cap %.1f°F — forcing %dW (PID override)",
                temp,
                self._supply_temp_safety_cap,
                new_power,
            )

        # Demand lockout: in lockout mode (default), if no configured thermostat
        # is calling for heat, the loop pump is likely idle and we have no flow
        # to dissipate power into. Force power_min and engage safety; auto-resumes
        # the next time any demand entity returns to "heating". In envelope mode,
        # the output bounds are already scaled before calc().
        demand_index = self._demand_index()
        if self._demand_mode == "lockout" and self._demand_entities:
            if demand_index == 0.0 or demand_index is None:
                new_power = self._power_min
                safety_engaged = True
                if not self._no_demand_logged:
                    _LOGGER.warning(
                        "No demand from %s — forcing %dW (PID override) until a thermostat calls",
                        ", ".join(self._demand_entities),
                        new_power,
                    )
                    self._no_demand_logged = True
            elif self._no_demand_logged:
                _LOGGER.info("Demand returned — releasing no-demand lockout")
                self._no_demand_logged = False

        # Publish PID internals on every successful calc (even when we don't
        # actuate) so the diagnostic sensors / charts stay live while the loop
        # idles at target. requested_output stays at the raw PID desire;
        # output reflects the safety clamp so Chart C shows the veto.
        self._pid_state.update(
            {
                "error": self._pid.error,
                "proportional": self._pid.proportional,
                "integral": self._pid.integral,
                "derivative": self._pid.derivative,
                "external": self._pid.external,
                "output": new_power,
                "requested_output": requested_power,
                "enabled": True,
                "safety_engaged": safety_engaged,
                "pv_slope": self._slope_ewma,
                "demand_index": demand_index,
            }
        )

        # Hard lockout: stop mining outright, then return without trying to
        # set a power limit (the miner is going off — adjusting limits would
        # race the power-off command). User must toggle Mining Control back
        # on to recover; bumpless transfer at re-enable will re-seed the PID.
        if supply_lockout:
            try:
                await self.coordinator.api.power_off()
                self._last_commanded_power = None
                self._last_command_time = time()
            except Exception as err:
                _LOGGER.error("Failed to stop mining on supply-temp lockout: %s", err)
            return

        # Decide whether to actuate. Each adjust_power_limit call restarts
        # mining, so we gate on both magnitude (don't fire for sub-step wiggles)
        # and time (don't fire more often than the configured interval). The
        # safety-cap path bypasses the interval — overheat commands must go
        # out on the next tick, not the full interval later.
        # Throttle is asymmetric: power-up commands (zones called for heat) use
        # the shorter increase-interval; power-down commands use the longer one.
        reference = (
            self._last_commanded_power
            if self._last_commanded_power is not None
            else current_limit
        )
        delta = abs(new_power - reference)
        elapsed = time() - self._last_command_time
        # Step size scales with proximity to setpoint: coarse far away,
        # medium in the mid-band, fine near target. Lets the loop nudge
        # precisely without firing for sub-step wiggles when way off.
        if error_abs <= self._fine_step_band:
            effective_min_step = self._min_power_step_fine
            band_label = "fine"
        elif error_abs <= self._coarse_step_band:
            effective_min_step = self._min_power_step_medium
            band_label = "medium"
        else:
            effective_min_step = self._min_power_step
            band_label = "coarse"
        if self._slope_ewma is not None and self._slope_ewma_tau_s > 0:
            target_dir = math.copysign(1, target - temp)
            if math.copysign(1, self._slope_ewma) == target_dir and abs(self._slope_ewma) > 0.5:
                if effective_min_step == self._min_power_step:
                    effective_min_step = self._min_power_step_medium
                    band_label = "coarse→medium"
                elif effective_min_step == self._min_power_step_medium:
                    effective_min_step = self._min_power_step_fine
                    band_label = "medium→fine"
        step_ok = delta >= effective_min_step
        effective_interval = (
            self._min_adjust_interval_increase
            if new_power > reference
            else self._min_adjust_interval
        )
        interval_ok = elapsed >= effective_interval
        safety_fire = safety_engaged and step_ok

        if not safety_fire and not (step_ok and interval_ok):
            _LOGGER.debug(
                "PID actuation throttled: Δ=%dW (need %dW, %s band), elapsed=%.0fs (need %ds, %s)",
                delta,
                effective_min_step,
                band_label,
                elapsed,
                effective_interval,
                "increase" if new_power > reference else "decrease",
            )
            # Keep pid_state["output"] reflecting the *last actuated* value so
            # Chart C's gap between requested_output and output visualizes the
            # throttle. Fix that up — we overwrote it unconditionally above.
            self._pid_state["output"] = (
                self._last_commanded_power
                if self._last_commanded_power is not None
                else None
            )
            return

        _LOGGER.info(
            "PID: temp=%.1f°F target=%.1f°F → power %dW (was %dW, err=%.2f)",
            temp,
            target,
            new_power,
            reference,
            self._pid.error,
        )
        try:
            await self.coordinator.api.set_power_limit(new_power)
            self._last_commanded_power = new_power
            self._last_command_time = time()
        except Exception as err:
            _LOGGER.error("Failed to set power limit to %dW: %s", new_power, err)
