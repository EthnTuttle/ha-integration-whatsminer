# Changelog

## 2.0.0

The restart-era machinery comes out. 1.10–1.14 layered on daily plans, trim
bands, floor learning and autotune to avoid `adjust_power_limit` restarts;
since v3 `set.miner.power` has proven restart-free, those layers only fought
the loop (live: 1684 W commanded into a miner derated at 1224 W, forever).
The controller is now one control law: PID on the supply probe, clamped
between `floor(now)` and `ceiling(now)`, commanded via `set.miner.power`.

### Removed

- Control strategy option (`pid` / `daily` / `daily_observe` / `daily_trim`)
  and the daily plan
- Autotune: module usage, the `PID Autotune Mode` option, the Autotune State
  sensor, the Reset Autotune button
- Floor learning: episodes, the hardware-fault flag, proven_ok, and the
  Reset Learned Floor button
- The Power Regulation option: v3 `set.miner.power` is the only power path;
  `adjust_power_limit` survives only for ceiling writes and boot-window
  safety clamps, and the coordinator always polls `up_freq_finish`
- The proven-ceiling toggle

### Added

- Fault bounds: the effective floor comes from crash evidence (last
  uncommanded short run +250 W, fresh for 7 days) and the effective ceiling
  from fault evidence (demonstrated power +250 W, fresh for 7 days), so the
  caps converge onto what the hardware actually holds
- Reset Fault Memory button (`..._reset_fault_memory`) to forget
  crash/fault/derate evidence after the miner is serviced
- Config-entry migration to version 5: the removed option keys are stripped
  from data and options, and the orphaned Autotune State, Reset Learned
  Floor and Reset Autotune entities are pruned from the registry

### Changed

- Control Mode sensor attributes: the `power_floor_*`, `daily_plan_*`,
  `control_strategy` and `pid_gains_source` attributes are gone;
  `power_floor_effective` / `power_ceiling_effective` plus
  `crash_floor_w`/`crash_at`/`fault_at`/`fault_power_w`/`fault_reason`/
  `proven_max_w`/`proven_max_at` now report the fault bounds
- Expected Hashrate shows unknown when the summary carries neither
  `Target MHS` nor `Factory GHS`, instead of rendering `-0.0`

### Kept (unchanged protection)

PID, soft start, predictive back-off, setpoint ramp, Ke feedforward,
price/surplus envelope, demand shutoff, freeze guard, fallback curve,
error-code visibility + notifications, external-derate flag, the supply
lockout and Reset Supply Lockout button, Braiins pool sensors and the
firmware-update service.
