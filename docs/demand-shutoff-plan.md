# Demand shutoff for the Whatsminer integration: implementation plan

This plan is read-only. No files, HA state or config were changed. It starts from Proposal 1 (minimal, ownership flag, pure `decide()`) and adds the supply-overheat stop path, observe mode and strict snapshot from Proposal 2. It fixes every fatal flaw the critics raised. I re-checked the disputed code myself (switch.py:471-497, 653-754, 1104-1155, 1436-1458; `__init__.py:82-95`).

---

## 1. Evidence summary and the derived threshold

**The data we have**
- **No winter data.** There is nothing from Jan-Mar 2026.
- **HA long-term statistics.** About 40 usable shoulder days (2026-04-18 to 05-27): miner power/limit, Venstar daily and hourly runtime counters for den and great_room, and the boiler-loop-hx probe (`sensor.scout_21_temperature_probe`).
- **Repo captures.** 11 files covering about 115 h (04-21 to 05-03): power, limit and probe at 15-60 s resolution. They contain no thermostat or option data.
- **No measured outdoor temperature anywhere.** All outdoor numbers come from Open-Meteo reanalysis at 39.11,-78.13.
- **New bedroom thermostats.** Only 7 days, and those runtimes are inflated because the miner is offline.

**What the data showed**

| Source | Finding |
|---|---|
| HA stats (24 h means) | 1000 W was never enough on any day with a mean below about 59°F. It met or exceeded need from a mean of about 60-62°F. Great-room heating was about 0 on every night with a low of 61°F or more. Balance point about 62°F, measured at the spring setpoints of 73-75°F. |
| HA stats (cold anchor) | 04-21: mean 42.8°F, 3191 W average, both thermostats calling about 100%. That implies a heat loss of at least ~110 W/°F. |
| Captures (hourly bins) | Loop loaded in 13/13 h at 35-44°F, 12/14 at 45-49°F, 11/23 at 55-59°F, but only 2/15 at 60-64°F. Balance point about 57°F. |
| Captures (no-load) | At about 1 kW with no load, the loop settled at **117-133°F**, against a 98-104°F target and between the 122°F soft cap and the 140°F latch. Real 150°F+ trips happened at 59-73°F outdoor after hours at power_min. |
| Captures (after a stop) | The loop cooled from 152°F to about 85°F in about 30 min, then to about 72°F over about 3 h. Reheating to about 88°F took 30-60 min at 3-4 kW. |
| Physics | 1600 sq ft, UA about 470-784 BTU/h·°F, internal gains about 1.5-4.5 kBTU/h. The point where 1 kW exceeds the load is about **52-62°F, centred around 55-60°F**. Balance point 58-66°F. 5 kW stops being enough below about 28-44°F. Slab time constant about 5 h; it stores about 80 kBTU when 5°F above room. |

**Caveats that shape the defaults**
- **Some of the evidence is circular.** "1 kW was enough when idle" partly reflects the demand lockout forcing 1 kW. The 05-27 to 06-05 stretch may simply be thermostats switched off.
- **The lockout claim is a hypothesis.** Because the captures never recorded the demand config, "a shutoff would have prevented the 150°F trips" is unproven.
- **This season's setpoints are lower** (70/62/70/70°F vs 73-75°F in spring). Every derived balance point is therefore probably 3-5°F too high.
- **The probe mapping is unresolved.** boiler-loop-hx read 143-156°F without the latch apparently tripping. Which probe feeds the PID and the cap is unknown.

**The key reframe (from switch.py:1440-1443 and 757)**
The code itself says that when no thermostat calls, "the loop pump is likely idle and we have no flow to dissipate power into." So with everything idle, power_min heats a stagnant primary loop at *any* outdoor temperature. The cap docstring agrees: "a stagnant loop can trip the boiler's own high-limit even at power_min." That means two separate stop triggers are justified:
- **Trigger W (warm gate).** When outdoor is warm, idle periods last hours to days and 1 kW is surplus. Stop after a short dwell.
- **Trigger S (supply overheat).** At any outdoor temperature: all idle, supply at or above the 122°F soft cap, held for 10 min. Today's soft cap can only force power_min, which the miner is already at. Without this trigger, the loop keeps drifting toward the 140°F latch, which needs a manual reset.

