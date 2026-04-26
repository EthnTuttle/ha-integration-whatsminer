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
CONF_PID_MIN_POWER_STEP_MEDIUM = "pid_min_power_step_medium"
CONF_PID_MIN_POWER_STEP_FINE = "pid_min_power_step_fine"
CONF_PID_COARSE_STEP_BAND = "pid_coarse_step_band"
CONF_PID_FINE_STEP_BAND = "pid_fine_step_band"
CONF_PID_MIN_ADJUST_INTERVAL = "pid_min_adjust_interval"
CONF_PID_MIN_ADJUST_INTERVAL_INCREASE = "pid_min_adjust_interval_increase"
CONF_CHIP_TEMP_SAFETY_CAP = "chip_temp_safety_cap"
CONF_PID_SUPPLY_TEMP_SAFETY_CAP = "pid_supply_temp_safety_cap"
CONF_PID_SUPPLY_TEMP_LOCKOUT = "pid_supply_temp_lockout"
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
# Three-band step resolution. The closer we are to setpoint, the smaller the
# minimum step we'll fire — coarse moves fast when far off, fine nudges
# precisely near target. Thresholds compare |SP − PV| in °C.
#   |err| > coarse_band            → coarse step (max swings to recover)
#   fine_band < |err| ≤ coarse_band → medium step
#   |err| ≤ fine_band              → fine step
# Set fine_band = 0 to collapse to two bands; coarse_band = 0 + fine_band = 0
# to disable banding entirely (always uses coarse step).
DEFAULT_PID_MIN_POWER_STEP = 250  # watts (coarse — far from setpoint)
DEFAULT_PID_MIN_POWER_STEP_MEDIUM = 100  # watts (mid)
DEFAULT_PID_MIN_POWER_STEP_FINE = 25  # watts (fine — near setpoint)
DEFAULT_PID_COARSE_STEP_BAND = 5.0  # °C — boundary between far and mid
DEFAULT_PID_FINE_STEP_BAND = 2.0  # °C — boundary between mid and near
# Throttle is asymmetric: hydronic loops drop fast when zones call for heat
# (urgent — comfort impact), but mild overshoot when zones satisfy is harmless.
# Power-up commands use the shorter "increase" interval; power-down commands
# use the longer interval below to avoid thrashing the miner.
DEFAULT_PID_MIN_ADJUST_INTERVAL = 600  # seconds (10 min) — power-down floor
DEFAULT_PID_MIN_ADJUST_INTERVAL_INCREASE = 300  # seconds (5 min) — power-up floor
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
# Supply-side (boiler-loop) protection — chip-temp guards the miner; these
# guard the *plant*. Scout probe is upstream of the boiler's own high-limit,
# so these caps fire well before the boiler trips.
#   Soft cap: scout ≥ cap → force power_min. Recoverable; auto-clears below.
#   Hard cap: scout ≥ cap → also stop mining (latched). Operator must toggle
#            Mining Control back on to resume; crossing this means the soft
#            cap couldn't hold and the operator should review.
DEFAULT_PID_SUPPLY_TEMP_SAFETY_CAP = 50.0  # °C (122 °F)
DEFAULT_PID_SUPPLY_TEMP_LOCKOUT = 60.0  # °C (140 °F)
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
