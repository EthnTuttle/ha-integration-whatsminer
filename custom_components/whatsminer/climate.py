"""PID-controlled climate entity for Whatsminer power regulation.

Reads chip temperature from the coordinator and modulates the miner's
power limit via the existing Whatsminer API. HVAC mode COOL enables the
PID loop; OFF disables it (power limit is left at whatever the user or
PID last set it to).
"""
from __future__ import annotations

import logging
from time import time
from typing import Any

from homeassistant.components.climate import (
    ATTR_TEMPERATURE,
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util.unit_conversion import TemperatureConverter

from .const import (
    CONF_CHIP_TEMP_SAFETY_CAP,
    CONF_EXTERNAL_TEMP_SENSOR,
    CONF_PID_KD,
    CONF_PID_KI,
    CONF_PID_KP,
    CONF_PID_TARGET_TEMP,
    CONF_POWER_MAX,
    CONF_POWER_MIN,
    DEFAULT_CHIP_TEMP_SAFETY_CAP,
    DEFAULT_PID_KD,
    DEFAULT_PID_KI,
    DEFAULT_PID_KP,
    DEFAULT_PID_TARGET_TEMP,
    DEFAULT_POWER_MAX,
    DEFAULT_POWER_MIN,
    DOMAIN,
)
from .pid_controller import PID

_LOGGER = logging.getLogger(__name__)

# Only actuate when the PID output moves at least this many watts from the
# current miner setting — keeps us from sending an encrypted TCP command on
# every 30s poll for sub-watt wiggles.
_MIN_POWER_STEP_W = 25


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Whatsminer PID thermostat from a config entry."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator = data["coordinator"]
    config = data["config"]

    async_add_entities(
        [
            WhatsminerThermostat(
                coordinator,
                pid_state=data["pid_state"],
                power_min=config.get(CONF_POWER_MIN, DEFAULT_POWER_MIN),
                power_max=config.get(CONF_POWER_MAX, DEFAULT_POWER_MAX),
                kp=config.get(CONF_PID_KP, DEFAULT_PID_KP),
                ki=config.get(CONF_PID_KI, DEFAULT_PID_KI),
                kd=config.get(CONF_PID_KD, DEFAULT_PID_KD),
                default_target=config.get(
                    CONF_PID_TARGET_TEMP, DEFAULT_PID_TARGET_TEMP
                ),
                external_sensor_id=config.get(CONF_EXTERNAL_TEMP_SENSOR) or None,
                chip_temp_safety_cap=config.get(
                    CONF_CHIP_TEMP_SAFETY_CAP, DEFAULT_CHIP_TEMP_SAFETY_CAP
                ),
            )
        ]
    )


class WhatsminerThermostat(CoordinatorEntity, ClimateEntity, RestoreEntity):
    """PID thermostat that modulates miner power to hold a target chip temp."""

    _attr_has_entity_name = True
    _attr_name = "Temperature Control"
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.COOL]
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )
    _attr_min_temp = 20
    _attr_max_temp = 100
    _attr_target_temperature_step = 1

    def __init__(
        self,
        coordinator,
        pid_state: dict,
        power_min: int,
        power_max: int,
        kp: float,
        ki: float,
        kd: float,
        default_target: float,
        external_sensor_id: str | None,
        chip_temp_safety_cap: float,
    ) -> None:
        """Initialize the thermostat."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.data['mac']}_pid_thermostat"
        self._pid_state = pid_state
        self._power_min = power_min
        self._power_max = power_max
        self._target_temp: float = default_target
        self._hvac_mode: HVACMode = HVACMode.OFF
        self._last_input_time: float | None = None
        self._last_commanded_power: int | None = None
        self._external_sensor_id = external_sensor_id
        self._chip_temp_safety_cap = chip_temp_safety_cap
        self._external_unavail_logged = False
        self._pid = PID(
            kp=kp,
            ki=ki,
            kd=kd,
            out_min=float(power_min),
            out_max=float(power_max),
            sampling_period=0,
        )
        # Seed target so pid_target_temp sensor has a value before any PID run.
        self._pid_state["target"] = self._target_temp

    async def async_added_to_hass(self) -> None:
        """Restore state after a restart."""
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is None:
            return
        if last_state.state in (HVACMode.OFF, HVACMode.COOL):
            self._hvac_mode = HVACMode(last_state.state)
        target = last_state.attributes.get(ATTR_TEMPERATURE)
        if target is not None:
            try:
                self._target_temp = float(target)
            except (TypeError, ValueError):
                pass
        self._pid_state["target"] = self._target_temp
        self._pid_state["enabled"] = self._hvac_mode == HVACMode.COOL

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
    def available(self) -> bool:
        """Return if entity is available."""
        return self.coordinator.available

    @property
    def current_temperature(self) -> float | None:
        """Return the regulated variable: external sensor if configured, else chip temp."""
        if self._external_sensor_id:
            return self._read_external_sensor_celsius()
        return self._chip_temp()

    def _chip_temp(self) -> float | None:
        """Return the miner's own chip/board temperature in °C."""
        temp = self.coordinator.data.get("temperature_avg")
        return float(temp) if temp and temp > 0 else None

    def _read_external_sensor_celsius(self) -> float | None:
        """Read the user-selected temperature sensor, converting to °C if needed."""
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
        if unit and unit != UnitOfTemperature.CELSIUS:
            try:
                value = TemperatureConverter.convert(
                    value, unit, UnitOfTemperature.CELSIUS
                )
            except Exception as err:
                _LOGGER.warning(
                    "Could not convert %s from %s to °C: %s",
                    self._external_sensor_id,
                    unit,
                    err,
                )
                return None
        return value

    @property
    def target_temperature(self) -> float:
        return self._target_temp

    @property
    def hvac_mode(self) -> HVACMode:
        return self._hvac_mode

    @property
    def hvac_action(self) -> HVACAction:
        if self._hvac_mode == HVACMode.OFF:
            return HVACAction.OFF
        if not self.coordinator.data.get("is_mining"):
            return HVACAction.IDLE
        return HVACAction.COOLING

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose PID internals for tuning visibility."""
        return {
            "pid_error": round(self._pid.error, 2),
            "pid_proportional": round(self._pid.proportional, 2),
            "pid_integral": round(self._pid.integral, 2),
            "pid_derivative": round(self._pid.derivative, 2),
            "pid_dt": round(self._pid.dt, 2),
            "last_commanded_power": self._last_commanded_power,
        }

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Handle target temperature change."""
        temp = kwargs.get(ATTR_TEMPERATURE)
        if temp is None:
            return
        self._target_temp = float(temp)
        self._pid_state["target"] = self._target_temp
        self.async_write_ha_state()

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Handle HVAC mode change — turning COOL on/off starts/stops PID."""
        if hvac_mode not in (HVACMode.OFF, HVACMode.COOL):
            return
        if hvac_mode == self._hvac_mode:
            return
        self._hvac_mode = hvac_mode
        if hvac_mode == HVACMode.OFF:
            # Reset PID history so we don't carry stale derivative/integral terms
            # into the next run.
            self._pid.clear_samples()
            self._pid.integral = 0.0
            self._last_input_time = None
            # Null out PID-internal sensors so charts show a clean gap while
            # the loop is paused. Keep "target" populated so the setpoint line
            # keeps rendering.
            self._pid_state.update(
                {
                    "error": None,
                    "proportional": None,
                    "integral": None,
                    "derivative": None,
                    "output": None,
                    "requested_output": None,
                    "safety_engaged": False,
                    "enabled": False,
                }
            )
        else:
            self._pid_state["enabled"] = True
        self.async_write_ha_state()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Run a PID step on each coordinator poll."""
        if self._hvac_mode == HVACMode.COOL:
            self.hass.async_create_task(self._run_pid_step())
        super()._handle_coordinator_update()

    async def _run_pid_step(self) -> None:
        """Compute PID output and push to the miner if it changed meaningfully."""
        temp = self.current_temperature
        if temp is None:
            return
        # If the miner is off we have nothing to regulate — don't burn a token.
        if not self.coordinator.data.get("is_mining"):
            return

        now = time()
        last = self._last_input_time or now
        try:
            output, did_calc = self._pid.calc(
                input_val=float(temp),
                set_point=float(self._target_temp),
                input_time=now,
                last_input_time=last,
            )
        except Exception as err:  # defensive — don't break coordinator loop
            _LOGGER.exception("PID calculation failed: %s", err)
            return

        self._last_input_time = now
        if not did_calc:
            return

        requested_power = int(round(output))
        current_limit = self.coordinator.data.get("wattage_limit") or 0

        # Safety cap: when an external sensor drives the PID, the regulated
        # variable isn't the miner's own chip temp anymore, so we need an
        # independent override that slams power down if the chip overheats.
        # Only applies when external_sensor_id is configured — chip-targeted PID
        # already self-regulates.
        safety_engaged = False
        new_power = requested_power
        if self._external_sensor_id:
            chip = self._chip_temp()
            if chip is not None and chip > self._chip_temp_safety_cap:
                new_power = self._power_min
                safety_engaged = True
                _LOGGER.warning(
                    "Chip temp %.1f°C exceeds safety cap %.1f°C — clamping power to %dW",
                    chip,
                    self._chip_temp_safety_cap,
                    new_power,
                )

        # Publish PID internals on every successful calc (even when we don't
        # actuate) so the diagnostic sensors / charts stay live while the loop
        # idles at target.
        self._pid_state.update(
            {
                "error": self._pid.error,
                "proportional": self._pid.proportional,
                "integral": self._pid.integral,
                "derivative": self._pid.derivative,
                "output": new_power,
                "requested_output": requested_power,
                "safety_engaged": safety_engaged,
                "enabled": True,
            }
        )

        # Only push if the change is meaningful. Compare against both what the
        # miner reports and what we last commanded — avoids stale reads causing
        # repeat commands while a prior set_power_limit is still propagating.
        reference = (
            self._last_commanded_power
            if self._last_commanded_power is not None
            else current_limit
        )
        if abs(new_power - reference) < _MIN_POWER_STEP_W:
            return

        _LOGGER.info(
            "PID: temp=%.1f°C target=%.1f°C → power %dW (was %dW, err=%.2f%s)",
            temp,
            self._target_temp,
            new_power,
            reference,
            self._pid.error,
            ", SAFETY CAP" if safety_engaged else "",
        )
        try:
            await self.coordinator.api.set_power_limit(new_power)
            self._last_commanded_power = new_power
        except Exception as err:
            _LOGGER.error("Failed to set power limit to %dW: %s", new_power, err)
