"""Constants for the Whatsminer integration."""
from homeassistant.const import Platform

DOMAIN = "whatsminer"

# Platforms
PLATFORMS = [
    Platform.BUTTON,
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.SWITCH,
    Platform.NUMBER,
]

# Configuration keys
CONF_IP = "host"
CONF_PASSWORD = "password"
# API v3 "super" account password, used to stop/start mining on 4433.
CONF_SUPER_PASSWORD = "super_password"
CONF_PORT = "port"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_NAME = "name"
# MAC cached in entry.data after the first successful poll, so the entry can
# set up in a degraded state while the miner is unreachable.
CONF_MAC = "mac"
CONF_POWER_MIN = "power_min"
CONF_POWER_MAX = "power_max"
CONF_PID_KP = "pid_kp"
CONF_PID_KI = "pid_ki"
CONF_PID_KD = "pid_kd"
CONF_PID_KE = "pid_ke"
CONF_PID_TARGET_TEMP = "pid_target_temp"
CONF_EXTERNAL_TEMP_SENSOR = "external_temp_sensor"
CONF_PID_OUTDOOR_TEMP_SENSOR = "pid_outdoor_temp_sensor"
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
CONF_PID_DEMAND_ENTITIES = "pid_demand_entities"
CONF_PID_INTEGRAL_BAND = "pid_integral_band"
CONF_PID_SETPOINT_RAMP_RATE = "pid_setpoint_ramp_rate"
CONF_PID_PRICE_SENSOR = "pid_price_sensor"
CONF_PID_PRICE_HIGH = "pid_price_high"
CONF_PID_PRICE_LOW = "pid_price_low"
CONF_PID_SURPLUS_SENSOR = "pid_surplus_sensor"
CONF_PID_SURPLUS_DEFICIT = "pid_surplus_deficit"
CONF_PID_SURPLUS_FULL = "pid_surplus_full"
CONF_PID_WEATHER_ENTITY = "pid_weather_entity"
CONF_PID_FORECAST_LOOKAHEAD_MIN = "pid_forecast_lookahead_min"
CONF_PID_FORECAST_BLEND = "pid_forecast_blend"
CONF_PID_SLOPE_EWMA_TAU_S = "pid_slope_ewma_tau_s"
CONF_PID_FALLBACK_OUTDOOR_COLD = "pid_fallback_outdoor_cold"
CONF_PID_FALLBACK_OUTDOOR_WARM = "pid_fallback_outdoor_warm"
# Demand shutoff (power the miner off when every thermostat is idle)
CONF_PID_DEMAND_SHUTOFF_MODE = "pid_demand_shutoff_mode"
CONF_PID_DEMAND_SHUTOFF_OUTDOOR_MIN = "pid_demand_shutoff_outdoor_min"
CONF_PID_DEMAND_SHUTOFF_HYSTERESIS = "pid_demand_shutoff_hysteresis"
CONF_PID_DEMAND_SHUTOFF_IDLE_DWELL_MIN = "pid_demand_shutoff_idle_dwell_min"
CONF_PID_DEMAND_SHUTOFF_MIN_OFF_MIN = "pid_demand_shutoff_min_off_min"
CONF_PID_DEMAND_SHUTOFF_MIN_ON_MIN = "pid_demand_shutoff_min_on_min"
CONF_PID_DEMAND_SHUTOFF_SUPPLY_STOP = "pid_demand_shutoff_supply_stop"
CONF_PID_DEMAND_SHUTOFF_SUPPLY_DWELL_MIN = "pid_demand_shutoff_supply_dwell_min"
CONF_PID_DEMAND_SHUTOFF_UNKNOWN_GRACE_MIN = "pid_demand_shutoff_unknown_grace_min"
CONF_PID_DEMAND_SHUTOFF_COLD_ROOM_DELTA = "pid_demand_shutoff_cold_room_delta"
# Freeze guard for the miner's outdoor coolant loop
CONF_FREEZE_GUARD_SENSOR = "freeze_guard_sensor"
CONF_FREEZE_GUARD_THRESHOLD = "freeze_guard_threshold"
CONF_FREEZE_GUARD_FORECAST_HOURS = "freeze_guard_forecast_hours"
# Braiins Pool read-only web API (optional account/worker sensors)
CONF_BRAIINS_POOL_TOKEN = "braiins_pool_token"
CONF_BRAIINS_POOL_WORKER = "braiins_pool_worker"

# Option keys dropped in config-entry version 4 (PID-only refactor). Kept so
# async_migrate_entry can strip them from stored data/options.
REMOVED_OPTION_KEYS_V4: tuple[str, ...] = (
    "default_power_limit",
    "pid_demand_mode",
    "pid_demand_floor_frac",
    "pid_demand_ceiling_frac",
    "pid_demand_weight_by_error",
)

