"""Config flow for Whatsminer integration."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PASSWORD, CONF_PORT, CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
)

from .const import (
    CONF_CHIP_TEMP_SAFETY_CAP,
    CONF_EXTERNAL_TEMP_SENSOR,
    CONF_FREEZE_GUARD_FORECAST_HOURS,
    CONF_FREEZE_GUARD_SENSOR,
    CONF_FREEZE_GUARD_THRESHOLD,
    CONF_PID_INTEGRAL_BAND,
    CONF_PID_KD,
    CONF_PID_KE,
    CONF_PID_KI,
    CONF_PID_KP,
    CONF_PID_COARSE_STEP_BAND,
    CONF_PID_DEMAND_ENTITIES,
    CONF_PID_DEMAND_SHUTOFF_COLD_ROOM_DELTA,
    CONF_PID_DEMAND_SHUTOFF_HYSTERESIS,
    CONF_PID_DEMAND_SHUTOFF_IDLE_DWELL_MIN,
    CONF_PID_DEMAND_SHUTOFF_MIN_OFF_MIN,
    CONF_PID_DEMAND_SHUTOFF_MIN_ON_MIN,
    CONF_PID_DEMAND_SHUTOFF_MODE,
    CONF_PID_DEMAND_SHUTOFF_OUTDOOR_MIN,
    CONF_PID_DEMAND_SHUTOFF_SUPPLY_DWELL_MIN,
    CONF_PID_DEMAND_SHUTOFF_SUPPLY_STOP,
    CONF_PID_DEMAND_SHUTOFF_UNKNOWN_GRACE_MIN,
    CONF_PID_FALLBACK_OUTDOOR_COLD,
    CONF_PID_FALLBACK_OUTDOOR_WARM,
    CONF_PID_FORECAST_BLEND,
    CONF_PID_FORECAST_LOOKAHEAD_MIN,
    CONF_PID_FINE_STEP_BAND,
    CONF_PID_MIN_ADJUST_INTERVAL,
    CONF_PID_MIN_ADJUST_INTERVAL_INCREASE,
    CONF_PID_MIN_POWER_STEP,
    CONF_PID_MIN_POWER_STEP_FINE,
    CONF_PID_MIN_POWER_STEP_MEDIUM,
    CONF_PID_OUTDOOR_TEMP_SENSOR,
    CONF_PID_PRICE_HIGH,
    CONF_PID_PRICE_LOW,
    CONF_PID_PRICE_SENSOR,
    CONF_PID_SUPPLY_TEMP_LOCKOUT,
    CONF_PID_SUPPLY_TEMP_SAFETY_CAP,
    CONF_PID_SETPOINT_RAMP_RATE,
    CONF_PID_SLOPE_EWMA_TAU_S,
    CONF_PID_SURPLUS_DEFICIT,
    CONF_PID_SURPLUS_FULL,
    CONF_PID_SURPLUS_SENSOR,
    CONF_PID_TARGET_TEMP,
    CONF_PID_WEATHER_ENTITY,
    CONF_POWER_MAX,
    CONF_POWER_MIN,
    DEFAULT_CHIP_TEMP_SAFETY_CAP,
    DEFAULT_FREEZE_GUARD_FORECAST_HOURS,
    DEFAULT_FREEZE_GUARD_THRESHOLD,
    DEFAULT_PASSWORD,
    DEFAULT_PID_INTEGRAL_BAND,
    DEFAULT_PID_KD,
    DEFAULT_PID_KE,
    DEFAULT_PID_KI,
    DEFAULT_PID_KP,
    DEFAULT_PID_COARSE_STEP_BAND,
    DEFAULT_PID_DEMAND_ENTITIES,
    DEFAULT_PID_DEMAND_SHUTOFF_COLD_ROOM_DELTA,
    DEFAULT_PID_DEMAND_SHUTOFF_HYSTERESIS,
    DEFAULT_PID_DEMAND_SHUTOFF_IDLE_DWELL_MIN,
    DEFAULT_PID_DEMAND_SHUTOFF_MIN_OFF_MIN,
    DEFAULT_PID_DEMAND_SHUTOFF_MIN_ON_MIN,
    DEFAULT_PID_DEMAND_SHUTOFF_MODE,
    DEFAULT_PID_DEMAND_SHUTOFF_OUTDOOR_MIN,
    DEFAULT_PID_DEMAND_SHUTOFF_SUPPLY_DWELL_MIN,
    DEFAULT_PID_DEMAND_SHUTOFF_SUPPLY_STOP,
    DEFAULT_PID_DEMAND_SHUTOFF_UNKNOWN_GRACE_MIN,
    DEFAULT_PID_FALLBACK_OUTDOOR_COLD,
    DEFAULT_PID_FALLBACK_OUTDOOR_WARM,
    DEFAULT_PID_FINE_STEP_BAND,
    DEFAULT_PID_FORECAST_BLEND,
    DEFAULT_PID_FORECAST_LOOKAHEAD_MIN,
    DEFAULT_PID_MIN_ADJUST_INTERVAL,
    DEFAULT_PID_MIN_ADJUST_INTERVAL_INCREASE,
    DEFAULT_PID_MIN_POWER_STEP,
    DEFAULT_PID_MIN_POWER_STEP_FINE,
    DEFAULT_PID_MIN_POWER_STEP_MEDIUM,
    DEFAULT_PID_SUPPLY_TEMP_LOCKOUT,
    DEFAULT_PID_SUPPLY_TEMP_SAFETY_CAP,
    DEFAULT_PID_SETPOINT_RAMP_RATE,
    DEFAULT_PID_SLOPE_EWMA_TAU_S,
    DEFAULT_PID_TARGET_TEMP,
    DEFAULT_PORT,
    DEFAULT_POWER_MAX,
    DEFAULT_POWER_MIN,
    DEFAULT_SCAN_INTERVAL,
    DEMAND_SHUTOFF_MODES,
    DOMAIN,
)
from .coordinator import WhatsminerAPI

_LOGGER = logging.getLogger(__name__)


async def validate_input(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any]:
    """Validate the user input allows us to connect.

    Data has the keys from STEP_USER_DATA_SCHEMA with values provided by the user.
    """
    api = WhatsminerAPI(
        host=data[CONF_HOST],
        port=data.get(CONF_PORT, DEFAULT_PORT),
        password=data.get(CONF_PASSWORD, DEFAULT_PASSWORD),
    )

    if not await api.test_connection():
        raise CannotConnect

    return {
        "title": data.get(CONF_NAME) or f"Whatsminer {data[CONF_HOST]}",
    }


def _get_current_values(config_entry: config_entries.ConfigEntry) -> dict[str, Any]:
    """Merge data and options, preferring options for any overlap."""
    return {**config_entry.data, **config_entry.options}


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Whatsminer."""

    VERSION = 4

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                info = await validate_input(self.hass, user_input)
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except Exception:
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(
                    f"whatsminer_{user_input[CONF_HOST].replace('.', '_')}"
                )
                self._abort_if_unique_id_configured()

                return self.async_create_entry(
                    title=info["title"],
                    data=user_input,
                )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HOST): str,
                    vol.Optional(CONF_NAME): str,
                    vol.Optional(CONF_PASSWORD, default=DEFAULT_PASSWORD): str,
                    vol.Optional(CONF_PORT, default=DEFAULT_PORT): cv.port,
                    vol.Optional(
                        CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL
                    ): vol.All(vol.Coerce(int), vol.Range(min=10, max=300)),
                    vol.Optional(CONF_POWER_MIN, default=DEFAULT_POWER_MIN): vol.All(
                        vol.Coerce(int), vol.Range(min=100, max=10000)
                    ),
                    vol.Optional(CONF_POWER_MAX, default=DEFAULT_POWER_MAX): vol.All(
                        vol.Coerce(int), vol.Range(min=100, max=10000)
                    ),
                }
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Create the options flow."""
        return OptionsFlowHandler()


class OptionsFlowHandler(config_entries.OptionsFlow):
    """Handle options flow for Whatsminer."""

    def __init__(self) -> None:
        """Initialize options flow.

        HA (2024.11+) provides self.config_entry; assigning it raises on
        2025.12+.
        """
        self._current_data: dict[str, Any] = {}

    def _update_current(
        self, user_input: dict[str, Any], clearable: tuple[str, ...] = ()
    ) -> None:
        """Merge a step's input, dropping cleared optional entity pickers.

        Pickers pre-filled via suggested_value are omitted from user_input
        when the user clears them; without this the old entity would stick.
        """
        for key in clearable:
            if key not in user_input:
                self._current_data.pop(key, None)
        self._current_data.update(user_input)

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 1: Connection, power bounds, target, gains."""
        self._current_data = _get_current_values(self.config_entry)

        if user_input is not None:
            self._current_data.update(user_input)
            return await self.async_step_safety()

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_PASSWORD,
                        default=self._current_data.get(CONF_PASSWORD, DEFAULT_PASSWORD),
                    ): str,
                    vol.Optional(
                        CONF_SCAN_INTERVAL,
                        default=self._current_data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
                    ): vol.All(vol.Coerce(int), vol.Range(min=10, max=300)),
                    vol.Optional(
                        CONF_POWER_MIN,
                        default=self._current_data.get(CONF_POWER_MIN, DEFAULT_POWER_MIN),
                    ): vol.All(vol.Coerce(int), vol.Range(min=100, max=10000)),
                    vol.Optional(
                        CONF_POWER_MAX,
                        default=self._current_data.get(CONF_POWER_MAX, DEFAULT_POWER_MAX),
                    ): vol.All(vol.Coerce(int), vol.Range(min=100, max=10000)),
                    vol.Optional(
                        CONF_PID_TARGET_TEMP,
                        default=self._current_data.get(
                            CONF_PID_TARGET_TEMP, DEFAULT_PID_TARGET_TEMP
                        ),
                    ): vol.All(vol.Coerce(float), vol.Range(min=104, max=203)),
                    vol.Optional(
                        CONF_PID_KP,
                        default=self._current_data.get(CONF_PID_KP, DEFAULT_PID_KP),
                    ): vol.All(vol.Coerce(float), vol.Range(min=0, max=5000)),
                    vol.Optional(
                        CONF_PID_KI,
                        default=self._current_data.get(CONF_PID_KI, DEFAULT_PID_KI),
                    ): vol.All(vol.Coerce(float), vol.Range(min=0, max=1000)),
                    vol.Optional(
                        CONF_PID_KD,
                        default=self._current_data.get(CONF_PID_KD, DEFAULT_PID_KD),
                    ): vol.All(vol.Coerce(float), vol.Range(min=0, max=5000)),
                }
            ),
        )

    async def async_step_safety(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 2: Chip/supply caps, lockout."""
        if user_input is not None:
            self._current_data.update(user_input)
            return await self.async_step_demand()

        return self.async_show_form(
            step_id="safety",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_CHIP_TEMP_SAFETY_CAP,
                        default=self._current_data.get(
                            CONF_CHIP_TEMP_SAFETY_CAP, DEFAULT_CHIP_TEMP_SAFETY_CAP
                        ),
                    ): vol.All(vol.Coerce(float), vol.Range(min=140, max=212)),
                    vol.Optional(
                        CONF_PID_SUPPLY_TEMP_SAFETY_CAP,
                        default=self._current_data.get(
                            CONF_PID_SUPPLY_TEMP_SAFETY_CAP,
                            DEFAULT_PID_SUPPLY_TEMP_SAFETY_CAP,
                        ),
                    ): vol.All(vol.Coerce(float), vol.Range(min=86, max=176)),
                    vol.Optional(
                        CONF_PID_SUPPLY_TEMP_LOCKOUT,
                        default=self._current_data.get(
                            CONF_PID_SUPPLY_TEMP_LOCKOUT,
                            DEFAULT_PID_SUPPLY_TEMP_LOCKOUT,
                        ),
                    ): vol.All(vol.Coerce(float), vol.Range(min=86, max=194)),
                }
            ),
        )

    async def async_step_demand(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 3: Demand entities, demand shutoff, freeze guard."""
        if user_input is not None:
            self._update_current(user_input, (CONF_FREEZE_GUARD_SENSOR,))
            return await self.async_step_feedforward()

        return self.async_show_form(
            step_id="demand",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_PID_DEMAND_ENTITIES,
                        default=self._current_data.get(
                            CONF_PID_DEMAND_ENTITIES, DEFAULT_PID_DEMAND_ENTITIES
                        ),
                    ): EntitySelector(
                        EntitySelectorConfig(domain="climate", multiple=True)
                    ),
                    vol.Optional(
                        CONF_PID_DEMAND_SHUTOFF_MODE,
                        default=self._current_data.get(
                            CONF_PID_DEMAND_SHUTOFF_MODE, DEFAULT_PID_DEMAND_SHUTOFF_MODE
                        ),
                    ): vol.In(DEMAND_SHUTOFF_MODES),
                    vol.Optional(
                        CONF_PID_DEMAND_SHUTOFF_OUTDOOR_MIN,
                        default=self._current_data.get(
                            CONF_PID_DEMAND_SHUTOFF_OUTDOOR_MIN,
                            DEFAULT_PID_DEMAND_SHUTOFF_OUTDOOR_MIN,
                        ),
                    ): vol.All(vol.Coerce(float), vol.Range(min=-40, max=100)),
                    vol.Optional(
                        CONF_PID_DEMAND_SHUTOFF_HYSTERESIS,
                        default=self._current_data.get(
                            CONF_PID_DEMAND_SHUTOFF_HYSTERESIS,
                            DEFAULT_PID_DEMAND_SHUTOFF_HYSTERESIS,
                        ),
                    ): vol.All(vol.Coerce(float), vol.Range(min=0, max=15)),
                    vol.Optional(
                        CONF_PID_DEMAND_SHUTOFF_IDLE_DWELL_MIN,
                        default=self._current_data.get(
                            CONF_PID_DEMAND_SHUTOFF_IDLE_DWELL_MIN,
                            DEFAULT_PID_DEMAND_SHUTOFF_IDLE_DWELL_MIN,
                        ),
                    ): vol.All(vol.Coerce(int), vol.Range(min=5, max=240)),
                    vol.Optional(
                        CONF_PID_DEMAND_SHUTOFF_MIN_OFF_MIN,
                        default=self._current_data.get(
                            CONF_PID_DEMAND_SHUTOFF_MIN_OFF_MIN,
                            DEFAULT_PID_DEMAND_SHUTOFF_MIN_OFF_MIN,
                        ),
                    ): vol.All(vol.Coerce(int), vol.Range(min=0, max=240)),
                    vol.Optional(
                        CONF_PID_DEMAND_SHUTOFF_MIN_ON_MIN,
                        default=self._current_data.get(
                            CONF_PID_DEMAND_SHUTOFF_MIN_ON_MIN,
                            DEFAULT_PID_DEMAND_SHUTOFF_MIN_ON_MIN,
                        ),
                    ): vol.All(vol.Coerce(int), vol.Range(min=0, max=240)),
                    vol.Optional(
                        CONF_PID_DEMAND_SHUTOFF_SUPPLY_STOP,
                        default=self._current_data.get(
                            CONF_PID_DEMAND_SHUTOFF_SUPPLY_STOP,
                            DEFAULT_PID_DEMAND_SHUTOFF_SUPPLY_STOP,
                        ),
                    ): bool,
                    vol.Optional(
                        CONF_PID_DEMAND_SHUTOFF_SUPPLY_DWELL_MIN,
                        default=self._current_data.get(
                            CONF_PID_DEMAND_SHUTOFF_SUPPLY_DWELL_MIN,
                            DEFAULT_PID_DEMAND_SHUTOFF_SUPPLY_DWELL_MIN,
                        ),
                    ): vol.All(vol.Coerce(int), vol.Range(min=1, max=60)),
                    vol.Optional(
                        CONF_PID_DEMAND_SHUTOFF_UNKNOWN_GRACE_MIN,
                        default=self._current_data.get(
                            CONF_PID_DEMAND_SHUTOFF_UNKNOWN_GRACE_MIN,
                            DEFAULT_PID_DEMAND_SHUTOFF_UNKNOWN_GRACE_MIN,
                        ),
                    ): vol.All(vol.Coerce(int), vol.Range(min=0, max=120)),
                    vol.Optional(
                        CONF_PID_DEMAND_SHUTOFF_COLD_ROOM_DELTA,
                        default=self._current_data.get(
                            CONF_PID_DEMAND_SHUTOFF_COLD_ROOM_DELTA,
                            DEFAULT_PID_DEMAND_SHUTOFF_COLD_ROOM_DELTA,
                        ),
                    ): vol.All(vol.Coerce(float), vol.Range(min=0, max=5)),
                    vol.Optional(
                        CONF_FREEZE_GUARD_SENSOR,
                        description={
                            "suggested_value": self._current_data.get(
                                CONF_FREEZE_GUARD_SENSOR
                            )
                        },
                    ): EntitySelector(
                        EntitySelectorConfig(
                            domain="sensor", device_class="temperature"
                        )
                    ),
                    vol.Optional(
                        CONF_FREEZE_GUARD_THRESHOLD,
                        default=self._current_data.get(
                            CONF_FREEZE_GUARD_THRESHOLD, DEFAULT_FREEZE_GUARD_THRESHOLD
                        ),
                    ): vol.All(vol.Coerce(float), vol.Range(min=-40, max=100)),
                    vol.Optional(
                        CONF_FREEZE_GUARD_FORECAST_HOURS,
                        default=self._current_data.get(
                            CONF_FREEZE_GUARD_FORECAST_HOURS,
                            DEFAULT_FREEZE_GUARD_FORECAST_HOURS,
                        ),
                    ): vol.All(vol.Coerce(int), vol.Range(min=1, max=48)),
                }
            ),
        )

    async def async_step_feedforward(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 4: Outdoor sensor, Ke, weather + forecast."""
        if user_input is not None:
            self._update_current(
                user_input, (CONF_PID_OUTDOOR_TEMP_SENSOR, CONF_PID_WEATHER_ENTITY)
            )
            return await self.async_step_envelopes()

        return self.async_show_form(
            step_id="feedforward",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_EXTERNAL_TEMP_SENSOR,
                        description={
                            "suggested_value": self._current_data.get(
                                CONF_EXTERNAL_TEMP_SENSOR
                            )
                        },
                    ): EntitySelector(
                        EntitySelectorConfig(
                            domain="sensor", device_class="temperature"
                        )
                    ),
                    vol.Optional(
                        CONF_PID_OUTDOOR_TEMP_SENSOR,
                        description={
                            "suggested_value": self._current_data.get(
                                CONF_PID_OUTDOOR_TEMP_SENSOR
                            )
                        },
                    ): EntitySelector(
                        EntitySelectorConfig(
                            domain="sensor", device_class="temperature"
                        )
                    ),
                    vol.Optional(
                        CONF_PID_KE,
                        default=self._current_data.get(CONF_PID_KE, DEFAULT_PID_KE),
                    ): vol.All(vol.Coerce(float), vol.Range(min=0, max=500)),
                    vol.Optional(
                        CONF_PID_WEATHER_ENTITY,
                        description={
                            "suggested_value": self._current_data.get(
                                CONF_PID_WEATHER_ENTITY
                            )
                        },
                    ): EntitySelector(
                        EntitySelectorConfig(domain="weather")
                    ),
                    vol.Optional(
                        CONF_PID_FORECAST_LOOKAHEAD_MIN,
                        default=self._current_data.get(
                            CONF_PID_FORECAST_LOOKAHEAD_MIN,
                            DEFAULT_PID_FORECAST_LOOKAHEAD_MIN,
                        ),
                    ): vol.All(vol.Coerce(int), vol.Range(min=0, max=360)),
                    vol.Optional(
                        CONF_PID_FORECAST_BLEND,
                        default=self._current_data.get(
                            CONF_PID_FORECAST_BLEND,
                            DEFAULT_PID_FORECAST_BLEND,
                        ),
                    ): vol.All(vol.Coerce(float), vol.Range(min=0, max=1)),
                    vol.Optional(
                        CONF_PID_FALLBACK_OUTDOOR_COLD,
                        default=self._current_data.get(
                            CONF_PID_FALLBACK_OUTDOOR_COLD,
                            DEFAULT_PID_FALLBACK_OUTDOOR_COLD,
                        ),
                    ): vol.All(vol.Coerce(float), vol.Range(min=-40, max=100)),
                    vol.Optional(
                        CONF_PID_FALLBACK_OUTDOOR_WARM,
                        default=self._current_data.get(
                            CONF_PID_FALLBACK_OUTDOOR_WARM,
                            DEFAULT_PID_FALLBACK_OUTDOOR_WARM,
                        ),
                    ): vol.All(vol.Coerce(float), vol.Range(min=-40, max=100)),
                }
            ),
        )

    async def async_step_envelopes(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 5: Price, surplus (Time-of-Use / Solar / Battery Envelopes)."""
        if user_input is not None:
            self._update_current(
                user_input, (CONF_PID_PRICE_SENSOR, CONF_PID_SURPLUS_SENSOR)
            )
            return await self.async_step_tuning()

        return self.async_show_form(
            step_id="envelopes",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_PID_PRICE_SENSOR,
                        description={
                            "suggested_value": self._current_data.get(
                                CONF_PID_PRICE_SENSOR
                            )
                        },
                    ): EntitySelector(
                        EntitySelectorConfig(domain="sensor")
                    ),
                    vol.Optional(
                        CONF_PID_PRICE_HIGH,
                        default=self._current_data.get(CONF_PID_PRICE_HIGH, 0.0),
                    ): vol.All(vol.Coerce(float), vol.Range(min=0, max=1000)),
                    vol.Optional(
                        CONF_PID_PRICE_LOW,
                        default=self._current_data.get(CONF_PID_PRICE_LOW, 0.0),
                    ): vol.All(vol.Coerce(float), vol.Range(min=0, max=1000)),
                    vol.Optional(
                        CONF_PID_SURPLUS_SENSOR,
                        description={
                            "suggested_value": self._current_data.get(
                                CONF_PID_SURPLUS_SENSOR
                            )
                        },
                    ): EntitySelector(
                        EntitySelectorConfig(domain="sensor")
                    ),
                    vol.Optional(
                        CONF_PID_SURPLUS_DEFICIT,
                        default=self._current_data.get(CONF_PID_SURPLUS_DEFICIT, 0.0),
                    ): vol.All(vol.Coerce(float), vol.Range(min=-10000, max=10000)),
                    vol.Optional(
                        CONF_PID_SURPLUS_FULL,
                        default=self._current_data.get(CONF_PID_SURPLUS_FULL, 0.0),
                    ): vol.All(vol.Coerce(float), vol.Range(min=-10000, max=10000)),
                }
            ),
        )

    async def async_step_tuning(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 6: Step bands, intervals, integral band, ramp rate, slope τ."""
        if user_input is not None:
            self._current_data.update(user_input)
            return self.async_create_entry(title="", data=self._current_data)

        return self.async_show_form(
            step_id="tuning",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_PID_MIN_POWER_STEP,
                        default=self._current_data.get(
                            CONF_PID_MIN_POWER_STEP, DEFAULT_PID_MIN_POWER_STEP
                        ),
                    ): vol.All(vol.Coerce(int), vol.Range(min=1, max=2500)),
                    vol.Optional(
                        CONF_PID_MIN_POWER_STEP_MEDIUM,
                        default=self._current_data.get(
                            CONF_PID_MIN_POWER_STEP_MEDIUM,
                            DEFAULT_PID_MIN_POWER_STEP_MEDIUM,
                        ),
                    ): vol.All(vol.Coerce(int), vol.Range(min=1, max=2500)),
                    vol.Optional(
                        CONF_PID_MIN_POWER_STEP_FINE,
                        default=self._current_data.get(
                            CONF_PID_MIN_POWER_STEP_FINE,
                            DEFAULT_PID_MIN_POWER_STEP_FINE,
                        ),
                    ): vol.All(vol.Coerce(int), vol.Range(min=1, max=2500)),
                    vol.Optional(
                        CONF_PID_COARSE_STEP_BAND,
                        default=self._current_data.get(
                            CONF_PID_COARSE_STEP_BAND, DEFAULT_PID_COARSE_STEP_BAND
                        ),
                    ): vol.All(vol.Coerce(float), vol.Range(min=0, max=90)),
                    vol.Optional(
                        CONF_PID_FINE_STEP_BAND,
                        default=self._current_data.get(
                            CONF_PID_FINE_STEP_BAND, DEFAULT_PID_FINE_STEP_BAND
                        ),
                    ): vol.All(vol.Coerce(float), vol.Range(min=0, max=90)),
                    vol.Optional(
                        CONF_PID_MIN_ADJUST_INTERVAL,
                        default=self._current_data.get(
                            CONF_PID_MIN_ADJUST_INTERVAL, DEFAULT_PID_MIN_ADJUST_INTERVAL
                        ),
                    ): vol.All(vol.Coerce(int), vol.Range(min=0, max=3600)),
                    vol.Optional(
                        CONF_PID_MIN_ADJUST_INTERVAL_INCREASE,
                        default=self._current_data.get(
                            CONF_PID_MIN_ADJUST_INTERVAL_INCREASE,
                            DEFAULT_PID_MIN_ADJUST_INTERVAL_INCREASE,
                        ),
                    ): vol.All(vol.Coerce(int), vol.Range(min=0, max=3600)),
                    vol.Optional(
                        CONF_PID_INTEGRAL_BAND,
                        default=self._current_data.get(
                            CONF_PID_INTEGRAL_BAND, DEFAULT_PID_INTEGRAL_BAND
                        ),
                    ): vol.All(vol.Coerce(float), vol.Range(min=0, max=90)),
                    vol.Optional(
                        CONF_PID_SETPOINT_RAMP_RATE,
                        default=self._current_data.get(
                            CONF_PID_SETPOINT_RAMP_RATE, DEFAULT_PID_SETPOINT_RAMP_RATE
                        ),
                    ): vol.All(vol.Coerce(float), vol.Range(min=0, max=108)),
                    vol.Optional(
                        CONF_PID_SLOPE_EWMA_TAU_S,
                        default=self._current_data.get(
                            CONF_PID_SLOPE_EWMA_TAU_S, DEFAULT_PID_SLOPE_EWMA_TAU_S
                        ),
                    ): vol.All(vol.Coerce(float), vol.Range(min=0, max=3600)),
                }
            ),
        )


class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect."""


class InvalidAuth(HomeAssistantError):
    """Error to indicate there is invalid auth."""