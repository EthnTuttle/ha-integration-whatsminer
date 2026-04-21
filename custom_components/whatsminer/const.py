"""Constants for the Whatsminer integration."""
from homeassistant.const import Platform

DOMAIN = "whatsminer"

# Platforms
PLATFORMS = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.SWITCH,
    Platform.NUMBER,
    Platform.CLIMATE,
]

# Configuration keys
CONF_IP = "host"
CONF_PASSWORD = "password"
CONF_PORT = "port"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_NAME = "name"
CONF_POWER_MIN = "power_min"
CONF_POWER_MAX = "power_max"
CONF_PID_KP = "pid_kp"
CONF_PID_KI = "pid_ki"
CONF_PID_KD = "pid_kd"
CONF_PID_TARGET_TEMP = "pid_target_temp"
CONF_EXTERNAL_TEMP_SENSOR = "external_temp_sensor"
CONF_CHIP_TEMP_SAFETY_CAP = "chip_temp_safety_cap"

# Defaults
DEFAULT_PORT = 4028
DEFAULT_PASSWORD = "admin"
DEFAULT_SCAN_INTERVAL = 30  # seconds
DEFAULT_POWER_MIN = 1000  # watts
DEFAULT_POWER_MAX = 5000  # watts
# PID tuning — conservative starting point for a ~3kW miner tracking chip temp.
# Kp is in W/°C: 200 means a 1°C overshoot trims 200W. Tune in the options flow.
DEFAULT_PID_KP = 200.0
DEFAULT_PID_KI = 5.0
DEFAULT_PID_KD = 100.0
DEFAULT_PID_TARGET_TEMP = 75.0  # °C, typical safe chip temperature
# Chip-temp safety cap only applies when an external temperature sensor is
# driving the PID. Above the cap, the PID output is forced down to power_min.
DEFAULT_CHIP_TEMP_SAFETY_CAP = 85.0  # °C

# Units
TERA_HASH_PER_SECOND = "TH/s"
JOULES_PER_TERA_HASH = "J/TH"

# Sensor keys
SENSOR_HASHRATE = "hashrate"
SENSOR_EXPECTED_HASHRATE = "expected_hashrate"
SENSOR_TEMPERATURE_AVG = "temperature_avg"
SENSOR_WATTAGE = "wattage"
SENSOR_WATTAGE_LIMIT = "wattage_limit"
SENSOR_EFFICIENCY = "efficiency"
SENSOR_FAN_SPEED = "fan_speed"
SENSOR_BOARD_TEMP = "board_temp"
SENSOR_CHIP_TEMP = "chip_temp"
SENSOR_BOARD_HASHRATE = "board_hashrate"
SENSOR_UPTIME = "uptime"
SENSOR_ACCEPTED = "accepted"
SENSOR_REJECTED = "rejected"

# Binary sensor keys
BINARY_SENSOR_MINING = "is_mining"
