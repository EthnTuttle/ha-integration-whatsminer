"""Support for Whatsminer number controls."""
from __future__ import annotations

import logging

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_PID_TARGET_TEMP, DEFAULT_PID_TARGET_TEMP, DOMAIN
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

    default_target = config.get(CONF_PID_TARGET_TEMP, DEFAULT_PID_TARGET_TEMP)

    # The manual Power Limit slider was removed in v4: the controller is always
    # driving the limit, so a second actuator would only fight it. The
    # read-only Power Limit sensor remains.
    async_add_entities([WhatsminerPIDTargetNumber(coordinator, pid_state, default_target)])


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
