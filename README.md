# Exergy - Whatsminer (Ethan's Mod) Integration for Home Assistant

A Home Assistant custom integration for MicroBT Whatsminer ASIC miners used as the heat source of a hydronic heating system (Exergy heat recovery). The miner's power limit is driven by a PID loop on a supply-water temperature probe; the integration also owns the decision to stop and restart the miner when there is no heating demand, and guards the miner's outdoor coolant loop against freezing.

Tested with firmware: `Whatsminer-all-20251209.16`

All temperatures are in °F.

## Features

- **Sensors**: Hashrate, Expected Hashrate, Temperature, Power Consumption, Power Limit (read-only), Efficiency (J/TH), Uptime, Accepted Shares, Rejected Shares
- **Per-hashboard sensors**: Board Temperature, Chip Temperature, Board Hashrate
- **Fan sensors**: Fan Speed (RPM), when applicable
- **Binary sensors**: Mining Status; PID Safety Engaged (a cap or lockout is clamping output); Demand Shutoff (on while the integration owns a stop); Freeze Guard (on while freeze risk blocks stops)
- **Switch**: Mining Control. This is the manual emergency override: off stops mining and is never auto-resumed; on clears any stop the integration owns.
- **Buttons**: Reset Supply Lockout (clears the latched 140°F supply lockout); Reset Learned Floor (forgets a learned power floor after the miner has been serviced)
- **Number**: PID Target Temperature (setpoint, dashboard-adjustable)
- **Control Mode sensor** (enum): `pid`, `fallback`, `demand_lockout`, `safety_cap`, `dwell`, `stopped`, `resuming`, `latched`, `idle`. Attributes carry the full shutoff and freeze-guard picture: `supply_lockout_latched`, `demand_shutoff_state`, `demand_shutoff_reason`, `demand_shutoff_blocking`, `demand_shutoff_since`, `demand_shutoff_gate`, `freeze_guard_active`, `freeze_guard_source`, `freeze_guard_value`, `power_floor_effective`, `power_floor_learned`, `power_floor_unholdable_limit`, `power_floor_proven_ok`, `restart_short_runs`
- **Demand Shutoff State sensor** (enum): `disabled`, `running`, `dwell`, `stopped`, `resuming`, `suppressed`
- **Outdoor 24h Mean sensor** (°F): the centred 24 h outdoor mean used as the warm gate
- **PID diagnostic sensors**: Target, Error, Proportional, Integral, Derivative, Output, Requested Output, Demand Index, External Compensation, effective output bounds, PV slope
- **PID is always on.** The power limit is driven from a **required** external supply temperature probe. There is no manual power-limit slider and no PID on/off switch. The miner's own chip temperature is deliberately not a PID input (it is noisy and the firmware already self-manages thermals); it is only a veto on output.
- **Safety caps and lockout**: chip-temp cap, supply soft cap, latched supply lockout, demand lockout (details below)
- **Demand shutoff**: stops the miner when every thermostat is idle and heat is surplus, restarts it when a zone calls (details below)
- **Freeze guard**: blocks or reverses stops when the miner's outdoor coolant loop is at freeze risk (details below)
- **Probe-loss fallback**: if the supply probe drops out, the loop runs open-loop on an outdoor-reset curve (power_max at/below 10°F, power_min at/above 60°F, linear between) instead of dropping to power_min. Control Mode shows `fallback`.
- **Auto-recovery from mining shutoffs**: on any mining on/off transition the PID clears its integrator and throttle clock and re-seeds bumpless transfer from the current wattage.
- **Braiins Pool sensors** (optional, read-only token): account hashrate (5m/1h/24h), workers online/offline, reward today, estimated reward, balance, all-time reward, plus this miner's worker hashrate, state, last share and shares (details below)

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

## 1.8.2: soft start and predictive limiting

Every resume used to overshoot. From about 88°F the PID asked for 3.5–4.2 kW, mostly integral wound up through the 10 min boot hold. The supply climbed 2.5–3.3°F/min and peaked at 124–130°F, because the zones satisfied mid-climb and left a stagnant loop with that power still arriving. The decrease interval then kept the no-demand clamp from cutting for up to 30 min. From 1.8.2:

