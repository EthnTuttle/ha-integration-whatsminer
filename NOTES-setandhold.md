# Set-and-hold, notify-only collapse, and the write gate

Synthesis-driven changes to `custom_components/whatsminer/controller.py`
(and a small telemetry addition in `sensor.py`), driven by
`/home/ethan/network/api-design-synthesis.md` and the live evidence in
`/home/ethan/network/power-test-20261012-220429.log` (adjudicated in
`wmt-re/docs/09-log-bundle-analysis.md`). No config keys were added; no
safety path lost authority. Tests: **278 passed** (baseline 274 + 4 new).

---

## Change 1 — the stale "no restart" comment is now the measured truth

`controller.py` ~line 198. The old comment claimed v3
`set.miner.power` is applied with no btminer restart. The 2026-10-12 power
test shows the honest picture: the command applies without an *immediate*
restart, but around a large step the mining process can recycle ~80 s
*after* the command (1250 W step: 1884 → 1244 W with elapsed counting,
then elapsed reset to 0 and working false ~45 s; firmware 20260908.12).
The comment now says exactly that, cites both evidence files, and draws
the operative conclusion: treat every power write as potentially
interrupting hashing briefly, and only send one while tuning has finished
(up-freq-finish 1) — which is what the write gate (Change 4) enforces.

## Change 2 — "pinned" set-and-hold mode for a collapsed band

When `power_max - power_min < PINNED_BAND_W` (50 W, the default fine
step) the PID is bypassed entirely (`_pinned_step`, dispatched at the top
of `_run_pid_step`). Inside a sub-step band the PID could only re-issue
tune-churning writes for a few watts — the synthesis verdict called this
the worst point on the tradeoff curve. Instead:

- The target is fixed at `power_min`, clamped into the fault bounds.
- The believed operating point (`_power_in_force()`: the sent power, else
  the limit in force) is reconciled toward the target through the
  existing tuned v3 path — at most once per tuned epoch (the write gate)
  and only when it differs by ≥ `PINNED_RECONCILE_W` (25 W). A fresh
  install with an unknown operating point still gets the one reconcile
  write (one command beats guessing).
- PID telemetry keeps publishing with `pid_mode="pinned"` marking the
  bypass; `sensor.py` exposes `pid_mode` on the control-mode sensor.
- Safety keeps full authority: a cap clamp below the believed point fires
  via `adjust_power_limit` with the same step rule as the PID path's
  safety fire (the fine step is the most permissive band), demand
  lockout / shutoff dwell keep their telemetry labels, and the shutoff's
  own stops are unchanged.
- No new config keys: the mode falls out of the existing
  `power_min`/`power_max` the user already set.

## Change 3 — hashrate collapse is observed, never undone

The collapse detector (`_regulation_tick`) is unchanged in *detection*:
a step between two regulated powers whose TH/W falls below
`V3_COLLAPSE_FRAC` still arms the crash floor and fault memory. What
changed is the response: instead of an `adjust_power_limit` undo write
(which restarts btminer — exactly the hazard Change 1 documents), the
controller now logs a WARNING, raises a persistent notification
(`whatsminer_power_collapse`, dismissible via Reset Fault Memory), and
publishes `power_regulation_error = "collapse-observed-no-undo"` on the
control-mode sensor. The error self-clears on the next stable read once
TH/W recovers above the detection threshold. No power write of any kind
is sent in response to the collapse.

## Change 4 — `_gate_write`: one funnel for every power/service write

`_gate_write(now, *, safety, kind) -> str | None` returns the
suppression reason or None. Non-safety writes are suppressed unless
tuning has finished (up-freq-finish 1 on a fresh, successful poll) **and**
the one-non-safety-write budget of the current tuned epoch is unspent.
Safety clamps and stop-class commands (demand-shutoff `power_off`,
resume-edge `power_on`, lockout stops) pass unconditionally — the safety
paths are exactly as capable as before the gate existed. Every
suppression is INFO-logged once per (kind, reason), repeats DEBUG.

- Routed through it: the v3 power send, ceiling raise/lower via
  `adjust_power_limit`, `_command_power` dispatch (this is also the new
  home of the old not-tuned hold — a suppressed request is held as
  `_op_target` and delivered by the re-establish path when the gate
  opens), and the four stop/start sites.
- The budget is spent only on a *successful* non-safety send
  (`_spend_epoch_write` in `_set_power_limit`/`_send_v3_power`) and is
  re-armed only on real epoch boundaries: a btminer start edge / Elapsed
  regression (`_note_btminer_restart`) or up-freq-finish leaving and
  returning to 1 (`_note_reported_limit`). Tune-grace expiry does not
  re-arm it.
- The tuning-timeout limit write is itself gated; when held, it retries
  next tick (the re-establish path delivers the wanted power once tuning
  finishes anyway), so `_ceiling_hold_until` / `_untuned_since` are only
  consumed when the write actually goes out.

This closes a load-bearing loophole: the old ceiling-raise path bypassed
the not-tuned hold, so a raise could reach a miner mid-tune and churn the
tuner.

## Change 5 — tests

Four new tests in `tests/test_power_regulation.py` (section headers
"set-and-hold (pinned)" and "the write gate"):

1. `test_a_collapsed_band_pins_the_target_and_reconciles_once` — a
   1300/1325 W band pins (`pid_mode="pinned"`), no PID actuation, the
   Power-Min target is delivered exactly once via v3 and then held.
2. `test_pinned_mode_gets_one_reconcile_write_per_tuned_epoch` — the
   second non-safety write is held until the retune the first write
   opened finishes (new epoch ⇒ budget re-arms).
3. `test_pinned_mode_keeps_the_safety_clamp_authority` — pinned *and*
   untuned: everything is gated, yet a chip cap still forces Power Min
   through `adjust_power_limit`.
4. `test_the_gate_holds_non_safety_writes_while_tuning_but_not_safety_clamps`
   — up-freq-finish 0 holds a non-safety raise (and the seed command) as
   the target; the chip-cap safety clamp still flies.

The rewritten collapse test already asserts the Change 3 contract
(no `adjust_power_limit`, no churn, notification raised, error
self-clears). Existing tests were adapted to the new contract only where
they relied on the closed loopholes: they now simulate the retune each
v3 write opens (`retune()` helper / `v3_plant(retune_s=...)` in
`test_soft_start.py`) or reset the epoch budget directly when driving
`_command_power` without ticks.

## Deliberately unchanged

- Safety paths: chip cap, supply cap, crash/fault floors — same rules,
  same actuation, exempt from the gate.
- Demand-shutoff stop/resume machinery and the lockout/freeze guard.
- Soft-start and predictive machinery in non-pinned mode.
- Collapse *detection* and fault-memory semantics (only the undo write
  is gone).
- No config schema changes, no migrations, no restart/reload required of
  a running HA instance beyond picking up the new code.

## Test run

```
$ python3 -m pytest tests/ -q
........................................................................ [ 26%]
........................................................................ [ 51%]
........................................................................ [ 77%]
........................................................................ [100%]
278 passed in 1.95s
```
