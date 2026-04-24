"""Support for Whatsminer binary sensors."""
from __future__ import annotations

import logging

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import WhatsminerCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Whatsminer binary sensors from a config entry."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: WhatsminerCoordinator = data["coordinator"]
    pid_state: dict = data["pid_state"]

    entities = [
        WhatsminerMiningSensor(coordinator),
        WhatsminerPIDSafetyBinarySensor(coordinator, pid_state),
    ]

    async_add_entities(entities)


class WhatsminerMiningSensor(CoordinatorEntity, BinarySensorEntity):
    """Representation of Whatsminer mining status."""

    _attr_device_class = BinarySensorDeviceClass.RUNNING
    _attr_has_entity_name = True

    def __init__(self, coordinator: WhatsminerCoordinator) -> None:
        """Initialize the binary sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.data['mac']}_is_mining"
        self._attr_name = "Mining Status"
        self._attr_icon = "mdi:pickaxe"

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
        """Return true if the miner is mining."""
        return self.coordinator.data.get("is_mining", False)

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return self.coordinator.available and self.coordinator.last_update_success


class WhatsminerPIDSafetyBinarySensor(CoordinatorEntity, BinarySensorEntity):
    """Reflects the PID safety-cap override state.

    On when the PID has clamped output to power_min because chip temp crossed
    the configured safety cap. Chip temp is NOT a PID input — this sensor just
    surfaces the veto so dashboards and automations can react to overheat
    events that the miner firmware would otherwise absorb silently.
    """

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_has_entity_name = True
    _attr_icon = "mdi:thermometer-alert"

    def __init__(
        self,
        coordinator: WhatsminerCoordinator,
        pid_state: dict,
    ) -> None:
        """Initialize the safety binary sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.data['mac']}_pid_safety_engaged"
        self._attr_name = "PID Safety Engaged"
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
    def is_on(self) -> bool:
        """Return true if the safety cap is currently engaged."""
        return bool(self._pid_state.get("safety_engaged"))

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return self.coordinator.available and self.coordinator.last_update_success
