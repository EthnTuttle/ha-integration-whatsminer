"""The Whatsminer integration."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PASSWORD, CONF_PORT, CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import entity_registry as er

from .const import (
    CONF_CHIP_TEMP_SAFETY_CAP,
    CONF_EXTERNAL_TEMP_SENSOR,
    CONF_MAC,
    CONF_PID_COARSE_STEP_BAND,
    CONF_PID_FINE_STEP_BAND,
    CONF_PID_INTEGRAL_BAND,
    CONF_PID_KD,
    CONF_PID_KI,
    CONF_PID_KP,
    CONF_PID_SETPOINT_RAMP_RATE,
    CONF_PID_SUPPLY_TEMP_LOCKOUT,
    CONF_PID_SUPPLY_TEMP_SAFETY_CAP,
    CONF_PID_TARGET_TEMP,
    DEFAULT_PASSWORD,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    PLATFORMS,
    REMOVED_OPTION_KEYS_V4,
)
from .controller import WhatsminerController
from .coordinator import DEFAULT_DATA, WhatsminerCoordinator
from .unit_helpers import c_to_f

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

    # Perform initial data fetch. If the miner doesn't answer and we have seen
    # it before, set up anyway in a degraded state: the controller may own a
    # stop that only it can release, and a miner that is powered off may not
    # answer the API at all. Entities stay unavailable until a poll succeeds.
    cached_mac = entry.data.get(CONF_MAC)
    try:
        await coordinator.async_config_entry_first_refresh()
    except ConfigEntryNotReady:
        if not cached_mac:
            raise
        _LOGGER.warning(
            "Miner at %s is unreachable — setting up in degraded mode with the cached "
            "MAC so the controller can resume a stopped miner",
            miner_ip,
        )
        coordinator.seed_offline(cached_mac, miner_ip, {**DEFAULT_DATA})
    else:
        mac = coordinator.data.get("mac")
        if mac and mac != cached_mac:
            hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_MAC: mac})

    # Options override initial setup data; consumers apply their own defaults.
    config = {**entry.data, **entry.options}

    if not config.get(CONF_EXTERNAL_TEMP_SENSOR):
        _LOGGER.warning(
            "No supply temperature sensor is configured — the controller runs on "
            "the outdoor-reset fallback curve until one is set in Configure"
        )

    # pid_state is a mutable dict shared between the controller (writer) and
    # the diagnostic sensors (readers) so both see the same numbers each tick.
    pid_state: dict = {
        "error": None,
        "proportional": None,
        "integral": None,
        "derivative": None,
        "external": None,
        "output": None,             # actuated (what we commanded)
        "requested_output": None,   # pre-clamp PID desire
        "target": None,
        "safety_engaged": False,
        "lockout_latched": False,   # supply lockout; restored by the controller
        "demand_index": None,
        "control_mode": "idle",
        "demand_shutoff": {},
        "freeze_guard": {},
        "power_floor": {},          # learned floor; published by the controller
        "outdoor_mean": None,
    }
    controller = WhatsminerController(hass, entry, coordinator, pid_state, config)
    await controller.async_setup()

    hass.data[DOMAIN][entry.entry_id] = {
        "coordinator": coordinator,
        "config": config,
        "pid_state": pid_state,
        "controller": controller,
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
        data = hass.data[DOMAIN].pop(entry.entry_id)
        controller: WhatsminerController | None = data.get("controller")
        if controller is not None:
            await controller.async_unload()

    return unload_ok


async def update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update."""
    await hass.config_entries.async_reload(entry.entry_id)


# Conf keys whose values are absolute temperatures — migrate via F = 9/5·C + 32.
_TEMPERATURE_KEYS_C_TO_F: tuple[str, ...] = (
    CONF_PID_TARGET_TEMP,
    CONF_CHIP_TEMP_SAFETY_CAP,
    CONF_PID_SUPPLY_TEMP_SAFETY_CAP,
    CONF_PID_SUPPLY_TEMP_LOCKOUT,
)