**Gate direction:** stop when it is **warm**. Below the gate, keep today's idle-at-power_min behaviour; Trigger S still protects the loop. The data never showed 1 kW to be excessive in the 30s-40s, so there is no cold-side demand stop.

**Gate signal:** a centred 24-hour mean, built from the trailing 12 h of hourly outdoor samples plus the next 12 h of the hourly forecast (`weather.get_forecasts`, already used at switch.py:1037). This matches how the thresholds were derived (24 h means). An instantaneous 60°F afternoon reading corresponds to a daily mean of only about 52-55°F, which is why the instantaneous reading is not used.

**Default:** stop allowed when the 24 h mean is **≥ 58°F**. Disarm when it falls **below 54°F** (4°F hysteresis; a 24 h mean moves slowly). 58°F sits at the bottom of the measured "1 kW is sufficient" band (60-62°F at the higher spring setpoints, shifted down for this season's lower setpoints). It also sits in the middle of the physics crossover band. An idle thermostat already confirms surplus heat at that moment, so the gate only has to filter false idles. Recalibrate this from October observe-mode data.

---

## 2. Behaviour spec

**Pure module `demand_shutoff.py` (no HA imports)**
`decide(cfg, st, snap, gate_t, supply, soft_cap, is_mining, fresh, latched, now)` returns `(action ∈ {none, stop, resume, reassert, adopt, release}, reason, blocking, new_state)`.

**Thermostat snapshot: `_demand_snapshot()`**
This is a new, strict helper. `_demand_index` is left untouched because its partial averaging is fail-cold: one live idle thermostat reads 0.0 even when the others are offline. Each configured entity is classified as:

| Class | Rule |
|---|---|
| CALLING | `hvac_action == "heating"`, **or** hvac_mode is heat and `current_temperature ≤ setpoint − cold_room_delta` (1.5°F). The second case guards against a thermostat stuck reporting idle. |
| IDLE | `hvac_action` is idle or off, or hvac_mode is off. |
| UNKNOWN | Missing, unavailable or unknown, `hvac_action` is None, or `last_reported` is more than 30 min old. |

These combine into a summary:
- **CALLING** if any entity is calling.
- **ALL_IDLE** if every entity is idle and at least one is configured.
- **UNKNOWN** otherwise.

**Preconditions**
- **Feature active** when `mode != off`, PID Mode is enabled, and `demand_entities` is non-empty.
- **When the feature is inactive** and we own a stop: RELEASE, meaning `power_on` and clear ownership. The exception is a latched lockout: then only clear ownership.
- **Stale data (`coordinator.last_update_success == False`)** skips the tick for stop, reassert and adopt. It never triggers RELEASE. This fixes Proposal 1's fatal flaw.
- **RESUME may run on stale data**, because it depends on thermostats, not miner telemetry. This matters if the M64's `summary` command fails while powered off.

**States** (persisted in a `helpers.storage.Store`, written immediately on every transition)

1. **RUNNING → DWELL** when ALL_IDLE and either:
   - Trigger W: the gate is armed (24 h mean ≥ 58°F, staying armed until it drops below 54°F); or
   - Trigger S: supply ≥ soft cap (122°F) and S is enabled. Trigger S is also allowed when the snapshot is UNKNOWN (but not CALLING), because a loop climbing past the cap at power_min is itself evidence of no flow.

   The existing lockout clamp to power_min (switch.py:1444) keeps running during the dwell.
2. **DWELL → RUNNING** when the condition breaks: CALLING, UNKNOWN on the W path, the gate disarms, or supply drops below the cap minus 5°F on the S path.
3. **DWELL → STOPPED** when all of these hold:
   - the dwell has elapsed (W: 30 min; S: 10 min);
   - min_on (60 min since our last resume) has passed; Trigger S bypasses this;
   - data is fresh and `is_mining` is true.

   Action: write ownership to the Store first, then call `api.power_off()`. If the call fails, roll back to DWELL and retry after 180 s.
4. **STOPPED → RESUMING** when either:
   - CALLING has held for 2 consecutive polls and either min_off (30 min) has elapsed or the gate is disarmed (cold); or
   - fail-warm: UNKNOWN has lasted longer than `unknown_grace` (20 min) **and** supply is at or below the PID target, so resuming can't push a hot stagnant loop.

   Action: `api.power_on()`, then stamp `_resume_hold_until = now + 600 s`.
5. **RESUMING → RUNNING** on a fresh `is_mining` true reading. If that hasn't happened after 10 min, retry `power_on` every 10 min. After 3 failures, raise a `persistent_notification`. Keep retrying; never give up silently.
6. **STOPPED with fresh `is_mining` true for 3 consecutive polls, more than 5 min after the stop** (power-outage auto-start, web UI, a stray limit command):
   - First occurrence: reassert `power_off` once.
   - If it is still mining 180 s later: adopt it. Clear ownership, set `suppressed`, and log a WARNING.
7. **Supply latch.**
   - The latch branch (switch.py:707-711) clears ownership before returning, which also covers a latch restored at startup.
   - We never call `power_on` while latched; there is an explicit guard in the send helper as well.
   - Reset Supply Lockout returns the shutoff state to RUNNING.
8. **User actions.** These go through the PID switch under `_step_lock`, which fixes the race between user handlers and the tick's awaits.
   - **Mining Control ON** while STOPPED or DWELL: clear ownership and set `suppressed` until the next CALLING.
   - **Mining Control OFF**: clear ownership. A user's off is never auto-resumed.
   - **PID Mode OFF** while STOPPED: clear ownership and **leave the miner off**. Skip the default-limit revert at line 639, because it would restart the miner at about 5 kW in warm, no-demand conditions. Log "miner left off; use Mining Control".
9. **Resume boot hold.** The off edge zeroes `_last_command_time` (switch.py:669). Without a hold, the first PID tick after a resume would send `adjust_power_limit` mid-boot, which is a second restart. The actuation gate (switch.py:~1487) therefore skips `set_power_limit` while `now < _resume_hold_until`. The safety-cap path is exempt.
10. **Observe mode.** The full state machine runs, but no `power_off` or `power_on` is sent. `would_stop` and `would_resume` are published.
11. **Max-off notice.** If STOPPED for more than 24 h, raise a notification. No automatic action.
12. **Safety Engaged.** It stays true through DWELL and STOPPED, matching the existing no-demand clamp, so it doesn't toggle at the stop boundary. A separate binary sensor distinguishes a demand stop from a latch or cap.

**Hard-coded constants:** resume debounce 2 polls; boot hold 600 s; reassert interval 180 s (`LOCKOUT_REASSERT_INTERVAL`); resume verify 10 min with 3 retries; thermostat staleness 30 min; max-off notice 24 h. There is no daily stop cap; the critics showed that hitting a cap forces the stagnant-loop condition.

---

## 3. Config options and observability

All options go in the options flow, step `demand`. Only keys are added, so no VERSION bump is needed.

| Key (`CONF_PID_DEMAND_SHUTOFF_*`) | Default | Range |
|---|---|---|
| `MODE` | `off` | `vol.In(off, observe, active)` |
| `OUTDOOR_MIN` (°F, 24 h centred mean) | 58.0 | −40 to 100 (−40 means always armed) |
| `HYSTERESIS` (°F) | 4.0 | 0-15 |
| `IDLE_DWELL_MIN` | 30 | 5-240 |
| `MIN_OFF_MIN` | 30 | 0-240 |
| `MIN_ON_MIN` | 60 | 0-240 |
| `SUPPLY_STOP` (bool) | True | — |
| `SUPPLY_DWELL_MIN` | 10 | 1-60 |
| `UNKNOWN_GRACE_MIN` | 20 | 0-120 |
| `COLD_ROOM_DELTA` (°F) | 1.5 | 0-5 (0 disables) |

The feature reuses the existing `CONF_PID_DEMAND_ENTITIES` and outdoor sensor/weather options. There are no new entity pickers.

**Entities**
- `binary_sensor.heatcore_pid_demand_shutoff`: on while the integration owns a stop.
- `sensor.heatcore_demand_shutoff_state` (enum): running, dwell, stopped, resuming, suppressed, observe, disabled.
- `sensor.heatcore_demand_shutoff_outdoor_mean` (°F).

**PID Mode switch attributes**
- `demand_shutoff_state`, `_active`, `_reason`
- `_blocking`, e.g. "climate.den heating", "outdoor mean 55.1 < 58", "min_on 23 min left", "climate.back_bedroom unknown"
- `_since`, `_gate` (armed, disarmed or unknown), `_would` (observe mode)
- `control_mode` gains the value `shutoff`

PID Safety Engaged also gets a `demand_shutoff_active` attribute.

**Logs:**
- INFO on every transition and on gate arm/disarm.
- WARNING on stops, fail-warm resumes, reasserts and adoptions.
- ERROR on command failures.

---

## 4. Code changes by file

| File:line | Change |
|---|---|
| `const.py:114-124` | Add the CONF_ and DEFAULT_ keys above plus `DEMAND_SHUTOFF_MODES`. Add a comment explaining the derivation and the stagnant-loop reason. |
| **new** `demand_shutoff.py` | `ShutoffConfig`, `ShutoffState` (state, owned, since, last_resume_at, dwell_start, call_polls, unknown_since, suppressed, reasserted, resume_attempts, gate_armed, hourly outdoor ring buffer), `DemandSnapshot`, `decide()`, and `to_dict`/`from_dict`. |
| `switch.py:109-209` / ctor `336-470` | Wire the options. Build the config. Create `Store(hass, 1, f"whatsminer.{entry_id}.demand_shutoff")`. Initialise `_resume_hold_until = 0`. |
| `switch.py:471-497` `async_added_to_hass` | Load the Store **before** the `STATE_ON` branch at line 484, which can return early at 486-492. A restored DWELL becomes RUNNING. Discard outdoor samples older than 12 h. |
| `switch.py:516-527` | Add the attributes above and `control_mode="shutoff"`. |
| `switch.py:529-554` `async_reset_lockout` | Reset the shutoff state to RUNNING and save. |
| `switch.py:614-650` PID `async_turn_off` | Take `_step_lock`. If we own a stop, clear ownership, save, and skip the `set_power_limit(default)` at line 639. |
| `switch.py:653-679` | No change to the edge logic. On the on edge in RESUMING, mark the resume confirmed. |
| `switch.py:699-725` `_control_step` | In the latch branch (707), clear ownership. Between 715 and 718, insert `if await self._demand_shutoff_step(temp, is_mining): return`. That method: (a) updates the hourly outdoor sample; (b) computes the centred mean, re-using the forecast fetch with a 30-min cache; (c) builds the snapshot; (d) calls `decide()`; (e) dispatches to `_send_shutoff_power_off`/`_on`, modelled on 747-754 (never raise, null `_last_commanded_power`, stamp time, save the Store, re-check ownership after every await). Update the docstring's order of authority. |
| `switch.py:~943-1060` | New `_outdoor_gate_mean(now)`. It factors the `get_forecasts` call out of `_blended_outdoor_temp` into a shared, cached helper. With a weather entity it averages the next 12 hourly forecasts plus the trailing 12 h of samples. Otherwise it needs at least 12 h of trailing samples. Without enough data it returns None, which disarms the gate; Trigger S still works. |
| `switch.py:1104` | Add `_demand_snapshot()` next to `_demand_index`, which stays unchanged. |
| `switch.py:1444-1458` | Keep the clamp. Append the shutoff state to the "No demand" log ("stop in N min" or "blocked: …"). |
| `switch.py:~1487-1553` actuation gate | Skip non-safety `set_power_limit` while `now < _resume_hold_until`. |
| `switch.py:286-327` `WhatsminerMiningSwitch` | ON (after the latch guard at 288) and OFF call `pid_switch.async_user_mining_override(on: bool)` via `hass.data[...]["pid_switch"]` (set at 211). That method runs under `_step_lock` before the API call. |
| `__init__.py:82-95` | Add `"demand_shutoff": {"state": "disabled", "active": False, "reason": None}` to `pid_state`. |
| `binary_sensor.py:115-123` | New PID Demand Shutoff binary sensor. Add the `demand_shutoff_active` attribute on Safety Engaged. |
| `sensor.py:148` | Add the state enum and outdoor-mean diagnostic sensors, following the `PID_INTERNAL_SENSORS` pattern. |
| `config_flow.py:305-351` | Add the fields to `async_step_demand` using the existing `vol.Optional(default=current)` + `Coerce`/`Range`/`In` pattern. |
| `strings.json:78` and `custom_components/whatsminer/translations/en.json` | Add identical `data` and `data_description` entries. Leave the stale root `translations/en.json` alone. |
| `scripts/pid-capture.py` | Capture the four `climate.*` entities, `weather.forecast_home`, the new entities and the switch attributes. |
| `scripts/pid-analyze.py:254-296,434` | Add stops per day, off durations, outdoor mean at each stop, comfort deficit while stopped, and time with supply ≥ 122°F while idle. |
| **new** `tests/test_demand_shutoff.py` | Plain pytest; see section 6. |
| README | Document the feature, how the defaults were derived, and how to roll back. |

(Corrected anchors: `pid_state` is at `__init__.py:82-95`, `first_refresh` at `__init__.py:64`, and the Power Limit slider guard and set call at `number.py:104/125`.)

---

## 5. Edge cases

- **One thermostat offline while the others are idle:** it counts as UNKNOWN, so no W stop. If already stopped, the miner resumes after 20 min, but only if supply ≤ target.
- **All thermostats unavailable:** never a W stop. S can still stop it, because a loop above the cap is evidence of no flow.
- **A thermostat stuck on idle:** the cold-room delta (1.5°F below setpoint) counts as CALLING. There is also the 24 h max-off notice.
- **Thermostat in hvac_mode off:** counts as idle, i.e. the user has opted that room out. If every thermostat is off, the system stops in warm weather and idles below the gate, with S protecting the loop. This is documented.
- **Den with a high setpoint calling almost continuously:** blocks stops (fail-warm). It shows up in `_blocking`.
- **Weather or forecast unavailable:** the gate disarms, so no new W stops. Resume still follows the thermostats. S is unaffected.
- **Hourly met.no steps:** absorbed by the 24 h mean and the 4°F hysteresis.
- **Probe lost (fallback mode):** W still works. S is inactive because it needs the probe. After a resume, the fallback curve runs, and its demand-0 power_min branch (switch.py:1189) still applies.
- **One failed poll during a stop:** stop, reassert and adopt are skipped. No RELEASE. Resume is still allowed.
- **M64 `summary` fails while powered off:** every poll is stale, but resume still fires from thermostat data. After an HA restart, though, the entry can't set up (`first_refresh`, `__init__.py:64`). This is the gating hardware test in rollout step 2.
- **HA crash right after a stop:** the Store is written before `power_off`, so ownership survives and the miner resumes on the next call. Proposal 1 relied on RestoreEntity attributes, which are dumped only about every 15 min, leaving a crash window.
- **Options reload:** `pid_state` is rebuilt (`__init__.py:82-95`), but the Store survives. A reload with mode set to off and ownership restored triggers RELEASE.
- **Firmware auto-start after a power outage:** reasserted once, then adopted with `suppressed` set. S still protects the loop.
- **`is_mining` blips:** decisions use our own ownership and timestamps, plus at least 3 consecutive fresh polls before adopting. A stop requires `is_mining` true, so we never take ownership of someone else's stop.
- **Double restart on resume:** prevented by the 600 s boot hold.
- **Latch while DWELL or STOPPED:** the latch wins and ownership is cleared. We never call `power_on` while latched.
- **Mining Control or PID toggled mid-tick:** serialised under `_step_lock`, and ownership is re-checked after each await.
- **Externally started miner while we own a stop:** runs without the 122°F soft cap for at most about 3 min before the reassert. The 140°F trip still applies.

---

## 6. Test plan

**Unit tests:** plain pytest on `decide()` with a fake clock and no HA.
1. W: ALL_IDLE with mean 59°F stops at 30 min but not at 29. A CALLING tick resets the dwell.
2. Hysteresis: 58 arms; 55 stays armed; 53.9 disarms; 57 after disarm stays disarmed. A None mean disarms.
3. S: ALL_IDLE with mean 40°F and supply 123°F for 10 min stops, and bypasses min_on. UNKNOWN with supply 123°F also stops. CALLING with supply 123°F does not.
4. min_on blocks a W stop at 59 min after a resume and allows it at 60.
5. Stopped, CALLING at 10 min: held until 30 min. A disarmed gate resumes at once. A single CALLING poll does not resume.
6. UNKNOWN for 20 min: resumes if supply ≤ target, holds if supply is above it.
7. The cold-room delta makes an "idle" thermostat 2°F below setpoint count as CALLING.
8. Stale data: no stop, reassert, adopt or release; resume is still allowed.
9. External start: one reassert, then adopt with `suppressed` set. A blip shorter than 3 polls is ignored.
10. Latched: never resumes, and ownership is cleared.
11. User ON sets `suppressed` until CALLING. User OFF and PID off clear ownership, with no `power_on`.
12. Observe mode emits `would_*` and never sends a command.
13. Store round-trip; a restored DWELL becomes RUNNING; mode off with owned set gives RELEASE.
14. Snapshot classification table (heating, idle, off, None, unavailable, stale).

**Static checks:** `python -m py_compile` on every module, and `diff strings.json translations/en.json`.

**Replay:** run `decide()` over the April-May HA hourly runtime counters plus Open-Meteo, and over the captures, using "probe rising while ≤ 1.5 kW" as a stand-in for idle. Report stops per day and S-path stops ahead of the three 150°F+ events, labelled as a hypothesis.

---

## 7. Rollout and validation

1. **Merge with the feature off** (mode `off`). Deploying is then a no-op. Commit only when you ask. The entry stays in `setup_error` until heatcore at 10.0.0.104:4028 is reachable; that is expected.
2. **Hardware gate, before turning the feature on:** with the miner back and attended:
   - Run Mining Control off, then on, and time it to hashing.
   - Check that `summary` and `get_miner_info` still answer while powered off. If they don't, the coordinator needs a power-off-tolerant path first, because otherwise the entry fails to set up after an HA restart.
   - Check whether `adjust_power_limit` wakes powered-off hashboards.
   - Check whether the power limit is kept across a power cycle.
3. **Run 24 h with the feature off** and confirm PID behaves exactly as before.
4. **Configure > Demand:**
   - Set demand entities to `climate.den`, `climate.great_room`, **`climate.back_bedroom`, `climate.windowed_bedroom`** (the two new T2000s).
   - Keep `demand_mode=lockout`.
   - Confirm the weather entity is `weather.forecast_home`.
   - Review the den setpoint.
5. **Mode `observe` for 5-7 days.** October daily means of about 61°F are an ideal test window. Update `pid-capture.py` first, then capture 48-72 h. Check that:
   - `would_stop` lines up with all-idle stretches and with the probe climbing at 1 kW;
   - the cycle count is a few per day at most;
   - nothing is blocked unexpectedly.
6. **Mode `active`.** Check daily that:
   - no 140°F latch trips;
   - the probe stays below about 122°F during idle;
   - rooms hold within about 1°F of setpoint;
   - resumes reach hashing within about 10 min;
   - no failed-resume notifications appear.
7. **After 2-4 weeks, recalibrate `OUTDOOR_MIN`.** Use the lowest 24 h mean at which all four thermostats stayed idle for at least 2 h, plus 2°F. A drop to about 55°F is likely given the lower setpoints. Then fit kWh/day against outdoor temperature to replace the physics UA estimate.
8. **Rollback:** set mode to `off`. That releases (powers on) any stop the integration holds.

---

## 8. Open questions for you

1. **Flow when zones are idle.** When every zone is idle, does the miner/heat-exchanger loop really stop circulating, as the code assumes? Is there a backup boiler on "main-boiler-supply" that fires on calls while the miner is off? This decides whether Trigger S is right and how much a 30-min min_off matters.
2. **Which probe** feeds the PID and the 140°F cap: `scout_21` (boiler-loop-hx) or the "supply" probe? boiler-loop-hx read up to about 156°F without the latch apparently tripping.
3. **Hydro cooling.** Is the M64 hydro-cooled with its own pump, and does it need a minimum coolant temperature or freeze protection while it is stopped?
4. **Two defaults to confirm:**
   - Stopping on supply overheat (Trigger S) at **any** outdoor temperature.
   - Turning PID Mode off during a shutoff **leaves the miner off**, rather than restarting it at the default limit.
---

## 9. Implementation notes (2026-10-06)

Implemented in v1.5.0 with these deliberate departures from the plan above:

- **PID only.** The PID Mode switch, Power Limit number, default power limit and the demand envelope mode were removed (config-entry v4 migration). The controller now lives in `controller.py`; `switch.py` keeps only Mining Control. The plan's "PID off during shutoff" question is moot.
- **Freeze guard (new).** `freeze_guard_sensor` / `freeze_guard_threshold` (40°F) / `freeze_guard_forecast_hours` (12). Freeze risk blocks W, S and the 140°F latch (which then holds power_min and raises a notification instead of latching), and force-resumes a stop we own once supply is below the soft cap. Fallback source is min(current outdoor, forecast minimum). With no source, stops are blocked unless the warm gate is armed. Release hysteresis 3°F.
- **Strict snapshot drives the lockout clamp too.** `_demand_index` is gone; the power_min clamp fires only on ALL_IDLE, so one unknown thermostat no longer forces 1 kW (fail-warm). `demand_index` is now the fraction of known thermostats calling.
- **Persistence.** The lockout latch moved from RestoreEntity attributes into the same `Store` as the shutoff state, written before every power command.
- **Timer tick.** `DataUpdateCoordinator` only notifies listeners when data or the success flag changes, so a powered-off miner that stops answering would starve the controller. A timer tick at the scan interval runs the step whenever the coordinator has gone quiet.
- **Observe mode** runs the state machine with `simulated=True` (no ownership, no commands, no reassert/adopt) and publishes `would_stop` / `would_resume`.
- **is_mining** no longer depends on `MHS 5s`, which this M64 firmware does not report.
- Entity names: `sensor.<miner>_control_mode`, `sensor.<miner>_demand_shutoff_state`, `sensor.<miner>_outdoor_24h_mean`, `binary_sensor.<miner>_demand_shutoff`, `binary_sensor.<miner>_freeze_guard`.
- **Review-driven hardening (same day).** A failed `power_off` send keeps ownership and retries (the command usually landed); an external start is reasserted three times before being adopted, and an adopted start that stops by itself within 10 min is reclaimed; a failed `power_on` on resume/release/unlatch is retried after 60 s; the unlatch and release paths own the restart until the miner is seen hashing; observe mode releases a real stop inherited from active mode; the entry sets up in a degraded state (cached MAC) when the miner doesn't answer, so a stopped miner can still be resumed after an HA restart; with no freeze source at all, stops need the 24 h mean to be at least 15°F above the freeze threshold, except an S stop with supply within 5°F of the latch (an owned stop resumes on demand, the latch does not); while stopped with no freeze source under a cold gate, the miner resumes once supply is below the soft cap.
