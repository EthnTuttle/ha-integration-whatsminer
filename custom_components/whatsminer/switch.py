"""Support for Whatsminer switches."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
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

    entities = [
        WhatsminerMiningSwitch(coordinator),
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