# Conf keys whose values are temperature deltas or rates — migrate via ×1.8.
_DELTA_KEYS_C_TO_F: tuple[str, ...] = (
    CONF_PID_COARSE_STEP_BAND,
    CONF_PID_FINE_STEP_BAND,
    CONF_PID_INTEGRAL_BAND,
    CONF_PID_SETPOINT_RAMP_RATE,
)

# Gain keys (W/°C → W/°F): divide by 1.8 so feeding the PID a 1.8× larger
# error in °F produces the *same* watt output for the same physical conditions.
_GAIN_KEYS_C_TO_F: tuple[str, ...] = (
    CONF_PID_KP,
    CONF_PID_KI,
    CONF_PID_KD,
)


def _migrate_dict_celsius_to_fahrenheit(values: dict) -> dict:
    """Return a copy of ``values`` with temperature/delta/gain keys converted."""
    out = dict(values)
    for key in _TEMPERATURE_KEYS_C_TO_F:
        if key in out and out[key] is not None:
            out[key] = round(c_to_f(float(out[key])), 2)
    for key in _DELTA_KEYS_C_TO_F:
        if key in out and out[key] is not None:
            out[key] = round(float(out[key]) * 1.8, 3)
    for key in _GAIN_KEYS_C_TO_F:
        if key in out and out[key] is not None:
            out[key] = round(float(out[key]) / 1.8, 3)
    return out


def _strip_keys(values: dict, keys: tuple[str, ...]) -> dict:
    return {k: v for k, v in values.items() if k not in keys}


# Entities removed in v4: (domain, unique_id suffix)
_REMOVED_ENTITIES_V4: tuple[tuple[str, str], ...] = (
    ("switch", "_pid_mode"),
    ("number", "_power_limit"),
)


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate Whatsminer config entries between schema versions.

    v1 → v2: internal Celsius to internal Fahrenheit. All temperature
    absolutes, deltas/rates and W/°C gains are converted in place.

    v2 → v3: new optional keys only (envelope, feedforward); no data change.

    v3 → v4: PID-only refactor. The PID Mode switch, the Power Limit number,
    the default power limit and the demand envelope mode are gone. Their
    option keys are dropped from data and options and the orphaned entity
    registry entries are removed so they don't linger as "restored".
    """
    _LOGGER.info(
        "Considering migration for Whatsminer entry %s (version %s)",
        entry.entry_id,
        entry.version,
    )
    if entry.version == 1:
        new_data = _migrate_dict_celsius_to_fahrenheit(entry.data)
        new_options = _migrate_dict_celsius_to_fahrenheit(entry.options)
        hass.config_entries.async_update_entry(
            entry, data=new_data, options=new_options, version=2
        )
        _LOGGER.info("Whatsminer entry %s migrated v1 → v2 (Celsius → Fahrenheit)", entry.entry_id)
    if entry.version == 2:
        hass.config_entries.async_update_entry(entry, version=3)
        _LOGGER.info("Whatsminer entry %s migrated v2 → v3 (multi-step options flow)", entry.entry_id)
    if entry.version == 3:
        new_data = _strip_keys(entry.data, REMOVED_OPTION_KEYS_V4)
        new_options = _strip_keys(entry.options, REMOVED_OPTION_KEYS_V4)
        hass.config_entries.async_update_entry(
            entry, data=new_data, options=new_options, version=4
        )
        registry = er.async_get(hass)
        for reg_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
            for domain, suffix in _REMOVED_ENTITIES_V4:
                if reg_entry.domain == domain and str(reg_entry.unique_id).endswith(suffix):
                    _LOGGER.info("Removing retired entity %s", reg_entry.entity_id)
                    registry.async_remove(reg_entry.entity_id)
        _LOGGER.info("Whatsminer entry %s migrated v3 → v4 (PID-only controller)", entry.entry_id)
    return True