- **Soft start.** After any start the controller did not cause with a limit change (demand-shutoff resume, Mining Control on, lockout reset, firmware restart), and after an HA restart onto a loop more than the fine band below target, increases are stepped:
  - The limit is held through the boot hold.
  - It then steps up only while the projected supply is short of target, at most once per 12 min.
  - Each step is Kp × that shortfall, capped at half the floor-to-`Power Max` span.
  - The PID tracks the limit in force and takes over bumplessly once the supply is within the fine band of target.
  - The soft start never forces a decrease.
- **Predictive limiting.** The supply is projected 12 min ahead along its 5 min least-squares slope.
  - Rising (≥ 0.2°F/min) and projected at or past target: no increase, and a pull-back of Kp × the predicted overshoot, gated by the increase interval.
  - Projected at or past the supply cap, or past target with every zone idle (or a shutoff dwell running): the floor goes out at once, bypassing the throttle and boot hold like a cap, and the soft start re-arms.
- **Settled hold.** With both the error and the projection within half the fine band of target, fine-sized changes are not sent and the integral is held. Before, the loop spent a restart every 20–40 min on ±1°F of probe dither.
- **Anti-windup.** The integral cannot grow while a cap, the no-demand clamp, a dwell, the boot hold or a predictive limit holds the output below the PID's request.

The `pid_safety_engaged` binary sensor gains `soft_start`, `predictive` (`hold`, `pull_back`, `back_off`, `settled`), `supply_projected` and `supply_slope` attributes.

In a replay of the plant fitted to the 2026-10-08 history, a resume from 88°F with half the zone flow peaked at 112°F at Kp 111 (1500–5000 W) and 104.5°F at Kp 60 (1200–3500 W). The 1.8.1 controller peaked at 122.5°F and 115.4°F. With a pessimistic 12 min lag the peaks were 106°F vs 126.8°F and 105.7°F vs 114.3°F, and 6 h limit counts did not rise. Kp 111 with 20–30 min intervals still cycles in steady state (about ±6°F), so prefer the lower Kp.

`Power Min` below 1500 W (for example 1200 W) needs no other change. If the miner can't hold it, the learned floor rises 250 W after two short runs, as for any limit, and restarts while the supply is hot don't count. A stagnant loop at 1200 W settles near the 122°F cap rather than about 130°F at 1500 W, so idle drift reaches the S trigger later.

## 1.8.1: the safety caps win over the learned floor

On 2026-10-07 the supply sat at about 123°F, over the 122°F cap, with no zone calling. The cap forced `Power Min`, but the miner kept restarting on its own; each pair of restarts raised the learned floor, and the supply cap, the no-demand clamp and the dwell all followed the floor up to 2500 W until the 140°F lockout tripped. From 1.8.1:

- Every safety cap (supply, chip, freeze-guard hold) forces `Power Min`, never the learned floor, and floor enforcement is inert while a cap is engaged. The demand lockout and the dwell can no longer lift output above a cap's clamp.
- Restarts while the supply is hot are not counted toward the learned floor. Hot means a supply cap is engaged, the supply is within 5°F of the cap, or it was either within the last 15 min. They do not mark a limit unholdable and do not raise the "miner needs service" notification.

A learned floor survives the upgrade. If yours was raised during a hot spell, press **Reset Learned Floor**.

## Upgrading from 1.7

1.8 stops and starts mining over the miner's API v3 (port 4433, `set.miner.service stop`/`start`) on firmware from November 2024 on. On that firmware the old v2 `power_off` does not hold: btminer restarts and resumes hashing within minutes, so the supply lockout and the demand shutoff could not keep the miner off. The first stop opens the v3 write API once over port 4028 with the admin password. Set **API v3 Super Password** in Configure if the miner's `super` account is not on the default `super`. Older firmware, or any v3 failure, falls back to v2 `power_off`/`power_on`; the **Mining Status** sensor's `control_api` attribute shows which path is in use.

## Upgrading from 1.6

