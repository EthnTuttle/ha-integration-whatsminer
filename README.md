# Exergy - Whatsminer Integration for Home Assistant

A Home Assistant custom integration for MicroBT Whatsminer ASIC miners, designed for use with Exergy heat recovery systems.

Tested with firmware: `Whatsminer-all-20251209.16`

## Features

- **Sensors**: Hashrate, Expected Hashrate, Temperature, Power Consumption, Power Limit, Efficiency (J/TH), Uptime, Accepted Shares, Rejected Shares
- **Per-hashboard sensors**: Board Temperature, Chip Temperature, Board Hashrate
- **Fan sensors**: Fan Speed (RPM) — when applicable
- **Binary Sensor**: Mining Status (running/not running)
- **Switch**: Mining Control (start/stop mining)
- **Number**: Power Limit slider (adjust wattage cap)

## Installation via HACS

1. In Home Assistant, go to **HACS → Integrations → ⋮ → Custom repositories**
2. Add `https://github.com/tronsington/ha-integration-whatsminer` with category **Integration**
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
