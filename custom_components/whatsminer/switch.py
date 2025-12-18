"""Support for Whatsminer switches."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity
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
    """Set up Whatsminer switches from a config entry."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: WhatsminerCoordinator = data["coordinator"]

    entities = [
        WhatsminerMiningSwitch(coordinator),
    ]

    async_add_entities(entities)


class WhatsminerMiningSwitch(CoordinatorEntity, SwitchEntity):
    """Representation of Whatsminer mining control switch."""

    _attr_icon = "mdi:power"
    _attr_has_entity_name = True

    def __init__(self, coordinator: WhatsminerCoordinator) -> None:
        """Initialize the switch."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.data['mac']}_mining_control"
        self._attr_name = "Mining Control"

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

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on mining (power on hashboards)."""
        try:
            _LOGGER.info(f"Powering on hashboards on {self.coordinator.miner_ip}")
            result = await self.coordinator.api.power_on()
            _LOGGER.info(f"Power on command sent to {self.coordinator.miner_ip}: {result}")
            # Refresh data after command
            await self.coordinator.async_request_refresh()
        except Exception as err:
            _LOGGER.error(f"Failed to power on {self.coordinator.miner_ip}: {err}")
            raise

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off mining (power off hashboards)."""
        try:
            _LOGGER.info(f"Powering off hashboards on {self.coordinator.miner_ip}")
            result = await self.coordinator.api.power_off()
            _LOGGER.info(f"Power off command sent to {self.coordinator.miner_ip}: {result}")
            # Refresh data after command
            await self.coordinator.async_request_refresh()
        except Exception as err:
            _LOGGER.error(f"Failed to power off {self.coordinator.miner_ip}: {err}")
            raise
