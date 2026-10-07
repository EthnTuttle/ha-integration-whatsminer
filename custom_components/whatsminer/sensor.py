"""Support for Whatsminer sensors."""
from __future__ import annotations

import logging

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    UnitOfPower,
    UnitOfTemperature,
    UnitOfTime,
    REVOLUTIONS_PER_MINUTE,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONTROL_MODES,
    DOMAIN,
    JOULES_PER_TERA_HASH,
    SHUTOFF_STATES,
    TERA_HASH_PER_SECOND,
)
from .coordinator import WhatsminerCoordinator

_LOGGER = logging.getLogger(__name__)

# Sensor descriptions
SENSOR_TYPES: dict[str, SensorEntityDescription] = {
    "hashrate": SensorEntityDescription(
        key="hashrate",
        name="Hashrate",
        native_unit_of_measurement=TERA_HASH_PER_SECOND,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:speedometer",
    ),
    "expected_hashrate": SensorEntityDescription(
        key="expected_hashrate",
        name="Expected Hashrate",
        native_unit_of_measurement=TERA_HASH_PER_SECOND,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:speedometer",
    ),
    "temperature_avg": SensorEntityDescription(
        key="temperature_avg",
        name="Temperature",
        native_unit_of_measurement=UnitOfTemperature.FAHRENHEIT,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    "wattage": SensorEntityDescription(
        key="wattage",
        name="Power Consumption",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    "wattage_limit": SensorEntityDescription(
        key="wattage_limit",
        name="Power Limit",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    "efficiency": SensorEntityDescription(
        key="efficiency",
        name="Efficiency",
        native_unit_of_measurement=JOULES_PER_TERA_HASH,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:gauge",
    ),
    "uptime": SensorEntityDescription(
        key="uptime",
        name="Uptime",
        native_unit_of_measurement=UnitOfTime.SECONDS,
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:clock-outline",
    ),
    "accepted": SensorEntityDescription(
        key="accepted",
        name="Accepted Shares",
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:check-circle",
    ),
    "rejected": SensorEntityDescription(
        key="rejected",
        name="Rejected Shares",
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:close-circle",
    ),
}

BOARD_SENSOR_TYPES: dict[str, SensorEntityDescription] = {
    "temp": SensorEntityDescription(
        key="temp",
        name="Board Temperature",
        native_unit_of_measurement=UnitOfTemperature.FAHRENHEIT,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    "chip_temp": SensorEntityDescription(
        key="chip_temp",
        name="Chip Temperature",
        native_unit_of_measurement=UnitOfTemperature.FAHRENHEIT,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    "hashrate": SensorEntityDescription(
        key="hashrate",
        name="Board Hashrate",
        native_unit_of_measurement=TERA_HASH_PER_SECOND,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:speedometer",
    ),
}

FAN_SENSOR_TYPES: dict[str, SensorEntityDescription] = {
    "speed": SensorEntityDescription(
        key="speed",
        name="Fan Speed",
        native_unit_of_measurement=REVOLUTIONS_PER_MINUTE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:fan",
    ),
}

# PID diagnostic sensors — read from the shared pid_state dict populated by
# the controller. "target" always reports; the internals are None (a chart
# gap) whenever the loop isn't computing, e.g. while the miner is stopped.
PID_TARGET_SENSOR_KEY = "target"
PID_TARGET_SENSOR = SensorEntityDescription(
    key="pid_target_temp",
    name="PID Target Temperature",
    native_unit_of_measurement=UnitOfTemperature.FAHRENHEIT,
    device_class=SensorDeviceClass.TEMPERATURE,
    state_class=SensorStateClass.MEASUREMENT,
    entity_category=EntityCategory.DIAGNOSTIC,
    icon="mdi:thermometer-lines",
)
PID_INTERNAL_SENSORS: dict[str, SensorEntityDescription] = {
    "error": SensorEntityDescription(
        # Stores a temperature *delta* (setpoint − PV). Declaring it as
        # TEMPERATURE makes HA apply the absolute F = 9/5·C + 32 conversion
        # to a delta and add a bogus +32° offset, so we omit device_class
        # and ship the unit string directly.
        key="pid_error",
        name="PID Error",
        native_unit_of_measurement="Δ°F",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:delta",
    ),
    "proportional": SensorEntityDescription(
        key="pid_proportional",
        name="PID Proportional",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    "integral": SensorEntityDescription(
        key="pid_integral",
        name="PID Integral",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    "derivative": SensorEntityDescription(
        key="pid_derivative",
        name="PID Derivative",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    "external": SensorEntityDescription(
        key="pid_external_compensation",
        name="PID External Compensation",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    "output": SensorEntityDescription(
        key="pid_output",
        name="PID Output",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    "requested_output": SensorEntityDescription(
        key="pid_requested_output",
        name="PID Requested Output",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    "pv_slope": SensorEntityDescription(
        key="pid_pv_slope",
        name="PID PV Slope",
        native_unit_of_measurement="°F/min",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:chart-line",
    ),
    "demand_index": SensorEntityDescription(
        key="pid_demand_index",
        name="PID Demand Index",
        native_unit_of_measurement="%",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:thermometer",
    ),
    "out_max_effective": SensorEntityDescription(
        key="pid_out_max_effective",
        name="PID Out Max Effective",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:arrow-up-bold",
    ),
    "out_min_effective": SensorEntityDescription(
        key="pid_out_min_effective",
        name="PID Out Min Effective",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:arrow-down-bold",
    ),
    "outdoor_mean": SensorEntityDescription(
        key="outdoor_24h_mean",
        name="Outdoor 24h Mean",
        native_unit_of_measurement=UnitOfTemperature.FAHRENHEIT,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:sun-thermometer-outline",
    ),
}

# Controller status enums. Available even while the miner is offline: the
# state they describe is local, and it matters most while the miner is off.
CONTROL_MODE_SENSOR = SensorEntityDescription(
    key="control_mode",
    name="Control Mode",
    device_class=SensorDeviceClass.ENUM,
    options=CONTROL_MODES,
    entity_category=EntityCategory.DIAGNOSTIC,
    icon="mdi:state-machine",
)
SHUTOFF_STATE_SENSOR = SensorEntityDescription(
    key="demand_shutoff_state",
    name="Demand Shutoff State",
    device_class=SensorDeviceClass.ENUM,
    options=SHUTOFF_STATES,
    entity_category=EntityCategory.DIAGNOSTIC,
    icon="mdi:power-sleep",
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Whatsminer sensors from a config entry."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: WhatsminerCoordinator = data["coordinator"]
    pid_state: dict = data["pid_state"]

    entities = []

    # Add main miner sensors
    for sensor_key, description in SENSOR_TYPES.items():
        entities.append(
            WhatsminerSensor(
                coordinator=coordinator,
                description=description,
                sensor_key=sensor_key,
            )
        )

    # Add hashboard sensors
    for idx, board in enumerate(coordinator.data.get("hashboards", [])):
        for sensor_key, description in BOARD_SENSOR_TYPES.items():
            entities.append(
                WhatsminerBoardSensor(
                    coordinator=coordinator,
                    description=description,
                    sensor_key=sensor_key,
                    board_index=idx,
                    board_slot=board.get("slot", idx),
                )
            )

    # Add fan sensors
    for idx in range(len(coordinator.data.get("fans", []))):
        for sensor_key, description in FAN_SENSOR_TYPES.items():
            entities.append(
                WhatsminerFanSensor(
                    coordinator=coordinator,
                    description=description,
                    sensor_key=sensor_key,
                    fan_index=idx,
                )
            )

    # PID diagnostic sensors — target always reports; internals gap when off.
    entities.append(
        WhatsminerPIDSensor(
            coordinator=coordinator,
            description=PID_TARGET_SENSOR,
            pid_state=pid_state,
            state_key=PID_TARGET_SENSOR_KEY,
            always_report=True,
        )
    )
    for state_key, description in PID_INTERNAL_SENSORS.items():
        entities.append(
            WhatsminerPIDSensor(
                coordinator=coordinator,
                description=description,
                pid_state=pid_state,
                state_key=state_key,
                always_report=False,
            )
        )
    entities.append(WhatsminerControlModeSensor(coordinator, pid_state))
    entities.append(WhatsminerShutoffStateSensor(coordinator, pid_state))

    async_add_entities(entities)


class WhatsminerSensor(CoordinatorEntity, SensorEntity):
    """Representation of a Whatsminer sensor."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: WhatsminerCoordinator,
        description: SensorEntityDescription,
        sensor_key: str,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._sensor_key = sensor_key
        self._attr_unique_id = f"{coordinator.data['mac']}_{sensor_key}"
        self._attr_name = description.name

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
    def native_value(self):
        """Return the state of the sensor."""
        value = self.coordinator.data.get(self._sensor_key)
        
        # Format hashrate values
        if self._sensor_key in ["hashrate", "expected_hashrate"] and value is not None:
            return round(value, 2)
        
        # Format efficiency
        if self._sensor_key == "efficiency" and value is not None:
            return round(value, 2)
            
        return value

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return self.coordinator.available and self.coordinator.last_update_success


class WhatsminerBoardSensor(CoordinatorEntity, SensorEntity):
    """Representation of a Whatsminer hashboard sensor."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: WhatsminerCoordinator,
        description: SensorEntityDescription,
        sensor_key: str,
        board_index: int,
        board_slot: int,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._sensor_key = sensor_key
        self._board_index = board_index
        self._board_slot = board_slot
        self._attr_unique_id = f"{coordinator.data['mac']}_board_{board_slot}_{sensor_key}"
        self._attr_name = f"Board {board_slot} {description.name}"

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
    def native_value(self):
        """Return the state of the sensor."""
        hashboards = self.coordinator.data.get("hashboards", [])
        if self._board_index < len(hashboards):
            return hashboards[self._board_index].get(self._sensor_key)
        return None

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return (
            self.coordinator.available
            and self.coordinator.last_update_success
            and self._board_index < len(self.coordinator.data.get("hashboards", []))
        )


class WhatsminerFanSensor(CoordinatorEntity, SensorEntity):
    """Representation of a Whatsminer fan sensor."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: WhatsminerCoordinator,
        description: SensorEntityDescription,
        sensor_key: str,
        fan_index: int,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._sensor_key = sensor_key
        self._fan_index = fan_index
        self._attr_unique_id = f"{coordinator.data['mac']}_fan_{fan_index}_{sensor_key}"
        self._attr_name = f"Fan {fan_index + 1} Speed"

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
    def native_value(self):
        """Return the state of the sensor."""
        fans = self.coordinator.data.get("fans", [])
        if self._fan_index < len(fans):
            return fans[self._fan_index].get(self._sensor_key)
        return None

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return (
            self.coordinator.available
            and self.coordinator.last_update_success
            and self._fan_index < len(self.coordinator.data.get("fans", []))
        )


class WhatsminerPIDSensor(CoordinatorEntity, SensorEntity):
    """Diagnostic sensor reading from the shared PID state dict."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: WhatsminerCoordinator,
        description: SensorEntityDescription,
        pid_state: dict,
        state_key: str,
        always_report: bool,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._pid_state = pid_state
        self._state_key = state_key
        # Kept for the target sensor, which must never gap.
        self._always_report = always_report
        self._attr_unique_id = f"{coordinator.data['mac']}_{description.key}"
        self._attr_name = description.name

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
    def native_value(self):
        """Return the latest PID state value, or None to produce a chart gap."""
        value = self._pid_state.get(self._state_key)
        if isinstance(value, float):
            if self._state_key == "demand_index":
                return round(value * 100.0, 1)
            return round(value, 2)
        return value

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return self.coordinator.available and self.coordinator.last_update_success


class WhatsminerControlModeSensor(CoordinatorEntity, SensorEntity):
    """Which layer of the controller is deciding the power limit right now."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: WhatsminerCoordinator, pid_state: dict) -> None:
        super().__init__(coordinator)
        self.entity_description = CONTROL_MODE_SENSOR
        self._pid_state = pid_state
        self._attr_unique_id = f"{coordinator.data['mac']}_control_mode"
        self._attr_name = CONTROL_MODE_SENSOR.name

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
        """Local controller state: available even while the miner is offline."""
        return True

    @property
    def native_value(self):
        mode = self._pid_state.get("control_mode")
        return mode if mode in CONTROL_MODES else "idle"

    @property
    def extra_state_attributes(self) -> dict:
        shutoff = self._pid_state.get("demand_shutoff") or {}
        freeze = self._pid_state.get("freeze_guard") or {}
        return {
            "supply_lockout_latched": bool(self._pid_state.get("lockout_latched")),
            "safety_engaged": bool(self._pid_state.get("safety_engaged")),
            "miner_data_fresh": bool(self.coordinator.last_update_success),
            "demand_shutoff_mode": shutoff.get("mode"),
            "demand_shutoff_state": shutoff.get("state"),
            "demand_shutoff_active": shutoff.get("active"),
            "demand_shutoff_reason": shutoff.get("reason"),
            "demand_shutoff_blocking": shutoff.get("blocking"),
            "demand_shutoff_since": shutoff.get("since"),
            "demand_shutoff_gate": shutoff.get("gate"),
            "demand_shutoff_trigger": shutoff.get("trigger"),
            "demand_shutoff_would_stop": shutoff.get("would_stop"),
            "demand_shutoff_would_resume": shutoff.get("would_resume"),
            "freeze_guard_status": freeze.get("status"),
            "freeze_guard_active": freeze.get("active"),
            "freeze_guard_source": freeze.get("source"),
            "freeze_guard_value": freeze.get("value"),
            "freeze_guard_threshold": freeze.get("threshold"),
            "outdoor_24h_mean": self._pid_state.get("outdoor_mean"),
        }


class WhatsminerShutoffStateSensor(WhatsminerControlModeSensor):
    """Demand shutoff state machine state."""

    def __init__(self, coordinator: WhatsminerCoordinator, pid_state: dict) -> None:
        super().__init__(coordinator, pid_state)
        self.entity_description = SHUTOFF_STATE_SENSOR
        self._attr_unique_id = f"{coordinator.data['mac']}_demand_shutoff_state"
        self._attr_name = SHUTOFF_STATE_SENSOR.name

    @property
    def native_value(self):
        state = (self._pid_state.get("demand_shutoff") or {}).get("state")
        return state if state in SHUTOFF_STATES else "disabled"

    @property
    def extra_state_attributes(self) -> dict:
        shutoff = self._pid_state.get("demand_shutoff") or {}
        return {
            "mode": shutoff.get("mode"),
            "active": shutoff.get("active"),
            "reason": shutoff.get("reason"),
            "blocking": shutoff.get("blocking"),
            "since": shutoff.get("since"),
            "gate": shutoff.get("gate"),
            "trigger": shutoff.get("trigger"),
            "would_stop": shutoff.get("would_stop"),
            "would_resume": shutoff.get("would_resume"),
        }