1.7 adds optional Braiins Pool sensors (see *Braiins Pool*). Nothing changes until a token is entered in the last Configure step.

## Upgrading from 1.5

1.6 adds the learned power floor (see *Learned power floor*), a Reset Learned Floor button and `power_floor_*` attributes on the Control Mode sensor. No config migration. Review `Power Min`: it must be a limit the miner can hold; for an M64 about 2000 W. The options flow now rejects `Power Min` ≥ `Power Max`.

## Upgrading from 1.4

The config entry is migrated to **version 4** on first load. What changes:

- `switch.<miner>_pid_mode` and `number.<miner>_power_limit` are **removed**. PID is always on; use `switch.<miner>_mining_control` as the manual override. `sensor.<miner>_power_limit` (read-only) remains.
- The options `default_power_limit`, `pid_demand_mode`, `pid_demand_floor_frac`, `pid_demand_ceiling_frac` and `pid_demand_weight_by_error` are **dropped**. Demand handling is now lockout-only (idle thermostats clamp to power_min) plus the new demand shutoff.
- New entities: `sensor.<miner>_control_mode`, `sensor.<miner>_demand_shutoff_state`, `sensor.<miner>_outdoor_24h_mean`, `binary_sensor.<miner>_demand_shutoff`, `binary_sensor.<miner>_freeze_guard`.
- **Fix dashboards and automations** that referenced the PID Mode switch or the Power Limit number. Anything that toggled PID Mode off to run a fixed wattage has no equivalent; stop the miner with Mining Control instead.
- The **supply lockout latch does not carry over** the upgrade. If the miner was latched off before upgrading, it comes up unlatched; check the supply temperature before turning Mining Control on.
- Demand shutoff ships with mode `off`, so the upgrade changes no stop/start behaviour until you enable it.

## Configuration

Initial setup asks for the connection details. Everything else is in **Configure** (options flow), grouped by step.

### Connection and basics

| Field | Default | Description |
|-------|---------|-------------|
| Host | — | Miner IP address |
| Name | Whatsminer `<ip>` | Friendly name; entity IDs are derived from its slug |
| Password | `admin` | Miner admin password |
| Port | `4028` | API port |
| API v3 Super Password | `super` | Password of the miner's `super` account, used on port 4433 to stop and start mining (Configure only) |
| Scan Interval | `30` s | Poll frequency (10–300 s) |
| Power Min | `1000` W | Lower bound of the PID output, and what every cap and lockout forces. Must be a limit the miner can actually hold: the firmware accepts any value and crash-loops below the hashboards' real minimum. For an M64 use about `2000` W (see *Learned power floor*). |
| Power Max | `5000` W | Upper bound of the PID output |
| PID Target Temperature | `167` °F | Initial setpoint (adjustable later from the number entity) |
| Kp / Ki / Kd | `111.11` / `2.78` / `55.56` | Gains, W per °F (Kp), W per °F·s (Ki), W per °F/s (Kd) |

### Safety

| Field | Default | Description |
|-------|---------|-------------|
| Chip Temp Safety Cap | `185` °F | Chip-temp average at/above this forces `Power Min`. Recoverable. |
| Supply Temp Safety Cap | `122` °F | Supply probe at/above this forces `Power Min` (soft cap). Recoverable; auto-clears below. |
| Supply Temp Lockout | `140` °F | Supply probe at/above this **stops mining and latches**. Cleared only by the Reset Supply Lockout button or Mining Control on. |

### Demand