# Defaults
DEFAULT_PORT = 4028
DEFAULT_PASSWORD = "admin"
DEFAULT_SUPER_PASSWORD = "super"
DEFAULT_SCAN_INTERVAL = 30  # seconds
DEFAULT_POWER_MIN = 1000  # watts
DEFAULT_POWER_MAX = 5000  # watts
# Each adjust_power_limit call restarts mining. Only actuate when the PID
# output moves at least this many watts from the last commanded value, and
# not more often than this interval. Defaults err on the conservative side.
# Three-band step resolution. The closer we are to setpoint, the smaller the
# minimum step we'll fire — coarse moves fast when far off, fine nudges
# precisely near target. Thresholds compare |SP − PV| in °F.
#   |err| > coarse_band            → coarse step (max swings to recover)
#   fine_band < |err| ≤ coarse_band → medium step
#   |err| ≤ fine_band              → fine step
# Set fine_band = 0 to collapse to two bands; coarse_band = 0 + fine_band = 0
# to disable banding entirely (always uses coarse step).
DEFAULT_PID_MIN_POWER_STEP = 250  # watts (coarse — far from setpoint)
DEFAULT_PID_MIN_POWER_STEP_MEDIUM = 150  # watts (mid)
DEFAULT_PID_MIN_POWER_STEP_FINE = 50  # watts (fine — near setpoint)
DEFAULT_PID_COARSE_STEP_BAND = 9.0  # °F — boundary between far and mid (= 5°C)
DEFAULT_PID_FINE_STEP_BAND = 3.6  # °F — boundary between mid and near (= 2°C)
# Throttle is asymmetric: hydronic loops drop fast when zones call for heat
# (urgent — comfort impact), but mild overshoot when zones satisfy is harmless.
# Power-up commands use the shorter "increase" interval; power-down commands
# use the longer interval below to avoid thrashing the miner.
DEFAULT_PID_MIN_ADJUST_INTERVAL = 600  # seconds (10 min) — power-down floor
DEFAULT_PID_MIN_ADJUST_INTERVAL_INCREASE = 300  # seconds (5 min) — power-up floor
# PID tuning — conservative starting point for a ~3kW miner.
# Kp is in W/°F: 111.11 means a 1°F overshoot trims ~111W. Tune in the options flow.
DEFAULT_PID_KP = 111.11
DEFAULT_PID_KI = 2.78
DEFAULT_PID_KD = 55.56
DEFAULT_PID_KE = 0.0
DEFAULT_PID_TARGET_TEMP = 167.0  # °F, a reasonable external-target starting point (= 75°C)
DEFAULT_PID_OUTDOOR_TEMP_SENSOR = None
# Belt-and-suspenders over the miner's own firmware thermal protection: if the
# chip-temp average crosses this threshold, the PID is overridden to power_min
# regardless of what the external-sensor loop wants. Chip temp is NOT a PID
# input (noisy, already firmware-managed) — it's purely a veto on output.
DEFAULT_CHIP_TEMP_SAFETY_CAP = 185.0  # °F (= 85°C)
# Supply-side (boiler-loop) protection — chip-temp guards the miner; these
# guard the *plant*. Scout probe is upstream of the boiler's own high-limit,
# so these caps fire well before the boiler trips.
#   Soft cap: scout ≥ cap → force power_min. Recoverable; auto-clears below.
#   Hard cap: scout ≥ cap → also stop mining (latched). Operator must toggle
#            Mining Control back on to resume; crossing this means the soft
#            cap couldn't hold and the operator should review.
DEFAULT_PID_SUPPLY_TEMP_SAFETY_CAP = 122.0  # °F (= 50°C)
DEFAULT_PID_SUPPLY_TEMP_LOCKOUT = 140.0  # °F (= 60°C)
# Demand lockout: when every one of these climate entities is idle (none with
# hvac_action == "heating"), force power_min and engage the safety binary
# sensor. With no thermostat calling, the zone pumps are off and the primary
# loop is stagnant, so there is no flow to dissipate power into. Empty list
# disables demand handling (lockout and demand shutoff). Recoverable: the loop
# auto-resumes when any entity transitions back to "heating".
DEFAULT_PID_DEMAND_ENTITIES: list[str] = []
# Integral is only frozen when |SP − PV| > this band AND the output has hit a
# saturation rail (out_min/out_max). Outside the band but with actuator
# headroom, integration continues — that's the disturbance-recovery case where
# the integrator is supposed to push. Inside the band, integration always runs
# normally. 0 disables the conditional freeze entirely.
DEFAULT_PID_INTEGRAL_BAND = 5.4  # °F (= 3°C)
# Max rate (°F/min) at which the effective setpoint moves toward the user's
# target. 0 disables ramping (the PID sees the full step immediately). A
# non-zero value turns a large SP change into a smooth ramp, which keeps the
# integrator well-behaved on slow plants.
DEFAULT_PID_SETPOINT_RAMP_RATE = 0.0
DEFAULT_PID_WEATHER_ENTITY = None
DEFAULT_PID_FORECAST_LOOKAHEAD_MIN = 60
DEFAULT_PID_FORECAST_BLEND = 0.5
# Time constant (seconds) of the supply-temp slope EWMA used to bias the
# step-band classifier. 0 disables slope smoothing entirely.
DEFAULT_PID_SLOPE_EWMA_TAU_S = 0.0
# Probe-loss fallback: if the supply probe drops out while PID Mode is on, run
# open-loop on an outdoor-reset curve instead of dropping to power_min —
# power_max at/below COLD, power_min at/above WARM, linear between. Thermostat
# demand still gates it (all idle → power_min in lockout mode; envelope mode
# scales the bounds). With no outdoor reading the current limit is held.
DEFAULT_PID_FALLBACK_OUTDOOR_COLD = 10.0  # °F
DEFAULT_PID_FALLBACK_OUTDOOR_WARM = 60.0  # °F

