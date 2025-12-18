"""The Whatsminer integration."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PASSWORD, CONF_PORT, CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant

from .const import (
    CONF_POWER_MAX,
    CONF_POWER_MIN,
    DEFAULT_PASSWORD,
    DEFAULT_PORT,
    DEFAULT_POWER_MAX,
    DEFAULT_POWER_MIN,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    PLATFORMS,
)
from .coordinator import WhatsminerCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Whatsminer from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    # Get config values with defaults
    miner_ip = entry.data[CONF_HOST]
    password = entry.data.get(CONF_PASSWORD, DEFAULT_PASSWORD)
    port = entry.data.get(CONF_PORT, DEFAULT_PORT)
    scan_interval = entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
    name = entry.data.get(CONF_NAME) or entry.title

    # Apply options overrides if they exist
    if entry.options:
        password = entry.options.get(CONF_PASSWORD, password)
        scan_interval = entry.options.get(CONF_SCAN_INTERVAL, scan_interval)

    _LOGGER.info(f"Setting up Whatsminer at {miner_ip}:{port}")

    # Create coordinator
    coordinator = WhatsminerCoordinator(
        hass=hass,
        ip=miner_ip,
        password=password,
        port=port,
        scan_interval=scan_interval,
        name=name,
    )

    # Perform initial data fetch
    await coordinator.async_config_entry_first_refresh()

    # Store coordinator and config for platforms to use
    hass.data[DOMAIN][entry.entry_id] = {
        "coordinator": coordinator,
        "config": {
            CONF_POWER_MIN: entry.options.get(
                CONF_POWER_MIN,
                entry.data.get(CONF_POWER_MIN, DEFAULT_POWER_MIN)
            ),
            CONF_POWER_MAX: entry.options.get(
                CONF_POWER_MAX,
                entry.data.get(CONF_POWER_MAX, DEFAULT_POWER_MAX)
            ),
        },
    }

    # Set up platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Register update listener for options
    entry.async_on_unload(entry.add_update_listener(update_listener))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok


async def update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update."""
    await hass.config_entries.async_reload(entry.entry_id)