| Field | Default | Description |
|-------|---------|-------------|
| External Temperature Sensor | — | **Required.** The supply probe the PID regulates. Any HA `sensor` with `device_class: temperature`. |
| Demand Entities | `[]` | `climate` entities whose `hvac_action` indicates heating demand. Empty disables demand lockout and demand shutoff. |
| Demand Shutoff Mode | `off` | `off`, `observe`, `active` (see Demand shutoff) |
| Shutoff Outdoor Min | `58` °F | Warm gate: centred 24 h outdoor mean at/above this arms Trigger W |
| Shutoff Hysteresis | `4` °F | Gate disarms when the mean falls below Outdoor Min minus this |
| Idle Dwell | `30` min | All-idle time required before a Trigger W stop |
| Min Off | `30` min | Minimum stop length before a demand-driven resume (a disarmed gate resumes immediately) |
| Min On | `60` min | Minimum run after a resume before another Trigger W stop (Trigger S bypasses this) |
| Supply Stop | `true` | Enable Trigger S (stop on supply ≥ soft cap while idle) |
| Supply Dwell | `10` min | Time supply must sit at/above the cap before a Trigger S stop |
| Unknown Grace | `20` min | Fail-warm: resume after this long with thermostat data unknown, if supply ≤ target |
| Cold Room Delta | `1.5` °F | A thermostat this far below its setpoint counts as calling even if it reports idle |

### Freeze guard

| Field | Default | Description |
|-------|---------|-------------|
| Freeze Guard Sensor | — | Optional temperature sensor on or near the outdoor coolant loop |
| Freeze Guard Threshold | `40` °F | At/below this, stops are blocked and an owned stop is resumed |
| Freeze Guard Forecast Hours | `12` | Forecast horizon used when falling back to the weather entity |

### Feedforward and tuning

The feedforward step holds the optional outdoor sensor, weather entity, Ke, forecast lookahead/blend and the fallback curve endpoints. The tuning step holds the actuation throttle: three-band minimum power step (`250` / `150` / `50` W at `>9°F` / `3.6–9°F` / `≤3.6°F` error), minimum adjust interval for power-down (`600` s) and power-up (`300` s), integral freeze band (`5.4` °F), setpoint ramp rate and slope EWMA. Optional price and surplus envelopes scale the output bounds.

### Braiins Pool (optional)

| Field | Default | Description |
|-------|---------|-------------|
| Braiins Pool Access Token | — | Read-only web-API token. In Braiins Pool: **Settings → Access Profiles**, pick or create a profile, enable **Allow access to web APIs**, **Generate New Token**. Leave empty to disable. |
| Pool Worker Name | — | Which pool worker is this miner: the part after the dot in the miner's stratum username (`user.worker` → `worker`) or the full `user.worker`. Not needed on a single-worker account. |

The token is checked against the pool when you save it. Every 5 minutes the integration reads the account profile and the worker list (two requests; the pool asks for no more than about one per 5 s) and publishes them as sensors on the miner's device: `Pool Hashrate 5m/1h/24h`, `Pool Workers Online/Offline`, `Pool Reward Today`, `Pool Estimated Reward`, `Pool Balance`, `Pool All-Time Reward`, and for this miner `Pool Worker Hashrate 5m/24h`, `Pool Worker State` (`ok`/`low`/`off`/`dis`), `Pool Worker Last Share`, `Pool Worker Shares 24h`. Hashrates are normalised to TH/s and money to BTC. Nothing is ever written to the pool, and a pool or internet outage only makes these sensors unavailable; the heating controller does not depend on them. If the worker name matches nothing, the worker sensors are unavailable and the log lists the account's workers.

## Requirements

- `passlib >= 1.7.4`
- `pycryptodome >= 3.20.0`

These are installed automatically by Home Assistant.

## How the loop controls the miner

### Order of authority

Each poll, the integration decides the power limit in this order; the first that applies wins and `sensor.<miner>_control_mode` reports it:

1. **Supply lockout latched** (`latched`): mining is stopped and nothing is sent until the latch is reset.
2. **Demand shutoff owns a stop** (`stopped` / `resuming`): no power-limit commands; the miner is off or booting.
3. **Safety caps** (`safety_cap`): chip temp ≥ 185°F or supply ≥ 122°F forces `Power Min` on the next tick, bypassing the time throttle. A learned floor never raises it.
4. **Demand lockout** (`demand_lockout` / `dwell`): every configured thermostat idle clamps to the effective floor (`Power Min`, or the learned floor), but never above an engaged cap's `Power Min`. With no zone calling, the zone pumps are off and the primary loop is stagnant, so there is no flow to dissipate power into. `dwell` means a shutoff dwell is also counting down.
5. **Probe lost** (`fallback`): open-loop outdoor-reset curve.
6. **Closed loop** (`pid`), shaped by the soft start and predictive limiting (see *Soft start and predictive limiting*). A predictive back-off to the floor bypasses the time throttle and the boot hold like a cap.