# Demand shutoff. Two stop triggers, both requiring every thermostat idle:
#   W (warm gate): the centred 24 h outdoor mean is at/above OUTDOOR_MIN, so
#      1 kW is surplus and idle periods last hours. Dwell IDLE_DWELL_MIN.
#   S (supply overheat): supply at/above the soft cap at any outdoor temp. The
#      stagnant primary loop is being heated at power_min and would drift to
#      the 140°F latch. Dwell SUPPLY_DWELL_MIN; bypasses MIN_ON.
# OUTDOOR_MIN 58°F: bottom of the measured "1 kW is sufficient" band (60-62°F
# at spring setpoints, shifted down for this season's lower setpoints) and the
# middle of the physics crossover band for a 1600 sq ft slab. Recalibrate from
# observe-mode data. The miner is the primary heat for the monitored zones, so
# every ambiguous case
# biases toward heating (fail-warm).
DEMAND_SHUTOFF_MODES = ["off", "observe", "active"]
DEFAULT_PID_DEMAND_SHUTOFF_MODE = "off"
DEFAULT_PID_DEMAND_SHUTOFF_OUTDOOR_MIN = 58.0  # °F, centred 24 h mean
DEFAULT_PID_DEMAND_SHUTOFF_HYSTERESIS = 4.0  # °F
DEFAULT_PID_DEMAND_SHUTOFF_IDLE_DWELL_MIN = 30
DEFAULT_PID_DEMAND_SHUTOFF_MIN_OFF_MIN = 30
DEFAULT_PID_DEMAND_SHUTOFF_MIN_ON_MIN = 60
DEFAULT_PID_DEMAND_SHUTOFF_SUPPLY_STOP = True
DEFAULT_PID_DEMAND_SHUTOFF_SUPPLY_DWELL_MIN = 10
DEFAULT_PID_DEMAND_SHUTOFF_UNKNOWN_GRACE_MIN = 20
DEFAULT_PID_DEMAND_SHUTOFF_COLD_ROOM_DELTA = 1.5  # °F below setpoint counts as calling

# Freeze guard. The M64's own coolant loop runs outdoors and can freeze while
# the miner is stopped. When the freeze source reads at/below THRESHOLD, every
# stop trigger (W, S and the supply lockout latch) is blocked and a stop we own
# is resumed at once. Source priority: the dedicated loop/outdoor probe, else
# min(current outdoor temperature, forecast minimum over FORECAST_HOURS). With
# no source at all, stops are blocked unless the warm gate is armed.
# 40°F assumes plain water coolant (freezes at 32°F) plus margin for a probe
# that reads warmer than the coldest exposed fitting and for radiative cooling
# below air temperature on clear nights. Lower it if the loop holds glycol.
DEFAULT_FREEZE_GUARD_SENSOR = None
DEFAULT_FREEZE_GUARD_THRESHOLD = 40.0  # °F
DEFAULT_FREEZE_GUARD_FORECAST_HOURS = 12
# Release the freeze condition only this far above the threshold (°F) so a
# stop/resume can't flap around the line.
FREEZE_GUARD_RELEASE_HYSTERESIS = 3.0

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

# Enum values published by the controller
CONTROL_MODES = [
    "pid",            # closed loop on the supply probe
    "fallback",       # probe lost: open-loop outdoor-reset curve
    "demand_lockout", # all thermostats idle: clamped to power_min
    "safety_cap",     # chip or supply soft cap forcing power_min
    "dwell",          # shutoff dwell running (still clamped to power_min)
    "stopped",        # demand shutoff owns a stop
    "resuming",       # power_on sent, waiting for hashing
    "latched",        # supply lockout latched
    "idle",           # miner not mining and we did not stop it
]
SHUTOFF_STATES = ["disabled", "running", "dwell", "stopped", "resuming", "suppressed"]
