# Exergy - Whatsminer (Ethan's Mod) Integration for Home Assistant

A Home Assistant custom integration for MicroBT Whatsminer ASIC miners, designed for use with Exergy heat recovery systems.

Tested with firmware: `Whatsminer-all-20251209.16`

## Features

- **Sensors**: Hashrate, Expected Hashrate, Temperature, Power Consumption, Power Limit, Efficiency (J/TH), Uptime, Accepted Shares, Rejected Shares
- **Per-hashboard sensors**: Board Temperature, Chip Temperature, Board Hashrate
- **Fan sensors**: Fan Speed (RPM) — when applicable
- **Binary Sensor**: Mining Status (running/not running)
- **Switches**: Mining Control (start/stop mining); **PID Mode** (enable/disable temperature-targeted power modulation)
- **Numbers**: Power Limit (slider, blocked while PID Mode is on); **PID Target Temperature** (setpoint, dashboard-adjustable)
- **Temperature targeting**: PID Mode drives the miner's power limit from a **required** external Home Assistant temperature sensor (e.g. a boiler loop, a storage tank) — the miner's own chip temp is deliberately not used in the control loop (noisy, and the miner firmware already self-manages thermals)
- **Chip-temp safety cap**: configurable hard veto on PID output — if the chip-temp average crosses the cap (default 85°C), the PID is overridden to the minimum power limit and a `PID Safety Engaged` binary sensor flips to `problem`. Belt-and-suspenders over the firmware's own thermal protection so overheat events are visible in HA, not silently absorbed
- **PID-off fallback**: when PID Mode is turned off, the miner reverts to a configurable **Default Power Limit** (defaults to the configured maximum) instead of getting stuck at whatever wattage the PID last commanded
- **PID diagnostic sensors**: Target, Error, Proportional, Integral, Derivative, Output, Requested Output — for charting and tuning
- **Auto-recovery from mining shutoffs**: on any mining on↔off transition (miner auto-shutoff, firmware thermal cutback, network drop, manual off), the PID clears its integrator and throttle clock so the next resumption starts fresh with a bumpless-transfer re-seed instead of firing stale state

## Installation via HACS

1. In Home Assistant, go to **HACS → Integrations → ⋮ → Custom repositories**
2. Add `https://github.com/EthnTuttle/ha-integration-whatsminer` with category **Integration**
3. Search for **Whatsminer** in HACS and install
4. Restart Home Assistant
5. Go to **Settings → Devices & Services → Add Integration** and search for **Whatsminer**

## Manual Installation

1. Copy the `custom_components/whatsminer` folder into your HA `custom_components` directory
2. Restart Home Assistant
3. Go to **Settings → Devices & Services → Add Integration** and search for **Whatsminer**

## Configuration

| Field | Default | Description |
|-------|---------|-------------|
| Host | — | Miner IP address |
| Name | Whatsminer `<ip>` | Friendly name |
| Password | `admin` | Miner admin password |
| Port | `4028` | API port |
| Scan Interval | `30` s | Poll frequency (10–300 s) |
| Power Min | `1000` W | Lower bound for power limit slider |
| Power Max | `5000` W | Upper bound for power limit slider |
| External Temperature Sensor | — | **Required for PID Mode.** Any HA `sensor` with `device_class: temperature` |
| Chip Temp Safety Cap | `85` °C | If chip-temp average crosses this, PID output is overridden to `Power Min` and `binary_sensor.<miner>_pid_safety_engaged` flips on |
| PID Min Power Step | `250` W | Suppress PID commands smaller than this (reduces mining restarts) |
| PID Min Adjust Interval | `600` s | Minimum seconds between `adjust_power_limit` calls |

## Requirements

- `passlib >= 1.7.4`
- `pycryptodome >= 3.20.0`

These are installed automatically by Home Assistant.

## Charts & PID tuning

The integration exposes diagnostic sensors for the PID internals so you can drop a `history-graph` card into any dashboard and watch the loop. No HACS frontend dependency required.

**Entity IDs**: Home Assistant derives entity IDs from the miner's Name. Replace `<miner>` below with the slugified version (e.g. "Basement Miner" → `basement_miner`). If unsure, check **Developer Tools → States** and search `pid`.

### Chart A — Tracking (the money chart)

Target vs current temperature vs error. If the PID is doing its job, Current should hover around Target and Error should sit near zero.

```yaml
type: history-graph
hours_to_show: 6
title: Miner — PID Tracking
entities:
  - entity: sensor.<miner>_pid_target_temperature
    name: Target
  - entity: sensor.<miner>_temperature
    name: Current
  - entity: sensor.<miner>_pid_error
    name: Error
```