`idle` means the miner is not mining and the integration did not stop it (Mining Control off, firmware shutoff, power loss).

### Safety caps and the supply lockout

The chip-temp cap guards the miner; the supply caps guard the plant. The supply probe is upstream of the boiler's own high-limit, so these fire well before the boiler trips.

- **Soft cap, 122°F**: forces `Power Min`. Recoverable; the loop returns to normal when supply drops below the cap.
- **Hard cap, 140°F**: stops mining and **latches**. Crossing it means the soft cap could not hold, which with a stagnant loop can happen even at power_min. Nothing is restarted automatically; press **Reset Supply Lockout** or turn **Mining Control** on after reviewing why it tripped. The latch survives HA restarts (but not the 1.4 → 1.5 upgrade).

`binary_sensor.<miner>_pid_safety_engaged` is on whenever a cap, lockout or demand lockout is clamping output, and stays on through a demand shutoff dwell and stop so it does not toggle at the stop boundary. Use `binary_sensor.<miner>_demand_shutoff` and the `supply_lockout_latched` attribute to tell the cases apart.

### Learned power floor

The firmware treats `adjust_power_limit` as a ceiling and accepts any value. Below the hashboards' real minimum it cannot find a frequency solution and simply restarts btminer, forever (an M64 at 1000 W cycles every 2.5–4 min and never hashes). Before 1.6 every such restart re-armed the 10 min boot hold, so the controller could never raise the limit again: a supply-cap clamp to an unholdable `Power Min` left the house cold until someone intervened.

The controller now watches for restarts it did not command (an `Elapsed` regression confirmed by the next poll). Two in a row from runs shorter than 15 min at about the same limit prove that limit unholdable: the effective floor becomes limit + 250 W, is persisted, is sent through the boot hold, and every further crash in the same episode raises it another step. A run that hashes 15 min ends the episode. The floor replaces `Power Min` for the demand lockout, dwell, fallback curve and the PID's lower bound. Every safety cap (supply, chip-temp, and the freeze-guard hold over the supply lockout) still clamps to `Power Min`, and floor enforcement waits until the cap clears: less power into an over-temperature loop or chips beats more, and a crash loop dissipates less heat. Restarts while the supply is hot (a supply cap engaged, supply within 5°F of the cap, or either within the last 15 min) are not floor evidence, because the miner may be restarting on heat. Learning stops at `Power Min` + 1500 W; restarts above that are reported as a restart loop and left alone.

Each raise costs one commanded restart, and a 1000 → 2000 W climb costs about five crashes and 25 min, so set `Power Min` to a limit the miner is known to hold rather than relying on learning. The learned floor is shown in the Control Mode sensor's `power_floor_*` attributes and the `floor_raised` notification. **Reset Learned Floor** clears it after the hashboards have been serviced (on the M64 a crash loop at low limits goes with error codes 560–563, slot loss of balance: reseat the adapter/ribbon, re-torque the busbar). Raising `Power Min` above the learned floor also drops it.

### Soft start and predictive limiting

Each limit change restarts btminer, and the supply answers late. With zones calling it responds in 2–4 min. With zones satisfying mid-climb, the loop goes stagnant and keeps rising on heat already sent for 10–15 min. So the controller projects the supply `PREDICT_HORIZON_S` (12 min) ahead along its least-squares slope over the last 5 min. A slope below 0.2°F/min counts as flat, and a jump of more than 5°F between samples restarts the window.

