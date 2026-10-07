"""Minimal stand-ins for the Home Assistant modules controller.py imports.

Only what the controller touches is implemented. Installed into sys.modules by
test_controller_smoke.py before the package is imported.
"""
from __future__ import annotations

import asyncio
import sys
import types
from datetime import datetime, timezone


def _mod(name: str) -> types.ModuleType:
    m = types.ModuleType(name)
    sys.modules[name] = m
    return m


def install() -> None:
    ha = _mod("homeassistant")
    ha.__path__ = []

    const = _mod("homeassistant.const")
    const.STATE_UNAVAILABLE = "unavailable"
    const.STATE_UNKNOWN = "unknown"
    const.STATE_ON = "on"
    for name in ("CONF_HOST", "CONF_NAME", "CONF_PASSWORD", "CONF_PORT", "CONF_SCAN_INTERVAL"):
        setattr(const, name, name.lower()[5:])

    class UnitOfTemperature:
        FAHRENHEIT = "°F"
        CELSIUS = "°C"

    const.UnitOfTemperature = UnitOfTemperature

    class Platform:
        BUTTON = "button"
        SENSOR = "sensor"
        BINARY_SENSOR = "binary_sensor"
        SWITCH = "switch"
        NUMBER = "number"

    const.Platform = Platform

    core = _mod("homeassistant.core")
    core.HomeAssistant = object
    core.callback = lambda f: f

    config_entries = _mod("homeassistant.config_entries")
    config_entries.ConfigEntry = object

    exceptions = _mod("homeassistant.exceptions")

    class HomeAssistantError(Exception):
        pass

    exceptions.HomeAssistantError = HomeAssistantError

    components = _mod("homeassistant.components")
    components.__path__ = []
    pn = _mod("homeassistant.components.persistent_notification")
    pn.created: list[tuple[str, str, str]] = []
    pn.dismissed: list[str] = []

    def async_create(hass, message, title=None, notification_id=None):
        pn.created.append((notification_id, title, message))

    def async_dismiss(hass, notification_id):
        pn.dismissed.append(notification_id)

    pn.async_create = async_create
    pn.async_dismiss = async_dismiss

    helpers = _mod("homeassistant.helpers")
    helpers.__path__ = []
    storage = _mod("homeassistant.helpers.storage")

    class Store:
        saved: dict[str, dict] = {}

        def __init__(self, hass, version, key):
            self.key = key
            self.save_calls = 0
            self.delay_calls = 0

        async def async_load(self):
            return Store.saved.get(self.key)

        async def async_save(self, data):
            self.save_calls += 1
            Store.saved[self.key] = data

        def async_delay_save(self, fn, delay):
            self.delay_calls += 1
            Store.saved[self.key] = fn()

    storage.Store = Store

    event = _mod("homeassistant.helpers.event")
    event.timers: list = []

    def async_track_time_interval(hass, action, interval):
        event.timers.append((action, interval))
        return lambda: event.timers.remove((action, interval))

    event.async_track_time_interval = async_track_time_interval

    uc = _mod("homeassistant.helpers.update_coordinator")

    class DataUpdateCoordinator:
        def __init__(self, *a, **k):
            pass

    class UpdateFailed(Exception):
        pass

    uc.DataUpdateCoordinator = DataUpdateCoordinator
    uc.UpdateFailed = UpdateFailed

    util = _mod("homeassistant.util")
    util.__path__ = []
    conv = _mod("homeassistant.util.unit_conversion")

    class TemperatureConverter:
        @staticmethod
        def convert(value, from_unit, to_unit):
            if from_unit == to_unit:
                return value
            if from_unit == "°C" and to_unit == "°F":
                return value * 9 / 5 + 32
            if from_unit == "°F" and to_unit == "°C":
                return (value - 32) * 5 / 9
            raise ValueError(from_unit)

    conv.TemperatureConverter = TemperatureConverter


class FakeState:
    def __init__(self, state, attributes=None, age_s=0.0, now=None):
        self.state = state
        self.attributes = attributes or {}
        base = datetime.fromtimestamp(now or 0, tz=timezone.utc)
        from datetime import timedelta

        self.last_reported = base - timedelta(seconds=age_s)
        self.last_updated = self.last_reported


class FakeStates:
    def __init__(self):
        self.items: dict[str, FakeState] = {}

    def get(self, entity_id):
        return self.items.get(entity_id)


class FakeServices:
    def __init__(self):
        self.forecast: list[dict] = []
        self.calls = 0
        self.fail = False

    async def async_call(self, domain, service, data, blocking=False, return_response=False):
        self.calls += 1
        if self.fail:
            raise RuntimeError("forecast unavailable")
        return {data["entity_id"]: {"forecast": list(self.forecast)}}


class FakeHass:
    def __init__(self):
        self.states = FakeStates()
        self.services = FakeServices()
        self.config = types.SimpleNamespace(units=types.SimpleNamespace(temperature_unit="°F"))
        self.tasks: list[asyncio.Task] = []

    def async_create_task(self, coro):
        task = asyncio.get_event_loop().create_task(coro)
        self.tasks.append(task)
        return task


class FakeAPI:
    def __init__(self):
        self.calls: list[tuple[str, int | None]] = []
        self.fail: set[str] = set()

    async def power_on(self):
        if "power_on" in self.fail:
            raise RuntimeError("boom")
        self.calls.append(("power_on", None))
        return {}

    async def power_off(self):
        if "power_off" in self.fail:
            raise RuntimeError("boom")
        self.calls.append(("power_off", None))
        return {}

    async def set_power_limit(self, watts):
        if "set_power_limit" in self.fail:
            raise RuntimeError("boom")
        self.calls.append(("set_power_limit", int(watts)))
        return {}


class FakeCoordinator:
    def __init__(self):
        self.data = {
            "mac": "aa_bb", "is_mining": True, "wattage_limit": 4200,
            "temperature_avg": 120.0, "uptime": 86400,
        }
        self.last_update_success = True
        self.name = "heatcore"
        self.miner_ip = "10.0.0.104"
        self.api = FakeAPI()
        from datetime import timedelta as _td

        self.update_interval = _td(seconds=30)
        self.listeners = []
        self.update_calls = 0

    def async_add_listener(self, cb):
        self.listeners.append(cb)
        return lambda: self.listeners.remove(cb)

    def async_update_listeners(self):
        self.update_calls += 1
