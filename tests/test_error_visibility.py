"""Error visibility (1.14): the firmware's own error codes surface in the poll
data, a diagnostic sensor, and one notification per new (code, raised-at) pair.
"""
from __future__ import annotations

import importlib
import pathlib
import sys
import types

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_controller_smoke import Rig, controller_mod, run_async  # noqa: E402  (installs the HA stubs)

coordinator_mod = importlib.import_module("wm.coordinator")


# --- minimal HA sensor stubs (no other test imports wm.sensor) ----------------


def _install_sensor_stubs():
    sensor = types.ModuleType("homeassistant.components.sensor")
    sensor.SensorDeviceClass = types.SimpleNamespace(
        TEMPERATURE="temperature", POWER="power", DURATION="duration",
        TIMESTAMP="timestamp", ENUM="enum",
    )
    sensor.SensorStateClass = types.SimpleNamespace(
        MEASUREMENT="measurement", TOTAL_INCREASING="total_increasing",
    )
    sensor.SensorEntity = object

    class SensorEntityDescription:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    sensor.SensorEntityDescription = SensorEntityDescription
    sys.modules["homeassistant.components.sensor"] = sensor

    haconst = sys.modules["homeassistant.const"]
    haconst.UnitOfPower = types.SimpleNamespace(WATT="W")
    haconst.UnitOfTime = types.SimpleNamespace(SECONDS="s", HOURS="h")
    haconst.REVOLUTIONS_PER_MINUTE = "rpm"

    entity = types.ModuleType("homeassistant.helpers.entity")
    entity.EntityCategory = types.SimpleNamespace(DIAGNOSTIC="diagnostic", CONFIG="config")

    class DeviceInfo(dict):
        def __init__(self, **kw):
            super().__init__(kw)

    entity.DeviceInfo = DeviceInfo
    sys.modules["homeassistant.helpers.entity"] = entity

    platform = types.ModuleType("homeassistant.helpers.entity_platform")
    platform.AddEntitiesCallback = object
    sys.modules["homeassistant.helpers.entity_platform"] = platform

    class CoordinatorEntity:
        def __init__(self, coordinator):
            self.coordinator = coordinator

    sys.modules["homeassistant.helpers.update_coordinator"].CoordinatorEntity = CoordinatorEntity


_install_sensor_stubs()
sensor_mod = importlib.import_module("wm.sensor")


# -------------------------------------------------------------------- parser


def _coord():
    return coordinator_mod.WhatsminerCoordinator(None, "127.0.0.1", "admin", 4028, 30, "m64")


def test_the_live_list_payload_parses():
    # Captured 2026-10-17 on an M64 mid-incident (the motivator for 1.14).
    payload = {
        "STATUS": "S", "When": 1, "Code": 131,
        "Msg": {"error_code": [{"2320": "2026-10-17 17:33:06"}, {"24": "2026-10-17 17:31:00"}]},
        "Description": "",
    }
    assert _coord()._parse_error_codes(payload) == {
        "2320": "2026-10-17 17:33:06", "24": "2026-10-17 17:31:00",
    }


def test_a_bare_dict_error_code_field_parses_too():
    payload = {"STATUS": "S", "Msg": {"error_code": {"2320": "2026-10-17 17:33:06"}}}
    assert _coord()._parse_error_codes(payload) == {"2320": "2026-10-17 17:33:06"}


def test_a_clean_miner_parses_to_no_codes():
    assert _coord()._parse_error_codes({"STATUS": "S", "Msg": {"error_code": []}}) == {}


@pytest.mark.parametrize("bad", [
    None, "nope", 42, [], {},
    {"Msg": None}, {"Msg": "nope"}, {"Msg": []},
    {"Msg": {}}, {"Msg": {"error_code": None}}, {"Msg": {"error_code": [None, 5, "x"]}},
])
def test_malformed_payloads_never_raise_and_parse_to_what_they_can(bad):
    codes = _coord()._parse_error_codes(bad)
    assert codes == {} and isinstance(codes, dict)


def test_malformed_entries_are_skipped_not_fatal():
    payload = {"Msg": {"error_code": [None, "junk", {"2320": "2026-10-17 17:33:06"}]}}
    assert _coord()._parse_error_codes(payload) == {"2320": "2026-10-17 17:33:06"}


# --------------------------------------------------------------- coordinator