- **No increase / pull-back**: rising and projected at or past target. The limit is not raised. It is cut by Kp × the predicted overshoot, through the normal step gates on the (shorter) increase interval.
- **Back-off**: rising and projected at or past the supply cap, or at or past target with every zone idle or a shutoff dwell running. The floor goes out on the next tick, and the soft start re-arms.
- **Settled**: error and projection both within half the fine band. Fine-sized changes are held, and so is the integral.
- **Soft start**: armed by any start not caused by our own limit change, by Mining Control on, by a back-off, and by an HA restart onto a cold loop.
  - It holds through the boot hold, then steps up only while the projection is short of target.
  - Steps are at most one per 12 min (or per increase interval, if longer). Each is Kp × the shortfall, capped at half the floor-to-`Power Max` span.
  - It ends within the fine band of target.
  - While it runs, the PID integral tracks the limit in force, so a resume can't wind it up and the hand-back is bumpless.

Safety caps, floor enforcement, the no-demand clamp and the dwell all keep their authority over these rules.

### Mining Control (manual override)

`switch.<miner>_mining_control` is the human's emergency switch and always wins over the automation:

- **Off**: stops mining. The integration clears any stop it owned and will not restart the miner. Control Mode shows `idle`.
- **On**: starts mining (refused while the supply lockout is latched). If a demand shutoff was in progress, it is cancelled and the shutoff state goes to `suppressed` until a thermostat next calls for heat.

## Demand shutoff

With every thermostat idle, the demand lockout already clamps the miner to `Power Min` (1 kW by default), but that still heats a stagnant primary loop. On warm days 1 kW is surplus for hours; on any day a stagnant loop can drift from the 122°F soft cap to the 140°F latch. Demand shutoff powers the miner off in both cases and powers it back on when a zone calls. The miner is the primary heat for the monitored zones, so every ambiguous case biases toward heating (fail-warm).

### Triggers

Both require every configured thermostat to be idle (`hvac_action` idle/off, or `hvac_mode` off, and not within Cold Room Delta of its setpoint).

- **Trigger W (warm gate)**: the centred 24 h outdoor mean is at/above **Shutoff Outdoor Min** (58°F). Stop after **Idle Dwell** (30 min), subject to **Min On** (60 min since the last resume). The gate disarms when the mean falls below 54°F (4°F hysteresis) and re-arms at 58°F.
- **Trigger S (supply overheat)**: supply at/above the soft cap (122°F) for **Supply Dwell** (10 min), at any outdoor temperature. Bypasses Min On. Also allowed when thermostat data is unknown (but not when any is calling), because a loop climbing past the cap at power_min is itself evidence of no flow.

The gate signal is a centred 24 h mean: the trailing 12 h of hourly outdoor samples plus the next 12 h of the hourly forecast from the configured weather entity, published as `sensor.<miner>_outdoor_24h_mean`. Without a weather entity it needs 12 h of trailing samples; without enough data the gate disarms (no Trigger W stops) while Trigger S keeps working.

### Resume

From `stopped`, the miner is powered on when:

- a thermostat has been calling for 2 consecutive polls and either **Min Off** (30 min) has elapsed or the gate is disarmed (cold); or
- fail-warm: thermostat data has been unknown for **Unknown Grace** (20 min) and supply is at or below the PID target.

After `power_on` the state is `resuming` until a fresh mining reading arrives; power-limit commands are held for 10 min so the boot is not interrupted by a second restart. If hashing has not resumed after 10 min the command is retried every 10 min, with a persistent notification after 3 failures. A stop longer than 24 h also raises a notification.

If the miner starts on its own while a stop is owned (power-outage auto-start, web UI), the stop is reasserted once; if it is still hashing 3 min later the integration adopts it, sets `suppressed` and logs a warning. Freeze guard (below) overrides all of this.

### How 58°F was derived

From about 40 shoulder-season days (April–May 2026) of HA statistics and about 115 h of 15–60 s captures, with outdoor temperatures from Open-Meteo reanalysis (there was no outdoor sensor at the time):

- 1000 W was never enough on any day whose 24 h mean was below about 59°F, and met or exceeded demand from 60–62°F at the spring setpoints of 73–75°F.
- Hourly capture bins showed the loop loaded 11/23 h at 55–59°F but only 2/15 h at 60–64°F (balance point about 57°F).
- Physics for a 1600 sq ft slab house puts the "1 kW exceeds load" crossover at 52–62°F, centred 55–60°F.
- This season's setpoints are 3–5°F lower than the spring data, which shifts every derived balance point down.

