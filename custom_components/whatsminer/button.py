"""Support for Whatsminer buttons."""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import WhatsminerCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Whatsminer buttons from a config entry."""
    data = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([WhatsminerResetLockoutButton(data["coordinator"], data)])


class WhatsminerResetLockoutButton(CoordinatorEntity, ButtonEntity):
    """Clear the latched supply-temperature lockout.

    The controller owns the latch and refuses the reset unless the supply
    probe reads below the soft safety cap.
    """

    _attr_entity_category = EntityCategory.CONFIG
    _attr_has_entity_name = True
    _attr_icon = "mdi:lock-reset"

    def __init__(self, coordinator: WhatsminerCoordinator, data: dict) -> None:
        """Initialize the button."""
        super().__init__(coordinator)
        self._data = data
        self._attr_unique_id = f"{coordinator.data['mac']}_reset_supply_lockout"
        self._attr_name = "Reset Supply Lockout"

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
    def available(self) -> bool:
        """Available even while the miner is offline — the latch is local."""
        return True

    async def async_press(self) -> None:
        """Reset the lockout latch."""
        controller = self._data.get("controller")
        if controller is None:
            raise HomeAssistantError("Controller is not set up yet")
        await controller.async_reset_lockout()