def test_poll_merges_error_codes_and_keeps_them_over_a_failed_read():
    from test_api_v3 import _PollAPI

    coord = _coord()
    coord.api = _PollAPI({"STATUS": "S", "Msg": {}})
    coord.api.errors = [{"2320": "2026-10-17 17:33:06"}]
    data = run_async(coord._async_update_data())
    assert data["error_codes"] == {"2320": "2026-10-17 17:33:06"}
    coord.data = data  # DataUpdateCoordinator.async_refresh does this in production

    coord.api.errors = RuntimeError("over max connect")
    data = run_async(coord._async_update_data())
    assert data["error_codes"] == {"2320": "2026-10-17 17:33:06"}  # kept, not blanked
    coord.data = data

    coord.api.errors = None
    data = run_async(coord._async_update_data())
    assert data["error_codes"] == {}


# -------------------------------------------------------------------- sensor


def _error_sensor():
    import ha_stubs

    return sensor_mod.WhatsminerErrorCodeSensor(ha_stubs.FakeCoordinator())


def test_error_code_sensor_reports_the_newest_code_as_its_state():
    sensor = _error_sensor()
    sensor.coordinator.data["error_codes"] = {
        "24": "2026-10-17 17:31:00", "2320": "2026-10-17 17:33:06",
    }
    assert sensor.native_value == "2320"
    attrs = sensor.extra_state_attributes
    assert attrs["count"] == 2
    assert attrs["newest_at"] == "2026-10-17 17:33:06"
    assert attrs["codes"] == {"24": "2026-10-17 17:31:00", "2320": "2026-10-17 17:33:06"}


def test_error_code_sensor_reads_none_when_clean():
    sensor = _error_sensor()
    assert sensor.native_value == "none"
    attrs = sensor.extra_state_attributes
    assert attrs["count"] == 0 and attrs["newest_at"] is None and attrs["codes"] == {}


# ------------------------------------------------------------ notifications


def test_a_new_error_code_notifies_once_and_arms_the_fault_ceiling(monkeypatch):
    from test_controller_smoke import pn

    rig = Rig(monkeypatch)

    async def go():
        await rig.setup()
        rig.coord.data["error_codes"] = {"2320": "2026-10-17 17:33:06"}
        await rig.tick()
        keys = [k for k, _, _ in pn.created]
        assert keys.count("whatsminer_miner_error_2320") == 1
        _, title, message = [n for n in pn.created if n[0] == "whatsminer_miner_error_2320"][0]
        assert "2320" in title and "2026-10-17 17:33:06" in message
        assert "fault ceiling" in message
        # ... and the fault armed the ceiling: no witness yet, so it caps near
        # the power in force when it faulted (4200 W + the 250 W margin).
        assert rig.ctl._fault_reason == "error code 2320"
        assert rig.ctl._fault_power == 4200  # the limit in force when it faulted
        faults = rig.pid_state["faults"]
        assert faults["fault_at"] is not None
        assert faults["fault_power_w"] == 4200
        assert faults["fault_reason"] == "error code 2320"
        assert faults["ceiling_w"] == 4450
        await rig.tick()
        await rig.tick()
        assert [k for k, _, _ in pn.created].count("whatsminer_miner_error_2320") == 1  # deduped
        await rig.tick()
        assert [k for k, _, _ in pn.created].count("whatsminer_miner_error_2320") == 1

    run_async(go())


def test_a_new_occurrence_of_the_same_code_notifies_again(monkeypatch):
    from test_controller_smoke import pn

    rig = Rig(monkeypatch)

    async def go():
        await rig.setup()
        rig.coord.data["error_codes"] = {"2320": "2026-10-17 17:33:06"}
        await rig.tick()
        rig.coord.data["error_codes"] = {"2320": "2026-10-18 01:02:03"}  # new occurrence: new timestamp
        await rig.tick()
        assert [k for k, _, _ in pn.created].count("whatsminer_miner_error_2320") == 2

    run_async(go())


def test_a_code_that_clears_and_returns_notifies_again(monkeypatch):
    from test_controller_smoke import pn

    rig = Rig(monkeypatch)

    async def go():
        await rig.setup()
        rig.coord.data["error_codes"] = {"2320": "2026-10-17 17:33:06"}
        await rig.tick()
        rig.coord.data["error_codes"] = {}
        await rig.tick()
        rig.coord.data["error_codes"] = {"2320": "2026-10-17 17:33:06"}
        await rig.tick()
        assert [k for k, _, _ in pn.created].count("whatsminer_miner_error_2320") == 2

    run_async(go())