58°F sits at the bottom of the measured "1 kW is sufficient" band after that shift, and in the middle of the physics crossover. Because an idle thermostat already confirms surplus heat at that moment, the gate only has to filter false idles. The 24 h mean is used rather than the instantaneous reading because an afternoon reading of 60°F corresponds to a daily mean of only 52–55°F. Recalibrate after a few weeks of observe/active data: use the lowest 24 h mean at which all thermostats stayed idle for 2 h or more, plus 2°F.

### Rollout: observe, then active

1. Set Demand Entities to every zone thermostat and confirm the weather entity is set.
2. Set mode to **`observe`** for 5–7 days. The full state machine runs and publishes `dwell`/`stopped`/`resuming` in `sensor.<miner>_demand_shutoff_state` and the Control Mode attributes, but no `power_off`/`power_on` is sent. Check that stops line up with all-idle stretches and with the probe climbing at 1 kW, that there are only a few cycles per day, and that `demand_shutoff_blocking` never shows something unexpected.
3. Set mode to **`active`**. Check daily for 140°F latch trips (should be none), supply staying below about 122°F during idle, rooms within about 1°F of setpoint, resumes reaching hashing within 10 min, and no failed-resume notifications.
4. After 2–4 weeks, recalibrate Shutoff Outdoor Min from the data.

`scripts/pid-capture.py` captures the thermostats, weather entity and shutoff entities; `scripts/pid-analyze.py` reports stops per day, off durations with the outdoor mean at each stop, fraction of the window stopped, and minutes with supply ≥ 122°F while running/dwell.

### Rolling back

Set Demand Shutoff Mode to **`off`**. If the integration owns a stop at that moment, the miner is powered back on (unless the supply lockout is latched, in which case only ownership is cleared). Lockout behaviour (idle → power_min) is unchanged by the mode.

## Freeze guard

The M64's own coolant loop runs outdoors and can freeze while the miner is stopped. Freeze guard blocks every stop trigger, Trigger W, Trigger S and the 140°F supply lockout latch, and immediately resumes any stop the integration owns while the freeze source reads at/below **Freeze Guard Threshold**. `binary_sensor.<miner>_freeze_guard` is on while this is in force; the Control Mode attributes report `freeze_guard_source` and `freeze_guard_value`. It does not override Mining Control: a stop the user made is left alone.

Source priority:

1. **Freeze Guard Sensor** if configured (a probe on the loop or at the coldest exposed point is best).
2. Otherwise **min(current outdoor temperature, forecast minimum over Freeze Guard Forecast Hours)** from the configured outdoor sensor and weather entity.
3. With no source at all, stops are blocked unless the 24 h outdoor mean is at least 15°F above the freeze threshold (55°F at the defaults; a warm day is itself evidence there is no freeze risk). One exception: a supply-overheat stop is still allowed when the supply is within 5°F of the 140°F latch, because a stop the integration owns resumes on demand while the latch would hold the miner off until you reset it. While stopped with no freeze source under a cold gate, the miner is resumed as soon as the supply is below the soft cap.

The condition releases only once the source reads 3°F above the threshold, so a stop/resume cannot flap around the line.

The **40°F default assumes plain water coolant** (freezes at 32°F) plus margin for a probe that reads warmer than the coldest exposed fitting and for radiative cooling below air temperature on clear nights. **Lower it if the loop holds glycol**, according to the mix's freeze point and the same margin.

## Charts & PID tuning

The integration exposes diagnostic sensors for the PID internals so you can drop a `history-graph` card into any dashboard and watch the loop. No HACS frontend dependency required.

**Entity IDs**: Home Assistant derives entity IDs from the miner's Name. Replace `<miner>` below with the slugified version (e.g. "Heatcore" → `heatcore`). If unsure, check **Developer Tools → States** and search `pid`.

### Chart A — Tracking

Target vs supply temperature vs error. If the PID is doing its job, the supply probe should hover around Target and Error should sit near zero. Use your external probe entity for the supply line; `sensor.<miner>_temperature` is the chip temperature and is not the controlled variable.

