"""Support for Whatsminer number controls."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfPower, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_PID_TARGET_TEMP,
    CONF_POWER_MAX,
    CONF_POWER_MIN,
    DEFAULT_PID_TARGET_TEMP,
    DEFAULT_POWER_MAX,
    DEFAULT_POWER_MIN,
    DOMAIN,
)
from .coordinator import WhatsminerCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Whatsminer number controls from a config entry."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: WhatsminerCoordinator = data["coordinator"]
    config = data["config"]
    pid_state: dict = data["pid_state"]

    power_min = config.get(CONF_POWER_MIN, DEFAULT_POWER_MIN)
    power_max = config.get(CONF_POWER_MAX, DEFAULT_POWER_MAX)
    default_target = config.get(CONF_PID_TARGET_TEMP, DEFAULT_PID_TARGET_TEMP)

    entities = [
        WhatsminerPowerLimitNumber(coordinator, power_min, power_max, pid_state),
        WhatsminerPIDTargetNumber(coordinator, pid_state, default_target),
    ]

    async_add_entities(entities)


class WhatsminerPowerLimitNumber(CoordinatorEntity, NumberEntity):
    """Representation of Whatsminer power limit control."""

    _attr_icon = "mdi:flash"
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_mode = NumberMode.SLIDER
    _attr_native_step = 100
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: WhatsminerCoordinator,
        power_min: int,
        power_max: int,
        pid_state: dict,
    ) -> None:
        """Initialize the number control."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.data['mac']}_power_limit"
        self._attr_name = "Power Limit"
        self._attr_native_min_value = power_min
        self._attr_native_max_value = power_max
        self._pid_state = pid_state

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
    def native_value(self) -> float | None:
        """Return the current power limit."""
        return self.coordinator.data.get("wattage_limit")

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return self.coordinator.available and self.coordinator.last_update_success

    async def async_set_native_value(self, value: float) -> None:
        """Set new power limit."""
        # PID Mode rewrites the power limit on every tick. Block manual sets
        # with a user-visible error rather than silently letting them get
        # clobbered.
        if self._pid_state.get("enabled"):
            raise HomeAssistantError(
                "PID Mode is controlling power. Turn off PID Mode to set the "
                "power limit manually."
            )
        try:
            power_limit = int(value)

            # Validate range
            if not (self._attr_native_min_value <= power_limit <= self._attr_native_max_value):
                _LOGGER.error(
                    f"Power limit {power_limit}W is outside allowed range "
                    f"({self._attr_native_min_value}-{self._attr_native_max_value}W)"
                )
                return

            _LOGGER.info(
                f"Setting power limit to {power_limit}W on {self.coordinator.miner_ip} "
                f"(miner will reboot)"
            )

            await self.coordinator.api.set_power_limit(power_limit)

            _LOGGER.info(
                f"Power limit set to {power_limit}W on {self.coordinator.miner_ip}. "
                f"Miner is rebooting..."
            )

            # Refresh data after command (though miner will be rebooting)
            await self.coordinator.async_request_refresh()

        except Exception as err:
            _LOGGER.error(
                f"Failed to set power limit on {self.coordinator.miner_ip}: {err}"
            )
            raise


class WhatsminerPIDTargetNumber(CoordinatorEntity, NumberEntity, RestoreEntity):
    """Dashboard-adjustable target temperature for the PID loop."""

    _attr_icon = "mdi:thermometer-lines"
    _attr_native_unit_of_measurement = UnitOfTemperature.FAHRENHEIT
    _attr_mode = NumberMode.BOX
    _attr_native_min_value = 68
    _attr_native_max_value = 212
    _attr_native_step = 1
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: WhatsminerCoordinator,
        pid_state: dict,
        default_target: float,
    ) -> None:
        """Initialize the target-temp number."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.data['mac']}_pid_target_temperature"
        self._attr_name = "PID Target Temperature"
        self._pid_state = pid_state
        self._default_target = default_target

    async def async_added_to_hass(self) -> None:
        """Restore the last target temp across restarts."""
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is None or last_state.state in (None, "unknown", "unavailable"):
            return
        try:
            self._pid_state["target"] = float(last_state.state)
        except (TypeError, ValueError):
            pass

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
    def native_value(self) -> float | None:
        """Return the current PID target."""
        value = self._pid_state.get("target")
        return value if value is not None else self._default_target

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return self.coordinator.available and self.coordinator.last_update_success

    async def async_set_native_value(self, value: float) -> None:
        """Update the PID target temperature."""
        self._pid_state["target"] = float(value)
        self.async_write_ha_state()
