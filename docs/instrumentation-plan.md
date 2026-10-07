# Instrumentation plan: what to measure so the heating can be optimised

This file records what the system measures today, what it is missing, and where each new sensor should go. It was written 2026-10-07 from a status review. Update the status column as sensors land.

---

## 1. How the plant is laid out

```
miner ──(outdoor loop, freeze-prone)── HX ── boiler loop ── mixing valve ── radiant loop ── floors
                                              │  flow meter                  │  flow meter
                                              └─ sensor.scout_21_temperature_probe (boiler-loop supply)
```

- **The miner heats a boiler loop through a heat exchanger.** The PID controls that boiler loop.
- **The boiler loop feeds a mixing valve.** The valve tempers the water down before it enters the radiant floor loop.
- **The PID's supply probe sits in the boiler loop, before the mixer.** `sensor.scout_21_temperature_probe` is on ESPHome Scout_21 at 10.0.0.103. **It is not the temperature the floors see.**
- **Flow:** the boiler loop and the radiant loop each have a flow meter.
- **Thermostats:** four Venstars (den, great room, back bedroom, windowed bedroom). Main-level Venstars are planned.
- **The miner isn't the only heat in the house.**

### Why the mixer matters for tuning

A mixer can only cool the water. Any boiler-loop temperature above what the mixer needs is margin that costs heat:

- more heat is lost on the outdoor run
- more heat is lost through the HX
- the miner runs hotter

**The most efficient boiler-loop target is the radiant supply the mixer is delivering, plus a few °F of headroom.** At the time of writing the PID target was 104°F, picked without knowing the post-mixer temperature. Measuring the radiant supply (item 1 below) is what lets us lower the target with confidence.

Open questions:
- Is the mixer thermostatic (fixed setting) or motorised/outdoor-reset? If it's thermostatic, what is its dial set to?
- Are the flow meters electronic (pulse or analog output) or visual gauges? If they're visual, a static GPM reading per loop is still useful.

---

## 2. What we have today

| Signal | Source | Notes |
|---|---|---|
| Boiler-loop supply temp | `sensor.scout_21_temperature_probe` | PID process variable. |
| Miner power, limit, hashrate, chip/board temps, uptime | heatcore integration | Power is the miner's own **estimate**, not a measurement. |
| PID terms, control mode, demand index, freeze guard, learned floor | heatcore integration | |
| Room temp, setpoint, heating/idle | 4 × Venstar `climate.*` | The thermostats' "heating" status currently stands in for zone valve state. |
| Outdoor temp | `weather.forecast_home` | Forecast-grade; freeze guard source since 2026-10-07. |
| Pool hashrate, shares, rewards, balance | Braiins Pool sensors (v1.7.0+) | Token entered 2026-10-07. |

---

## 3. What's missing, in priority order

| # | Signal | Why it matters | Plan / status |
|---|---|---|---|
| 1 | **Boiler-loop return temp** | Supply minus return, times boiler flow, gives the heat the miner actually puts into the house. | Planned: probe to be added. |
| 1b | **Radiant-loop supply (after the mixer) and radiant return** | This is the temperature the floors see, and the heat the floors take in. It tells us the minimum boiler target (see §1). | Planned: probes to be added. |
| 2 | **Flow, boiler loop and radiant loop** | Needed for any heat (BTU) figure. Flow meters exist on both loops. | Find out whether they have an electronic output; if not, record a static GPM per loop. |
| 3 | **Miner-side HX in/out (glycol)** | Shows how well the HX works, and how much heat the outdoor run loses. | In progress. |
| 4 | **Outdoor loop / pipe temp at the coldest exposed point** | This is the real freeze risk, and a better freeze-guard source than the forecast. | In progress. |
| 5 | **Local outdoor air temp** | Outdoor temperature drives heating need, and the forecast is coarse. | In progress. Check whether the Venstars accept a wired outdoor sensor. |
| 6 | **Measured electrical power: miner, then pumps** | Gives true kWh and cost. The miner's own power reading is an estimate. | See §4 for the TP-Link caveat. |
| 7 | **Slab / floor temperature, one or two zones** | Slab lag (time constant about 5 h) is what the controller fights; floor temperature allows feedforward instead of reacting late. | Placement in §5. |
| 8 | **Zone valve / actuator states** | Shows which loops are actually open. | Custom wiring job; later. |
| 9 | **The other heat source's runtime** | Without it we can't tell which source warmed a room. | Main-floor Venstars should cover this. |
| 10 | **Electricity price** | Turns kWh into dollars, and BTC/kWh into profit or loss. | Static rate from Rappahannock Electric Cooperative. Enter it as a fixed price in the HA Energy dashboard, or as an `input_number` helper. |

---

## 4. Electrical power: TP-Link plugs won't take the miner

- **Plugs top out at 1800 W.** TP-Link energy-monitoring plugs (Kasa KP115/HS110/EP25, Tapo P110) are rated 15 A at 120 V, about 1800 W.
- **The M64 is too big for a plug.** It draws 2–5 kW on 240 V and can't go through one.
- **Use a current-clamp (CT) monitor on the miner's circuit instead.** Examples: Emporia Vue, Shelly Pro EM / EM with 50 A clamps, IoTaWatt. All of these integrate with HA locally or in the cloud.
- **Pumps are fine on a plug.** If they're on cords, a TP-Link energy plug works for them. If they're hard-wired, they can share the CT monitor.

---

## 5. Where to put a slab / floor temperature probe

The goal is a reading that stands for the floor's stored heat, not one pipe.

- **Pick a representative zone.**
  - Choose an interior area that people actually use.
  - Keep it away from exterior doors, windows and direct sun.
  - Avoid rugs, furniture and kitchen appliances.
  - Don't put it in the first few feet of a loop, where the water is hottest.
- **Place it between two tubes, not on one.** Mid-span between runs gives the slab temperature, not the pipe temperature. A thermal camera, or the floor while the loop is hot, shows where the tubes run.
- **If the tubing is in a slab or thin pour**, there are two ways in:
  - *Best:* a probe in a small drilled hole mid-span between tubes, sealed with thermal paste and caulk. Drill only after locating the tubing.
  - *Non-invasive:* a flat probe taped to the floor surface under a small foam or cork pad. It reads a little low and lags slightly, but trends well.
- **If the tubing is staple-up under a wood subfloor** (accessible from the basement), fix the probe to the underside of the subfloor between two tube runs, under the insulation.
- **Probes:** a DS18B20 or 10k NTC on the existing ESPHome boards is enough. One per zone is plenty; start with the great room.

---

## 6. What the analytics will compute once these exist

- Heat delivered (BTU/h and kWh-thermal) on the boiler loop and the radiant loop. The difference between them is heat lost in piping and at the mixer.
- Fraction of electrical input that reaches the floor. This needs item 6 plus items 1 and 2.
- Heat loss of the outdoor run, against outdoor temperature.
- Lowest workable boiler target for each outdoor temperature.
- Heating need against outdoor temperature (house heat-loss coefficient). This feeds outdoor-reset feedforward.
- Cost per day, and BTC earned against electricity cost.
- Controller health:
  - limit commands per day
  - restarts per day
  - time at the floor
  - supply tracking error and overshoot

Storage stays in HA: the recorder for raw history, long-term statistics for trends, and a longer `purge_keep_days` if needed. A daily job captures the data, analyses it and reports regressions. See the extended `scripts/pid-capture.py` and `scripts/pid-analyze.py` once they're built.
