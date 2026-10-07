"""Pure demand-shutoff and freeze-guard logic. No Home Assistant imports.

The controller (controller.py) gathers readings, builds ``ShutoffInputs`` and
calls :func:`decide` once per coordinator poll. ``decide`` returns the action
to take and the next persisted state; it never performs I/O.

Fail-warm is the governing rule: the miner is the primary heat for the monitored
zones, so every
ambiguous reading blocks a stop or favours a resume.

Thermostat classes
    CALLING  hvac_action == "heating", or hvac_mode is heat and the room is
             cold_room_delta below setpoint (guards a thermostat stuck on idle)
    IDLE     hvac_action idle/off, or hvac_mode off (room opted out)
    UNKNOWN  missing, unavailable, no hvac_action, or not reported recently

Stop triggers (require the summary to be ALL_IDLE, S also accepts UNKNOWN)
    W  warm gate armed (centred 24 h outdoor mean ≥ outdoor_min, hysteresis)
    S  supply ≥ soft cap: a stagnant loop heated at power_min

Freeze guard (``ShutoffInputs.freeze``)
    True   blocks every stop and force-resumes a stop we own
    None   unknown: blocks stops unless the warm gate is armed
    False  no freeze risk
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from typing import Any

# --- thermostat classification ------------------------------------------------

CALLING = "calling"
IDLE = "idle"
UNKNOWN = "unknown"
ALL_IDLE = "all_idle"

# --- states ---------------------------------------------------------------------

RUNNING = "running"
DWELL = "dwell"
STOPPED = "stopped"
RESUMING = "resuming"

# --- actions --------------------------------------------------------------------

NONE = "none"
STOP = "stop"
RESUME = "resume"
REASSERT = "reassert"
ADOPT = "adopt"
RELEASE = "release"
UNLATCH = "unlatch"

MODE_OFF = "off"
MODE_OBSERVE = "observe"
MODE_ACTIVE = "active"

# Hard-coded tunables (seconds / counts)
RESUME_DEBOUNCE_POLLS = 2
RESUME_VERIFY_S = 600
RESUME_NOTIFY_ATTEMPTS = 3
REASSERT_INTERVAL_S = 180
REASSERT_MIN_AGE_S = 300
# Re-send power_off this many times (REASSERT_INTERVAL_S apart) before adopting
# an external start. Our own slow stop never gets adopted within this window.
REASSERT_MAX = 3
# After adopting, a fresh "not mining" within this long means it was our own
# slow stop after all: take ownership back so it gets resumed.
ADOPT_RECLAIM_S = 600
# With no freeze source at all, allow stops only when the 24 h mean is at least
# this far above the freeze threshold (so the "warm gate" really is warm).
FREEZE_UNKNOWN_MARGIN_F = 15.0
# Minimum trailing hourly samples before the centred mean is trusted.
MIN_TRAILING_SAMPLES = 6
# A RELEASE (mode off while owning a stop) or UNLATCH power_on is retried this
# often until the miner is seen hashing.
RELEASE_RETRY_S = 120
ADOPT_MINING_POLLS = 3
STOP_RETRY_S = 180
THERMOSTAT_STALE_S = 1800
MAX_OFF_NOTICE_S = 86400


def classify_thermostat(
    hvac_action: str | None,
    hvac_mode: str | None,
    current_temp: float | None,
    setpoint: float | None,
    age_s: float | None,
    cold_room_delta: float,
    available: bool = True,
) -> str:
    """Return CALLING, IDLE or UNKNOWN for one climate entity."""
    if not available:
        return UNKNOWN
    if age_s is not None and age_s > THERMOSTAT_STALE_S:
        return UNKNOWN
    if hvac_action == "heating":
        return CALLING
    if hvac_mode == "off":
        return IDLE
    if hvac_action is None:
        return UNKNOWN
    if (
        cold_room_delta > 0
        and hvac_mode in ("heat", "heat_cool", "auto")
        and current_temp is not None
        and setpoint is not None
    ):
        try:
            if float(current_temp) <= float(setpoint) - cold_room_delta:
                return CALLING
        except (TypeError, ValueError):
            pass
    if hvac_action in ("idle", "off"):
        return IDLE
    # cooling/drying/fan/preheating/defrosting: not a heat call, not idle for
    # our purposes either — treat as unknown so it blocks a stop.
    return UNKNOWN


def summarize(classes: dict[str, str]) -> str:
    """Combine per-entity classes: CALLING > UNKNOWN > ALL_IDLE."""
    if not classes:
        return UNKNOWN
    values = list(classes.values())
    if CALLING in values:
        return CALLING
    if all(v == IDLE for v in values):
        return ALL_IDLE
    return UNKNOWN


# --- freeze guard and gate helpers ----------------------------------------------


def freeze_status(
    value: float | None, threshold: float, hysteresis: float, prev_active: bool
) -> bool | None:
    """True at/below threshold; stays True until threshold + hysteresis."""
    if value is None:
        return None
    if value <= threshold:
        return True
    if prev_active and value < threshold + hysteresis:
        return True
    return False


def freeze_fallback_value(
    outdoor_now: float | None, forecast_temps: list[float] | None
) -> float | None:
    """Fallback freeze source: the colder of now and the forecast minimum."""
    candidates: list[float] = []
    if outdoor_now is not None:
        candidates.append(float(outdoor_now))
    if forecast_temps:
        candidates.append(min(float(t) for t in forecast_temps))
    return min(candidates) if candidates else None


def centred_mean(
    samples: list[tuple[float, float]],
    forecast: list[tuple[float, float]],
    now: float,
    window_s: float = 12 * 3600,
    min_points: int = 12,
    min_trailing: int = MIN_TRAILING_SAMPLES,
) -> float | None:
    """Centred 24 h mean: trailing hourly samples plus the next 12 h forecast.

    ``samples`` and ``forecast`` are (unix_ts, °F). Returns None without
    enough points, which disarms the warm gate (fail-warm).
    """
    trailing = [v for ts, v in samples if now - window_s <= ts <= now]
    points = trailing + [v for ts, v in forecast if now < ts <= now + window_s]
    if len(points) < min_points or len(trailing) < min_trailing:
        return None
    return sum(points) / len(points)


def trim_samples(
    samples: list[tuple[float, float]], now: float, keep_s: float = 12 * 3600
) -> list[tuple[float, float]]:
    return [(ts, v) for ts, v in samples if now - keep_s <= ts <= now]


# --- config / state / inputs ----------------------------------------------------


@dataclass(frozen=True)
class ShutoffConfig:
    mode: str = MODE_OFF
    has_entities: bool = False
    outdoor_min: float = 58.0
    hysteresis: float = 4.0
    idle_dwell_s: float = 30 * 60
    min_off_s: float = 30 * 60
    min_on_s: float = 60 * 60
    supply_stop: bool = True
    supply_dwell_s: float = 10 * 60
    unknown_grace_s: float = 20 * 60
    soft_cap: float = 122.0
    lockout: float = 140.0
    freeze_threshold: float = 40.0

    @property
    def active(self) -> bool:
        return self.mode in (MODE_OBSERVE, MODE_ACTIVE) and self.has_entities

    @property
    def observe(self) -> bool:
        return self.mode == MODE_OBSERVE


@dataclass
class ShutoffState:
    state: str = RUNNING
    owned: bool = False            # we powered the miner off
    simulated: bool = False        # observe mode: state machine ran, no command
    since: float | None = None     # when ``state`` was entered
    dwell_start: float | None = None
    dwell_trigger: str | None = None  # "W" or "S"
    last_resume_at: float | None = None
    last_stop_at: float | None = None
    resume_sent_at: float | None = None
    resume_attempts: int = 0
    call_polls: int = 0
    unknown_since: float | None = None
    mining_polls: int = 0
    reasserted_at: float | None = None
    reasserts: int = 0
    adopted_at: float | None = None
    stop_failed_at: float | None = None
    suppressed: bool = False       # user turned mining on over our stop
    gate_armed: bool = False
    would_stop_at: float | None = None
    would_resume_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "ShutoffState":
        if not data:
            return cls()
        known = {f for f in cls.__dataclass_fields__}
        st = cls(**{k: v for k, v in data.items() if k in known})
        # A dwell never survives a restart; it must be re-earned.
        if st.state == DWELL:
            st.state = RUNNING
            st.dwell_start = None
            st.dwell_trigger = None
        return st


@dataclass(frozen=True)
class ShutoffInputs:
    summary: str                        # CALLING / ALL_IDLE / UNKNOWN
    calling: tuple[str, ...] = ()       # entity ids currently calling
    unknown: tuple[str, ...] = ()       # entity ids unknown/stale
    gate_mean: float | None = None      # centred 24 h outdoor mean, °F
    supply: float | None = None         # supply probe, °F
    target: float | None = None         # PID target, °F
    is_mining: bool = False
    fresh: bool = True                  # coordinator.last_update_success
    latched: bool = False               # supply lockout latched
    freeze: bool | None = None          # freeze guard: True/False/None(unknown)
    user_off: bool = False              # the user turned Mining Control off recently


@dataclass(frozen=True)
class Decision:
    action: str
    reason: str
    blocking: tuple[str, ...]
    state: ShutoffState
    notify: str | None = None


# --- user actions ---------------------------------------------------------------


def user_mining_on(st: ShutoffState, now: float) -> ShutoffState:
    """Mining Control ON: drop ownership, suppress stops until a thermostat calls."""
    st = replace(st)
    st.suppressed = True
    st.owned = False
    st.state = RUNNING
    st.since = now
    st.dwell_start = None
    st.dwell_trigger = None
    st.simulated = False
    return st


def user_mining_off(st: ShutoffState, now: float) -> ShutoffState:
    """Mining Control OFF: the user's stop is never auto-resumed."""
    st = replace(st)
    st.owned = False
    st.state = RUNNING
    st.since = now
    st.dwell_start = None
    st.dwell_trigger = None
    st.simulated = False
    return st


