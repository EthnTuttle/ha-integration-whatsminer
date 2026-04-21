"""Support for Whatsminer sensors."""
from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
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
    DOMAIN,
    JOULES_PER_TERA_HASH,
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
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
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
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    "chip_temp": SensorEntityDescription(
        key="chip_temp",
        name="Chip Temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
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
# the climate entity. Split into "target" (always reports, used for tracking
# chart) and "internals" (gapped when PID is disabled, so history-graph shows
# a clean break rather than a misleading flatline).
PID_TARGET_SENSOR_KEY = "target"
PID_TARGET_SENSOR = SensorEntityDescription(
    key="pid_target_temp",
    name="PID Target Temperature",
    native_unit_of_measurement=UnitOfTemperature.CELSIUS,
    device_class=SensorDeviceClass.TEMPERATURE,
    state_class=SensorStateClass.MEASUREMENT,
    entity_category=EntityCategory.DIAGNOSTIC,
    icon="mdi:thermometer-lines",
)
PID_INTERNAL_SENSORS: dict[str, SensorEntityDescription] = {
    "error": SensorEntityDescription(
        key="pid_error",
        name="PID Error",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
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
}


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
        # always_report=True keeps target visible even when PID is disabled, so
        # the tracking chart's setpoint line survives OFF/COOL toggles.
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
        if not self._always_report and not self._pid_state.get("enabled"):
            return None
        value = self._pid_state.get(self._state_key)
        if isinstance(value, float):
            return round(value, 2)
        return value

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return self.coordinator.available and self.coordinator.last_update_success
