"""The Whatsminer integration."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PASSWORD, CONF_PORT, CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant

from .const import (
    CONF_CHIP_TEMP_SAFETY_CAP,
    CONF_DEFAULT_POWER_LIMIT,
    CONF_EXTERNAL_TEMP_SENSOR,
    CONF_PID_COARSE_STEP_BAND,
    CONF_PID_DEMAND_ENTITIES,
    CONF_PID_DEMAND_MODE,
    CONF_PID_DEMAND_FLOOR_FRAC,
    CONF_PID_DEMAND_CEILING_FRAC,
    CONF_PID_DEMAND_WEIGHT_BY_ERROR,
    CONF_PID_FINE_STEP_BAND,
    CONF_PID_INTEGRAL_BAND,
    CONF_PID_KD,
    CONF_PID_KE,
    CONF_PID_KI,
    CONF_PID_KP,
    CONF_PID_MIN_ADJUST_INTERVAL,
    CONF_PID_MIN_POWER_STEP,
    CONF_PID_OUTDOOR_TEMP_SENSOR,
    CONF_PID_SETPOINT_RAMP_RATE,
    CONF_PID_SUPPLY_TEMP_LOCKOUT,
    CONF_PID_SUPPLY_TEMP_SAFETY_CAP,
    CONF_PID_TARGET_TEMP,
    CONF_POWER_MAX,
    CONF_POWER_MIN,
    DEFAULT_CHIP_TEMP_SAFETY_CAP,
    DEFAULT_DEFAULT_POWER_LIMIT,
    DEFAULT_PASSWORD,
    DEFAULT_PID_KD,
    DEFAULT_PID_KE,
    DEFAULT_PID_KI,
    DEFAULT_PID_KP,
    DEFAULT_PID_DEMAND_MODE,
    DEFAULT_PID_DEMAND_FLOOR_FRAC,
    DEFAULT_PID_DEMAND_CEILING_FRAC,
    DEFAULT_PID_DEMAND_WEIGHT_BY_ERROR,
    DEFAULT_PID_MIN_ADJUST_INTERVAL,
    DEFAULT_PID_MIN_POWER_STEP,
    DEFAULT_PID_OUTDOOR_TEMP_SENSOR,
    DEFAULT_PID_TARGET_TEMP,
    DEFAULT_PORT,
    DEFAULT_POWER_MAX,
    DEFAULT_POWER_MIN,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    PLATFORMS,
)
from .coordinator import WhatsminerCoordinator
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

    # Perform initial data fetch
    await coordinator.async_config_entry_first_refresh()

    def _opt(key: str, default):
        return entry.options.get(key, entry.data.get(key, default))

    if not _opt(CONF_EXTERNAL_TEMP_SENSOR, None):
        _LOGGER.warning(
            "PID Mode requires an external temperature sensor — open the "
            "integration's Configure dialog and pick one before enabling PID Mode"
        )

    # Store coordinator and config for platforms to use. pid_state is a mutable
    # dict shared between the PID Mode switch (writer) and the diagnostic PID
    # sensors (readers) so both platforms see the same numbers on each tick.
    hass.data[DOMAIN][entry.entry_id] = {
        "coordinator": coordinator,
"config": {
            CONF_POWER_MIN: _opt(CONF_POWER_MIN, DEFAULT_POWER_MIN),
            CONF_POWER_MAX: _opt(CONF_POWER_MAX, DEFAULT_POWER_MAX),
            CONF_PID_KP: _opt(CONF_PID_KP, DEFAULT_PID_KP),
            CONF_PID_KI: _opt(CONF_PID_KI, DEFAULT_PID_KI),
            CONF_PID_KD: _opt(CONF_PID_KD, DEFAULT_PID_KD),
            CONF_PID_KE: _opt(CONF_PID_KE, DEFAULT_PID_KE),
            CONF_PID_TARGET_TEMP: _opt(CONF_PID_TARGET_TEMP, DEFAULT_PID_TARGET_TEMP),
            CONF_EXTERNAL_TEMP_SENSOR: _opt(CONF_EXTERNAL_TEMP_SENSOR, None),
            CONF_PID_OUTDOOR_TEMP_SENSOR: _opt(CONF_PID_OUTDOOR_TEMP_SENSOR, DEFAULT_PID_OUTDOOR_TEMP_SENSOR),
            CONF_DEFAULT_POWER_LIMIT: _opt(
                CONF_DEFAULT_POWER_LIMIT, DEFAULT_DEFAULT_POWER_LIMIT
            ),
            CONF_PID_MIN_POWER_STEP: _opt(
                CONF_PID_MIN_POWER_STEP, DEFAULT_PID_MIN_POWER_STEP
            ),
            CONF_PID_MIN_ADJUST_INTERVAL: _opt(
                CONF_PID_MIN_ADJUST_INTERVAL, DEFAULT_PID_MIN_ADJUST_INTERVAL
            ),
            CONF_CHIP_TEMP_SAFETY_CAP: _opt(
                CONF_CHIP_TEMP_SAFETY_CAP, DEFAULT_CHIP_TEMP_SAFETY_CAP
            ),
            CONF_PID_DEMAND_ENTITIES: _opt(
                CONF_PID_DEMAND_ENTITIES, DEFAULT_PID_DEMAND_ENTITIES
            ),
            CONF_PID_DEMAND_MODE: _opt(
                CONF_PID_DEMAND_MODE, DEFAULT_PID_DEMAND_MODE
            ),
            CONF_PID_DEMAND_FLOOR_FRAC: _opt(
                CONF_PID_DEMAND_FLOOR_FRAC, DEFAULT_PID_DEMAND_FLOOR_FRAC
            ),
            CONF_PID_DEMAND_CEILING_FRAC: _opt(
                CONF_PID_DEMAND_CEILING_FRAC, DEFAULT_PID_DEMAND_CEILING_FRAC
            ),
            CONF_PID_DEMAND_WEIGHT_BY_ERROR: _opt(
                CONF_PID_DEMAND_WEIGHT_BY_ERROR, DEFAULT_PID_DEMAND_WEIGHT_BY_ERROR
            ),
        },
"pid_state": {
            "error": None,
            "proportional": None,
            "integral": None,
            "derivative": None,
            "external": None,
            "output": None,             # actuated (what we commanded)
            "requested_output": None,   # pre-clamp PID desire
            "target": None,
            "enabled": False,
            "safety_engaged": False,
            "demand_index": None,
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
    """Return a copy of ``values`` with temperature/delta/gain keys converted.

    Used by ``async_migrate_entry`` to walk both ``entry.data`` and
    ``entry.options`` in lockstep, so a key stored on initial setup (``data``)
    or later edited in the options flow (``options``) gets the same treatment.
    """
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


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate Whatsminer config entries between schema versions.

    v1 → v2: integration switches from internal Celsius to internal Fahrenheit.
    All temperature absolutes, deltas/rates, and W/°C gains are converted in
    place so existing user tuning survives the unit change with no behavior
    change. The migration is idempotent on re-runs (only fires when the stored
    version is < ConfigFlow.VERSION).

    v2 → v3: adds new optional config keys (envelope, feedforward placeholders).
    No data transformation needed - all new keys are Optional additions.
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
        _LOGGER.info(
            "Whatsminer entry %s migrated v1 → v2 (Celsius → Fahrenheit)",
            entry.entry_id,
        )
    if entry.version == 2:
        hass.config_entries.async_update_entry(entry, version=3)
        _LOGGER.info(
            "Whatsminer entry %s migrated v2 → v3 (multi-step options flow)",
            entry.entry_id,
        )
    return True
