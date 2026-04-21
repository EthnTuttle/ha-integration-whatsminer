# Exergy - Whatsminer Integration for Home Assistant

A Home Assistant custom integration for MicroBT Whatsminer ASIC miners, designed for use with Exergy heat recovery systems.

Tested with firmware: `Whatsminer-all-20251209.16`

## Features

- **Sensors**: Hashrate, Expected Hashrate, Temperature, Power Consumption, Power Limit, Efficiency (J/TH), Uptime, Accepted Shares, Rejected Shares
- **Per-hashboard sensors**: Board Temperature, Chip Temperature, Board Hashrate
- **Fan sensors**: Fan Speed (RPM) — when applicable
- **Binary Sensor**: Mining Status (running/not running)
- **Switches**: Mining Control (start/stop mining); **PID Mode** (enable/disable temperature-targeted power modulation)
- **Numbers**: Power Limit (slider, blocked while PID Mode is on); **PID Target Temperature** (setpoint, dashboard-adjustable)
- **Temperature targeting**: the PID targets the miner's chip temp by default, or any Home Assistant temperature sensor (e.g. a boiler loop, a storage tank) — see the external-sensor section below
- **Chip-temp safety cap**: when an external sensor drives the loop, a configurable chip-temp ceiling clamps power to minimum if the miner overheats
- **PID-off fallback**: when PID Mode is turned off, the miner reverts to a configurable **Default Power Limit** (defaults to the configured maximum) instead of getting stuck at whatever wattage the PID last commanded
- **PID diagnostic sensors**: Target, Error, Proportional, Integral, Derivative, Output, Requested Output — plus a "PID Safety Engaged" binary sensor — for charting and tuning

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

### Targeting an external sensor (e.g. boiler loop)

By default the PID targets the miner's own chip temperature. To hold a different temperature — say, a boiler loop or storage tank — set **External Temperature Sensor** in the integration options to that sensor's entity. Fahrenheit sensors are auto-converted to Celsius.

When an external sensor is driving the loop, the miner's intrinsic thermal variable is no longer part of the control signal, so a **Chip Temperature Safety Cap** (default 85°C) kicks in: if chip temp exceeds the cap, power is forced to the minimum regardless of what the PID wants. The `binary_sensor.<miner>_pid_safety_engaged` entity goes `on` while the cap is overriding. Watch `sensor.<miner>_pid_requested_output` against `sensor.<miner>_pid_output` to see how often the clamp is engaging.

Expect **Kp to need serious retuning** — a boiler loop has ~100× the thermal mass of a chip, so the default Kp=200 will be way too aggressive. Try Kp=20–50 with Ki and Kd starting at zero.

### Tuning recipe

Defaults (Kp=200, Ki=5, Kd=100, target=75°C) are a conservative starting point. To tune for your miner:

1. **Start with Ki=0, Kd=0** in the integration's options.
2. **Raise Kp** until Chart A shows visible oscillation around the target.
3. **Halve Kp** to damp the oscillation.
4. **Add small Ki** (start at 1–5) to eliminate steady-state error. Watch Chart B — if the integral term runs away, Ki is too high.
5. **Add small Kd** (start at 50–100) to reduce overshoot. If Chart B shows noisy D, your scan interval is probably too short — raise it in options.

Give each change at least 10–15 minutes to settle before judging — miner thermal response is slow.