```yaml
type: history-graph
hours_to_show: 6
title: Miner — PID Tracking
entities:
  - entity: sensor.<miner>_pid_target_temperature
    name: Target
  - entity: sensor.<your_supply_probe>
    name: Supply
  - entity: sensor.<miner>_pid_error
    name: Error
```

### Chart B — PID term breakdown

P, I, and D contributions (watts). Reveals which term is doing the work.

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
  - entity: sensor.<miner>_pid_requested_output
    name: PID Requested
  - entity: sensor.<miner>_pid_output
    name: PID Command
  - entity: sensor.<miner>_power_limit
    name: Miner Limit
  - entity: sensor.<miner>_power_consumption
    name: Actual Draw
```

### Chart D — Control mode and shutoff

Who is in charge, and whether the plant is being protected.

```yaml
type: history-graph
hours_to_show: 24
title: Miner — Control Mode
entities:
  - entity: sensor.<miner>_control_mode
  - entity: sensor.<miner>_demand_shutoff_state
  - entity: binary_sensor.<miner>_demand_shutoff
  - entity: binary_sensor.<miner>_pid_safety_engaged
  - entity: binary_sensor.<miner>_freeze_guard
  - entity: sensor.<miner>_outdoor_24h_mean
```

### Chart E — Efficiency

Hashrate vs power vs efficiency (J/TH).

```yaml
type: history-graph
hours_to_show: 24
title: Miner — Efficiency
entities:
  - entity: sensor.<miner>_hashrate
  - entity: sensor.<miner>_power_consumption
  - entity: sensor.<miner>_efficiency
```

### The external sensor (required)

The PID regulates power off an external HA temperature sensor; the miner's chip temperature is deliberately ignored by the loop. Pick the sensor that reflects what you are heating: the supply-side probe of the heat exchanger loop is the usual choice. Celsius sensors are converted to °F.

If the probe becomes unavailable the loop does not stop: it switches to the outdoor-reset fallback curve (Control Mode `fallback`) and returns to closed loop when the probe comes back. Trigger S is inactive while the probe is missing because it needs the reading; Trigger W still works.

### Tuning recipe

Defaults (Kp=111.11 W/°F, Ki=2.78, Kd=55.56, target 167°F) are a conservative starting point for a ~3 kW miner. To tune:

1. **Start with Ki=0, Kd=0** in the integration's options.
2. **Raise Kp** until Chart A shows visible oscillation around the target.
3. **Halve Kp** to damp the oscillation.
4. **Add small Ki** to eliminate steady-state error. Watch Chart B; if the integral term runs away, Ki is too high.
5. **Add small Kd** to reduce overshoot. If Chart B shows noisy D, your scan interval is probably too short.

Give each change at least 10–15 minutes to settle before judging; hydronic response is slow. Capture a run with `scripts/pid-capture.py` and report on it with `scripts/pid-analyze.py` (pass two captures for a side-by-side comparison).

### Actuation throttle (why the Power Limit chart looks "stepped")

Every `adjust_power_limit` call restarts the miner's mining process. To avoid thrashing, the PID only sends a command when the new value differs from the last commanded value by at least the minimum step for the current error band (250 / 150 / 50 W), **and** at least the minimum interval has passed since the last command (600 s for power-down, 300 s for power-up). Between those moments the PID math keeps running and `sensor.<miner>_pid_requested_output` keeps updating; only the actuator write is suppressed. Compare `pid_output` (last actuated) with `pid_requested_output` (what the PID wants) on Chart C to see the throttle working.

Safety-cap commands and a predictive back-off bypass the time throttle and go out on the next tick. Power-limit writes are also held for 10 min after a demand-shutoff resume so the boot is not interrupted, and the soft start then spaces increases at least 12 min apart. Near target the settled hold skips fine-sized changes altogether.

Tighten the step and interval for a responsive thermal target; loosen them for a large thermal mass. Set the minimum adjust interval to `0` to disable the time throttle and revert to magnitude-only.
