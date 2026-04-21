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
    CONF_PID_KD,
    CONF_PID_KI,
    CONF_PID_KP,
    CONF_PID_TARGET_TEMP,
    CONF_POWER_MAX,
    CONF_POWER_MIN,
    DEFAULT_CHIP_TEMP_SAFETY_CAP,
    DEFAULT_PASSWORD,
    DEFAULT_PID_KD,
    DEFAULT_PID_KI,
    DEFAULT_PID_KP,
    DEFAULT_PID_TARGET_TEMP,
    DEFAULT_PORT,
    DEFAULT_POWER_MAX,
    DEFAULT_POWER_MIN,
    DEFAULT_SCAN_INTERVAL,
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

    # Test connection by getting summary
    if not await api.test_connection():
        raise CannotConnect

    # Return info that you want to store in the config entry
    return {
        "title": data.get(CONF_NAME) or f"Whatsminer {data[CONF_HOST]}",
    }


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Whatsminer."""

    VERSION = 1

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
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"
            else:
                # Check if this miner is already configured
                await self.async_set_unique_id(
                    f"whatsminer_{user_input[CONF_HOST].replace('.', '_')}"
                )
                self._abort_if_unique_id_configured()

                return self.async_create_entry(
                    title=info["title"],
                    data=user_input,
                )

        # Show the form
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
        return OptionsFlowHandler(config_entry)


class OptionsFlowHandler(config_entries.OptionsFlow):
    """Handle options flow for Whatsminer."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialize options flow."""
        self.config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        # Get current values from config entry data and options
        current_data = {**self.config_entry.data, **self.config_entry.options}

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_PASSWORD,
                        default=current_data.get(CONF_PASSWORD, DEFAULT_PASSWORD),
                    ): str,
                    vol.Optional(
                        CONF_SCAN_INTERVAL,
                        default=current_data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
                    ): vol.All(vol.Coerce(int), vol.Range(min=10, max=300)),
                    vol.Optional(
                        CONF_POWER_MIN,
                        default=current_data.get(CONF_POWER_MIN, DEFAULT_POWER_MIN),
                    ): vol.All(vol.Coerce(int), vol.Range(min=100, max=10000)),
                    vol.Optional(
                        CONF_POWER_MAX,
                        default=current_data.get(CONF_POWER_MAX, DEFAULT_POWER_MAX),
                    ): vol.All(vol.Coerce(int), vol.Range(min=100, max=10000)),
                    vol.Optional(
                        CONF_PID_TARGET_TEMP,
                        default=current_data.get(
                            CONF_PID_TARGET_TEMP, DEFAULT_PID_TARGET_TEMP
                        ),
                    ): vol.All(vol.Coerce(float), vol.Range(min=40, max=95)),
                    vol.Optional(
                        CONF_PID_KP,
                        default=current_data.get(CONF_PID_KP, DEFAULT_PID_KP),
                    ): vol.All(vol.Coerce(float), vol.Range(min=0, max=5000)),
                    vol.Optional(
                        CONF_PID_KI,
                        default=current_data.get(CONF_PID_KI, DEFAULT_PID_KI),
                    ): vol.All(vol.Coerce(float), vol.Range(min=0, max=1000)),
                    vol.Optional(
                        CONF_PID_KD,
                        default=current_data.get(CONF_PID_KD, DEFAULT_PID_KD),
                    ): vol.All(vol.Coerce(float), vol.Range(min=0, max=5000)),
                    vol.Optional(
                        CONF_EXTERNAL_TEMP_SENSOR,
                        description={
                            "suggested_value": current_data.get(
                                CONF_EXTERNAL_TEMP_SENSOR
                            )
                        },
                    ): EntitySelector(
                        EntitySelectorConfig(
                            domain="sensor", device_class="temperature"
                        )
                    ),
                    vol.Optional(
                        CONF_CHIP_TEMP_SAFETY_CAP,
                        default=current_data.get(
                            CONF_CHIP_TEMP_SAFETY_CAP, DEFAULT_CHIP_TEMP_SAFETY_CAP
                        ),
                    ): vol.All(vol.Coerce(float), vol.Range(min=60, max=100)),
                }
            ),
        )


class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect."""


class InvalidAuth(HomeAssistantError):
    """Error to indicate there is invalid auth."""
