"""Constants for the Whatsminer integration."""
from homeassistant.const import Platform

DOMAIN = "whatsminer"

# Platforms
PLATFORMS = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.SWITCH,
    Platform.NUMBER,
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
CONF_DEFAULT_POWER_LIMIT = "default_power_limit"
CONF_PID_MIN_POWER_STEP = "pid_min_power_step"
CONF_PID_MIN_ADJUST_INTERVAL = "pid_min_adjust_interval"
CONF_CHIP_TEMP_SAFETY_CAP = "chip_temp_safety_cap"
CONF_PID_INTEGRAL_BAND = "pid_integral_band"
CONF_PID_SETPOINT_RAMP_RATE = "pid_setpoint_ramp_rate"

# Defaults
DEFAULT_PORT = 4028
DEFAULT_PASSWORD = "admin"
DEFAULT_SCAN_INTERVAL = 30  # seconds
DEFAULT_POWER_MIN = 1000  # watts
DEFAULT_POWER_MAX = 5000  # watts
# Each adjust_power_limit call restarts mining. Only actuate when the PID
# output moves at least this many watts from the last commanded value, and
# not more often than this interval. Defaults err on the conservative side.
DEFAULT_PID_MIN_POWER_STEP = 250  # watts
DEFAULT_PID_MIN_ADJUST_INTERVAL = 600  # seconds (10 min)
# PID tuning — conservative starting point for a ~3kW miner.
# Kp is in W/°C: 200 means a 1°C overshoot trims 200W. Tune in the options flow.
DEFAULT_PID_KP = 200.0
DEFAULT_PID_KI = 5.0
DEFAULT_PID_KD = 100.0
DEFAULT_PID_TARGET_TEMP = 75.0  # °C, a reasonable external-target starting point
# Applied when PID Mode is turned off — avoids leaving the miner stuck at the
# last wattage the PID commanded. Defaults to power_max (full tilt).
DEFAULT_DEFAULT_POWER_LIMIT = DEFAULT_POWER_MAX
# Belt-and-suspenders over the miner's own firmware thermal protection: if the
# chip-temp average crosses this threshold, the PID is overridden to power_min
# regardless of what the external-sensor loop wants. Chip temp is NOT a PID
# input (noisy, already firmware-managed) — it's purely a veto on output.
DEFAULT_CHIP_TEMP_SAFETY_CAP = 85.0  # °C
# Integral is only frozen when |SP − PV| > this band AND the output has hit a
# saturation rail (out_min/out_max). Outside the band but with actuator
# headroom, integration continues — that's the disturbance-recovery case where
# the integrator is supposed to push. Inside the band, integration always runs
# normally. 0 disables the conditional freeze entirely.
DEFAULT_PID_INTEGRAL_BAND = 3.0
# Max rate (°C/min) at which the effective setpoint moves toward the user's
# target. 0 disables ramping (the PID sees the full step immediately). A
# non-zero value turns a large SP change into a smooth ramp, which keeps the
# integrator well-behaved on slow plants.
DEFAULT_PID_SETPOINT_RAMP_RATE = 0.0

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