### Chart B — PID term breakdown

P, I, and D contributions (watts). Reveals which term is doing the work — useful when deciding what to raise or lower.

```yaml
type: history-graph
hours_to_show: 6
title: Miner — PID Terms
entities:
  - entity: sensor.<miner>_pid_proportional
  - entity: sensor.<miner>_pid_integral
  - entity: sensor.<miner>_pid_derivative
```

### Chart C — Actuator response

What the PID asked for vs what the miner accepted vs what it actually drew. Shows command lag and saturation.

```yaml
type: history-graph
hours_to_show: 6
title: Miner — Power Response
entities:
  - entity: sensor.<miner>_pid_output
    name: PID Command
  - entity: sensor.<miner>_power_limit
    name: Miner Limit
  - entity: sensor.<miner>_power_consumption
    name: Actual Draw
```

### Chart D — Efficiency (works regardless of PID)

Hashrate vs power vs efficiency (J/TH) — the "how well is this miner running?" view.

```yaml
type: history-graph
hours_to_show: 24
title: Miner — Efficiency
entities:
  - entity: sensor.<miner>_hashrate
  - entity: sensor.<miner>_power_consumption
  - entity: sensor.<miner>_efficiency
```

### Targeting an external sensor (required)

PID Mode regulates power off an external HA temperature sensor — the miner's own chip temp is deliberately ignored by the loop (it's noisy, and the miner firmware already manages its own thermal envelope). Pick the sensor that reflects what you're actually trying to heat: a boiler loop probe, a storage tank, a room sensor, etc. Fahrenheit sensors are auto-converted to Celsius.

PID Mode refuses to enable if no external sensor is set. If the sensor becomes unavailable while PID is running, the loop pauses (no new `adjust_power_limit` commands) until the sensor returns — it won't fall back to the miner's internal reading.

Even though the PID *inputs* only the external sensor, the chip temp retains a hard veto on the *output*: if `temperature_avg` crosses the **Chip Temperature Safety Cap** (default 85°C), the PID is forced to `Power Min` on the next tick and `binary_sensor.<miner>_pid_safety_engaged` flips on. This is a deliberate belt-and-suspenders over the miner's firmware-level thermal protection — the firmware will throttle and protect itself regardless, but without the cap those events are invisible to HA automations and dashboards.

Expect **Kp to need serious retuning** for a slow thermal mass: a boiler loop has ~100× the thermal inertia of a chip, so the default Kp=200 will be far too aggressive. Try Kp=20–50 with Ki and Kd starting at zero.

### Tuning recipe

Defaults (Kp=200, Ki=5, Kd=100, target=75°C) are a conservative starting point. To tune for your miner:

1. **Start with Ki=0, Kd=0** in the integration's options.
2. **Raise Kp** until Chart A shows visible oscillation around the target.
3. **Halve Kp** to damp the oscillation.
4. **Add small Ki** (start at 1–5) to eliminate steady-state error. Watch Chart B — if the integral term runs away, Ki is too high.
5. **Add small Kd** (start at 50–100) to reduce overshoot. If Chart B shows noisy D, your scan interval is probably too short — raise it in options.

Give each change at least 10–15 minutes to settle before judging — miner thermal response is slow.

### Actuation throttle (why the Power Limit chart looks "stepped")

Every `adjust_power_limit` call restarts the miner's mining process. To protect the miner from thrashing, the PID only actually sends a command when **both**: the new value differs from the last commanded value by at least **PID Min Power Step** (default 250 W), **and** at least **PID Min Adjust Interval** seconds (default 600) have passed since the last command. Between those moments the PID math keeps running and `sensor.<miner>_pid_requested_output` keeps updating — only the actuator write is suppressed. Watch `sensor.<miner>_pid_output` (last actuated value) vs `sensor.<miner>_pid_requested_output` (what the PID wants) on Chart C to see the throttle working.

The chip-temp safety cap bypasses the time throttle — overheat commands go out on the next tick, not 10 minutes later. Sub-step wiggles are still suppressed.

If the miner auto-shuts-off mid-run (firmware thermal cutback, network drop, manual off, etc.), the PID now resets its integrator, samples, and throttle clock on the off→on transition and re-seeds bumpless transfer from the current wattage instead of resuming with hours of stale state.

Tighten both values for faster response on a responsive thermal target; loosen them for a large thermal mass (boiler loop, storage tank) where fast commands are wasted work. Set `PID Min Adjust Interval` to `0` to disable the time throttle and revert to magnitude-only.