def lockout_reset(st: ShutoffState, now: float) -> ShutoffState:
    st = replace(st)
    st.owned = False
    st.state = RUNNING
    st.since = now
    st.dwell_start = None
    st.dwell_trigger = None
    st.simulated = False
    return st


def mark_stop_failed(st: ShutoffState, now: float) -> ShutoffState:
    """power_off raised. Keep ownership: the command may well have landed.

    The STOPPED branch re-sends power_off after STOP_RETRY_S while the miner
    still reports hashing, and clears the flag once it reports stopped.
    """
    st = replace(st)
    st.stop_failed_at = now
    return st


# --- the decision ---------------------------------------------------------------


def _update_gate(cfg: ShutoffConfig, st: ShutoffState, mean: float | None) -> None:
    if mean is None:
        st.gate_armed = False
    elif mean >= cfg.outdoor_min:
        st.gate_armed = True
    elif mean < cfg.outdoor_min - cfg.hysteresis:
        st.gate_armed = False
    # else: inside the hysteresis band, keep the current arm state


def _enter(st: ShutoffState, state: str, now: float) -> None:
    st.state = state
    st.since = now


def _to_running(st: ShutoffState, now: float) -> None:
    _enter(st, RUNNING, now)
    st.owned = False
    st.simulated = False
    st.dwell_start = None
    st.dwell_trigger = None
    st.call_polls = 0
    st.unknown_since = None
    st.mining_polls = 0
    st.reasserted_at = None


