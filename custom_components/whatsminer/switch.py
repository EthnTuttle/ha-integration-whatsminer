"""Support for Whatsminer switches."""
from __future__ import annotations

import logging
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
    CONF_PID_KD,
    CONF_PID_KI,
    CONF_PID_KP,
    CONF_PID_MIN_ADJUST_INTERVAL,
    CONF_PID_MIN_POWER_STEP,
    CONF_PID_TARGET_TEMP,
    CONF_POWER_MAX,
    CONF_POWER_MIN,
    DEFAULT_CHIP_TEMP_SAFETY_CAP,
    DEFAULT_DEFAULT_POWER_LIMIT,
    DEFAULT_PID_KD,
    DEFAULT_PID_KI,
    DEFAULT_PID_KP,
    DEFAULT_PID_MIN_ADJUST_INTERVAL,
    DEFAULT_PID_MIN_POWER_STEP,
    DEFAULT_PID_TARGET_TEMP,
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
            default_target=config.get(CONF_PID_TARGET_TEMP, DEFAULT_PID_TARGET_TEMP),
            external_sensor_id=config.get(CONF_EXTERNAL_TEMP_SENSOR) or None,
            default_power_limit=config.get(
                CONF_DEFAULT_POWER_LIMIT, DEFAULT_DEFAULT_POWER_LIMIT
            ),
            min_power_step=config.get(
                CONF_PID_MIN_POWER_STEP, DEFAULT_PID_MIN_POWER_STEP
            ),
            min_adjust_interval=config.get(
                CONF_PID_MIN_ADJUST_INTERVAL, DEFAULT_PID_MIN_ADJUST_INTERVAL
            ),
            chip_temp_safety_cap=config.get(
                CONF_CHIP_TEMP_SAFETY_CAP, DEFAULT_CHIP_TEMP_SAFETY_CAP
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
        default_target: float,
        external_sensor_id: str | None,
        default_power_limit: int,
        min_power_step: int,
        min_adjust_interval: int,
        chip_temp_safety_cap: float,
    ) -> None:
        """Initialize the PID switch."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.data['mac']}_pid_mode"
        self._attr_name = "PID Mode"
        self._pid_state = pid_state
        self._power_min = power_min
        self._power_max = power_max
        self._kp = kp  # kept for bumpless-transfer seed in async_turn_on
        self._default_target = default_target
        self._external_sensor_id = external_sensor_id
        self._default_power_limit = default_power_limit
        self._min_power_step = min_power_step
        self._min_adjust_interval = min_adjust_interval
        self._chip_temp_safety_cap = chip_temp_safety_cap
        self._external_unavail_logged = False
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

    def _current_temperature(self) -> float | None:
        """Return the regulated variable read from the external sensor.

        The miner's own chip temperature is deliberately not used — it's noisy
        and the miner firmware already manages its own thermal envelope. Returns
        None if no external sensor is configured or the sensor is unavailable.
        """
        if self._external_sensor_id is None:
            return None
        return self._read_external_sensor_celsius()

    async def _run_pid_step(self) -> None:
        """Compute PID output and push to the miner if it changed meaningfully."""
        temp = self._current_temperature()
        if temp is None:
            return
        # If the miner is off we have nothing to regulate — don't burn a token.
        if not self.coordinator.data.get("is_mining"):
            return

        target = self._pid_state.get("target")
        if target is None:
            target = self._default_target

        now = time()
        last = self._last_input_time or now
        try:
            output, did_calc = self._pid.calc(
                input_val=float(temp),
                set_point=float(target),
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
        new_power = requested_power
        current_limit = self.coordinator.data.get("wattage_limit") or 0

        # Safety cap veto: chip-temp is NOT a PID input, but it retains a hard
        # override on output. If the chip crosses the configured cap, force the
        # actuator to power_min regardless of what the external-sensor loop
        # wants. Firmware will also self-protect, but HA shouldn't be blind.
        safety_engaged = False
        chip = self._chip_temp()
        if chip is not None and chip >= self._chip_temp_safety_cap:
            new_power = self._power_min
            safety_engaged = True
            _LOGGER.warning(
                "Chip temp %.1f°C ≥ cap %.1f°C — forcing %dW (PID override)",
                chip,
                self._chip_temp_safety_cap,
                new_power,
            )

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
                "output": new_power,
                "requested_output": requested_power,
                "enabled": True,
                "safety_engaged": safety_engaged,
            }
        )

        # Decide whether to actuate. Each adjust_power_limit call restarts
        # mining, so we gate on both magnitude (don't fire for sub-step wiggles)
        # and time (don't fire more often than the configured interval). The
        # safety-cap path bypasses the interval — overheat commands must go
        # out on the next tick, not 10 minutes later.
        reference = (
            self._last_commanded_power
            if self._last_commanded_power is not None
            else current_limit
        )
        delta = abs(new_power - reference)
        elapsed = time() - self._last_command_time
        step_ok = delta >= self._min_power_step
        interval_ok = elapsed >= self._min_adjust_interval
        safety_fire = safety_engaged and step_ok

        if not safety_fire and not (step_ok and interval_ok):
            _LOGGER.debug(
                "PID actuation throttled: Δ=%dW (need %dW), elapsed=%.0fs (need %ds)",
                delta,
                self._min_power_step,
                elapsed,
                self._min_adjust_interval,
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
            "PID: temp=%.1f°C target=%.1f°C → power %dW (was %dW, err=%.2f)",
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
