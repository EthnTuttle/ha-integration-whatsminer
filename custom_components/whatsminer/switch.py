"""Support for Whatsminer switches.

Only Mining Control remains. It is the manual emergency override for the
controller in controller.py: a user's OFF is never auto-resumed, and a user's
ON over a demand-shutoff stop suppresses further stops until a thermostat
calls for heat again.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
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
    async_add_entities([WhatsminerMiningSwitch(data["coordinator"], data)])


class WhatsminerMiningSwitch(CoordinatorEntity, SwitchEntity):
    """Representation of Whatsminer mining control switch.

    Uses optimistic updates to handle the miner's slow state transitions.
    When a command is sent, the switch immediately reflects the target state
    and ignores the reported state for a grace period while the miner transitions.
    """

    _attr_icon = "mdi:power"
    _attr_has_entity_name = True

    def __init__(self, coordinator: WhatsminerCoordinator, data: dict) -> None:
        """Initialize the switch."""
        super().__init__(coordinator)
        self._data = data
        self._pid_state: dict = data["pid_state"]
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
        """
        actual_state = self.coordinator.data.get("is_mining", False)

        if self._assumed_state is not None and self._assumed_state_time is not None:
            if actual_state == self._assumed_state:
                self._assumed_state = None
                self._assumed_state_time = None
                return actual_state
            if datetime.now() - self._assumed_state_time < OPTIMISTIC_STATE_TIMEOUT:
                return self._assumed_state
            self._assumed_state = None
            self._assumed_state_time = None

        return actual_state

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return self.coordinator.available and self.coordinator.last_update_success

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose why the controller may have the miner off."""
        shutoff = self._pid_state.get("demand_shutoff") or {}
        return {
            "supply_lockout_latched": bool(self._pid_state.get("lockout_latched")),
            "demand_shutoff_active": bool(shutoff.get("active")),
            "control_mode": self._pid_state.get("control_mode"),
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on mining (power on hashboards)."""
        if self._pid_state.get("lockout_latched"):
            raise HomeAssistantError(
                "Supply temperature lockout is latched. Check the heating loop, "
                "then press Reset Supply Lockout before turning mining back on."
            )
        controller = self._data.get("controller")
        if controller is not None:
            # Takes the controller's step lock and releases any shutoff ownership
            # before the command goes out, so a tick can't race the user.
            await controller.async_user_mining_override(True)
        try:
            _LOGGER.info("Powering on hashboards on %s", self.coordinator.miner_ip)
            result = await self.coordinator.api.power_on()
            _LOGGER.info("Power on command sent to %s: %s", self.coordinator.miner_ip, result)
            self._assumed_state = True
            self._assumed_state_time = datetime.now()
            self.async_write_ha_state()
        except Exception as err:
            _LOGGER.error("Failed to power on %s: %s", self.coordinator.miner_ip, err)
            self._assumed_state = None
            self._assumed_state_time = None
            raise

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off mining (power off hashboards). Never auto-resumed."""
        controller = self._data.get("controller")
        if controller is not None:
            await controller.async_user_mining_override(False)
        try:
            _LOGGER.info("Powering off hashboards on %s", self.coordinator.miner_ip)
            result = await self.coordinator.api.power_off()
            _LOGGER.info("Power off command sent to %s: %s", self.coordinator.miner_ip, result)
            self._assumed_state = False
            self._assumed_state_time = datetime.now()
            self.async_write_ha_state()
        except Exception as err:
            _LOGGER.error("Failed to power off %s: %s", self.coordinator.miner_ip, err)
            self._assumed_state = None
            self._assumed_state_time = None
            raise