def _release_path(
    cfg: ShutoffConfig, st: ShutoffState, inp: ShutoffInputs, now: float
) -> Decision | None:
    """We own a real stop but must not keep it (mode off/observe): power on.

    Ownership is kept until the miner is seen hashing so a failed power_on is
    retried every RELEASE_RETRY_S.
    """
    if inp.fresh and inp.is_mining:
        _to_running(st, now)
        return Decision(NONE, "release confirmed", (), st)
    if st.state == RESUMING and now - (st.resume_sent_at or 0.0) < RELEASE_RETRY_S:
        return Decision(NONE, "release pending", ("waiting for hashing",), st)
    if st.state != RESUMING:
        _enter(st, RESUMING, now)
    st.resume_sent_at = now
    st.resume_attempts += 1
    st.last_resume_at = now
    notify = "resume_failed" if st.resume_attempts > RESUME_NOTIFY_ATTEMPTS else None
    return Decision(RELEASE, "releasing a stop the shutoff may no longer hold", (), st, notify)


def decide(cfg: ShutoffConfig, prev: ShutoffState, inp: ShutoffInputs, now: float) -> Decision:
    """One tick of the shutoff state machine."""
    st = replace(prev)
    blocking: list[str] = []
    _update_gate(cfg, st, inp.gate_mean)

    if inp.summary == CALLING and st.suppressed:
        st.suppressed = False

    # --- supply lockout latched: plant protection, independent of the mode ---
    if inp.latched:
        if st.state != RUNNING or st.owned:
            _to_running(st, now)
        if inp.freeze is True:
            if inp.supply is not None and inp.supply >= cfg.soft_cap:
                return Decision(
                    NONE, "freeze guard: waiting for supply below the soft cap",
                    (f"supply {inp.supply:.1f} ≥ cap {cfg.soft_cap:.1f}",), st,
                )
            # Own the restart so the RESUMING verify/retry path tracks it.
            _enter(st, RESUMING, now)
            st.owned = True
            st.resume_sent_at = now
            st.resume_attempts = 1
            st.last_resume_at = now
            return Decision(UNLATCH, "freeze guard: clearing supply lockout", (), st)
        return Decision(NONE, "supply lockout latched", ("latched",), st)

    # --- feature inactive, or observe mode inheriting a real stop -----------
    if not cfg.active:
        if st.owned:
            return _release_path(cfg, st, inp, now)
        if st.state != RUNNING:
            _to_running(st, now)
        return Decision(NONE, "disabled", (), st)
    if cfg.observe and st.owned and not st.simulated:
        return _release_path(cfg, st, inp, now)

    # --- RUNNING / DWELL: look for a stop trigger ---------------------------
    if st.state in (RUNNING, DWELL):
        # A just-adopted "external start" that stops by itself within minutes
        # was our own slow stop after all: take it back so it gets resumed.
        if (
            st.adopted_at is not None
            and now - st.adopted_at < ADOPT_RECLAIM_S
            and inp.fresh
            and not inp.is_mining
        ):
            _enter(st, STOPPED, now)
            st.owned = True
            st.suppressed = False
            st.adopted_at = None
            st.mining_polls = 0
            st.reasserts = 0
            st.reasserted_at = None
            return Decision(NONE, "adopted start stopped by itself; reclaiming ownership", (), st)
        if st.adopted_at is not None and now - st.adopted_at >= ADOPT_RECLAIM_S:
            st.adopted_at = None

        w_ok = st.gate_armed and inp.summary == ALL_IDLE
        s_ok = (
            cfg.supply_stop
            and inp.supply is not None
            and inp.supply >= cfg.soft_cap
            and inp.summary != CALLING
        )
        if st.state == DWELL and st.dwell_trigger == "S" and not s_ok:
            # S path releases at cap − 5°F; above that keep dwelling on S if idle
            if (
                cfg.supply_stop
                and inp.supply is not None
                and inp.supply >= cfg.soft_cap - 5.0
                and inp.summary != CALLING
            ):
                s_ok = True
        trigger = "S" if s_ok else ("W" if w_ok else None)

        if trigger is None:
            if inp.summary == CALLING:
                blocking += [f"{e} heating" for e in inp.calling] or ["thermostat calling"]
            elif inp.summary == UNKNOWN:
                blocking += [f"{e} unknown" for e in inp.unknown] or ["thermostats unknown"]
            if inp.summary != CALLING and not st.gate_armed:
                blocking.append(
                    "outdoor mean unknown" if inp.gate_mean is None
                    else f"outdoor mean {inp.gate_mean:.1f} < {cfg.outdoor_min:.0f}"
                )
            if st.state == DWELL:
                _to_running(st, now)
                return Decision(NONE, "dwell cancelled", tuple(blocking), st)
            return Decision(NONE, "running", tuple(blocking), st)

        if st.state == RUNNING:
            _enter(st, DWELL, now)
            st.dwell_start = now
            st.dwell_trigger = trigger
        elif st.dwell_trigger != trigger:
            st.dwell_trigger = trigger

        # Everything below blocks the stop but keeps the dwell alive.
        if inp.freeze is True:
            blocking.append("freeze guard")
        elif inp.freeze is None:
            warm_enough = (
                st.gate_armed
                and inp.gate_mean is not None
                and inp.gate_mean >= cfg.freeze_threshold + FREEZE_UNKNOWN_MARGIN_F
            )
            # Exception: a loop about to hit the latch. An owned S stop resumes
            # on demand; the latch would stop the miner until a human resets it.
            near_latch = (
                trigger == "S"
                and inp.supply is not None
                and inp.supply >= cfg.lockout - 5.0
            )
            if not warm_enough and not near_latch:
                blocking.append("freeze status unknown (cold gate)")
        if st.suppressed:
            blocking.append("suppressed until a thermostat calls")
        dwell_needed = cfg.supply_dwell_s if trigger == "S" else cfg.idle_dwell_s
        elapsed = now - (st.dwell_start or now)
        if elapsed < dwell_needed:
            blocking.append(f"dwell {int((dwell_needed - elapsed) // 60) + 1} min left")
        if trigger == "W" and st.last_resume_at is not None:
            on_for = now - st.last_resume_at
            if on_for < cfg.min_on_s:
                blocking.append(f"min_on {int((cfg.min_on_s - on_for) // 60) + 1} min left")
        if not inp.fresh:
            blocking.append("miner data stale")
        elif not inp.is_mining:
            blocking.append("miner not mining")
        if inp.user_off:
            blocking.append("user turned mining off")
        if blocking:
            return Decision(NONE, f"dwell ({trigger})", tuple(blocking), st)

        # STOP
        _enter(st, STOPPED, now)
        st.last_stop_at = now
        st.stop_failed_at = None
        st.mining_polls = 0
        st.reasserts = 0
        st.reasserted_at = None
        st.adopted_at = None
        st.call_polls = 0
        st.unknown_since = None
        st.resume_attempts = 0
        if cfg.observe:
            st.simulated = True
            st.owned = False
            st.would_stop_at = now
            return Decision(NONE, f"would stop ({trigger})", (), st)
        st.owned = True
        return Decision(STOP, f"stop ({trigger})", (), st)

    # --- STOPPED ------------------------------------------------------------
    if st.state == STOPPED:
        notify = None
        off_for = now - (st.since or now)
        if off_for > MAX_OFF_NOTICE_S and st.owned:
            notify = "max_off"
        heat_wanted = inp.summary == CALLING or inp.freeze is True

        if st.owned and not st.simulated and inp.fresh:
            if inp.is_mining:
                # Still (or again) hashing while we own a stop.
                if st.stop_failed_at is not None:
                    # Our power_off send failed; retry it unless heat is wanted now.
                    if now - st.stop_failed_at < STOP_RETRY_S:
                        return Decision(NONE, "stopped", ("retrying stop shortly",), st, notify)
                    if heat_wanted:
                        st.last_resume_at = now
                        _to_running(st, now)
                        return Decision(NONE, "stop failed and heat wanted; running", (), st)
                    st.stop_failed_at = now
                    return Decision(STOP, "retrying stop", (), st, notify)
                if off_for > REASSERT_MIN_AGE_S:
                    st.mining_polls += 1
                    if st.mining_polls < ADOPT_MINING_POLLS:
                        return Decision(
                            NONE, "stopped", ("miner reports hashing; confirming",), st, notify
                        )
                    if heat_wanted:
                        # Running and heat is wanted (or freeze risk): take it as
                        # resumed rather than bouncing it off and on.
                        st.last_resume_at = now
                        _to_running(st, now)
                        return Decision(NONE, "miner running and heat wanted; treating as resumed", (), st)
                    if st.reasserted_at is None or now - st.reasserted_at >= REASSERT_INTERVAL_S:
                        if st.reasserts < REASSERT_MAX:
                            st.reasserts += 1
                            st.reasserted_at = now
                            return Decision(
                                REASSERT, f"miner running while stopped; reasserting stop ({st.reasserts}/{REASSERT_MAX})", (), st,
                            )
                        _to_running(st, now)
                        st.suppressed = True
                        st.adopted_at = now
                        return Decision(ADOPT, "miner kept running after reasserts; adopting external start", (), st, "adopted")
                    return Decision(NONE, "stopped", ("reassert pending",), st, notify)
            else:
                # Fresh "not mining": the stop landed.
                st.stop_failed_at = None
                st.mining_polls = 0
                st.reasserts = 0
                st.reasserted_at = None

        # Freeze guard: resume now, only waiting for a hot stagnant loop to cool.
        if inp.freeze is True:
            if inp.supply is not None and inp.supply >= cfg.soft_cap:
                return Decision(
                    NONE, "freeze guard: waiting for supply below the soft cap",
                    (f"supply {inp.supply:.1f} ≥ cap {cfg.soft_cap:.1f}",), st, notify,
                )
            return _resume(cfg, st, now, "freeze guard", notify)
        # No freeze source under a cold gate: we can no longer tell. Fail-warm
        # once the loop is below the soft cap (the reason an S stop was taken).
        if inp.freeze is None and not st.gate_armed:
            if inp.supply is None or inp.supply < cfg.soft_cap:
                return _resume(cfg, st, now, "freeze status unknown (fail-warm)", notify)
            blocking.append("freeze unknown; waiting for supply below the soft cap")

        if inp.summary == CALLING:
            st.call_polls += 1
            st.unknown_since = None
        elif inp.summary == UNKNOWN:
            st.call_polls = 0
            if st.unknown_since is None:
                st.unknown_since = now
        else:
            st.call_polls = 0
            st.unknown_since = None

        if inp.summary == CALLING:
            if st.call_polls < RESUME_DEBOUNCE_POLLS:
                blocking.append("confirming call")
            elif off_for >= cfg.min_off_s or not st.gate_armed:
                return _resume(cfg, st, now, "thermostat calling", notify)
            else:
                blocking.append(f"min_off {int((cfg.min_off_s - off_for) // 60) + 1} min left")
        elif inp.summary == UNKNOWN:
            unknown_for = now - (st.unknown_since or now)
            if unknown_for >= cfg.unknown_grace_s:
                if inp.supply is None or inp.target is None or inp.supply <= inp.target:
                    return _resume(cfg, st, now, "thermostats unknown (fail-warm)", notify)
                blocking.append(f"supply {inp.supply:.1f} > target {inp.target:.1f}")
            else:
                blocking.append(
                    f"unknown grace {int((cfg.unknown_grace_s - unknown_for) // 60) + 1} min left"
                )
        else:
            blocking.append("all thermostats idle")
        return Decision(NONE, "stopped", tuple(blocking), st, notify)

    # --- RESUMING -----------------------------------------------------------
    if st.state == RESUMING:
        if st.simulated:
            _to_running(st, now)
            return Decision(NONE, "would resume: back to running", (), st)
        if inp.fresh and inp.is_mining:
            _to_running(st, now)
            return Decision(NONE, "resume confirmed", (), st)
        sent = st.resume_sent_at or now
        if now - sent >= RESUME_VERIFY_S:
            st.resume_sent_at = now
            st.resume_attempts += 1
            notify = "resume_failed" if st.resume_attempts > RESUME_NOTIFY_ATTEMPTS else None
            return Decision(RESUME, f"resume retry {st.resume_attempts}", (), st, notify)
        return Decision(NONE, "waiting for hashing", ("resume pending",), st)

    # Unknown state value (corrupt store): fail-warm by releasing ownership.
    _to_running(st, now)
    return Decision(NONE, "state reset", (), st)


def _resume(
    cfg: ShutoffConfig, st: ShutoffState, now: float, why: str, notify: str | None
) -> Decision:
    st.call_polls = 0
    st.unknown_since = None
    st.last_resume_at = now
    if st.simulated:
        st.would_resume_at = now
        _to_running(st, now)
        return Decision(NONE, f"would resume ({why})", (), st, notify)
    _enter(st, RESUMING, now)
    st.resume_sent_at = now
    st.resume_attempts = 1
    return Decision(RESUME, f"resume ({why})", (), st, notify)
