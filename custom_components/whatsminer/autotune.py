"""Self-tuning: step-response identification, run scoring, gain suggestions (1.9.0).

Pure logic, no Home Assistant imports. The controller feeds one ``Obs`` per
tick (after its own decisions are made) and every successful limit command;
this module never touches the miner. In ``observe`` mode it only learns and
publishes. In ``active`` mode it may return new kp/ki for the controller to
apply between runs, rate limited and rolled back if runs get worse.

System identification
    A segment opens when the soft start arms on a mining start, or when the
    controller raises the limit by at least STEP_TRIGGER_W, with the supply
    more than TRIGGER_BELOW_F under target and at least one zone calling. It
    carries PREROLL_S of earlier input as history for the dead time. It
    closes on: SEGMENT_MAX_S elapsed, the zones-calling bucket changing, a
    safety cap / lockout / shutoff stop, the miner off for RUN_GAP_S, the
    probe lost, or a probe jump of more than SUPPLY_JUMP_F. The data before
    the close is still fitted (a cap or zone change does not make it wrong,
    it only ends it); the rejection rules below decide if it is usable.

    Model: first order plus dead time,
        tau · dT/dt = −(T − Tb) + K · u(t − θ)
    with u the miner's draw (kW; the limit while hashing when the draw is
    not reported, 0 while not hashing, so restart blackouts are in u). The
    fit is output-error least squares on a 1 min grid: for each (tau, θ) on
    a grid the response is linear in (T0, Tb, K), so those three are solved
    exactly and (tau, θ) is searched, coarse then fine. Free T0 and Tb mean
    the segment need not start at steady state (a resume starts on a
    cooling loop) and any input shape works (soft-start steps, restarts,
    PID moves after the hand-back).

    K/tau (°F/min per kW, the initial rise rate) is the robust quantity: it
    is fixed by the first minutes of the climb, while K and tau separately
    need the response to bend over. The kp rule below depends on K/tau
    only, so a segment shorter than tau still gives a sound kp; ki also
    uses tau. A tau at the top of the grid is kept and flagged
    ``tau_bounded`` (an integrating-looking loop).

    Rejected: fewer than SEGMENT_MIN_S of data after the trigger; input span
    under MIN_U_SPAN_KW or supply span under MIN_RISE_F (no excitation);
    K outside K_RANGE; θ at the grid top or tau at the grid bottom; RMSE
    over FIT_MAX_RMSE_F or R² under FIT_MIN_R2.

    Fits are keyed by zones calling, bucketed "1", "2", "3+" (flow, so gain
    and time constant, change with open zones). A bucket's model is the
    median of its last MODEL_FITS fits, parameter by parameter.

Tuning rule (SIMC, Skogestad 2003)
        θe   = θ + increase_interval / 2     the PID can act no faster
        τc   = max(TC_FACTOR · θe, TC_MIN_MIN)
        kp   = tau / (K · (τc + θe))         = 1 / ((K/tau) · (τc + θe))
        τI   = min(tau, 4 · (τc + θe))
        ki   = kp / τI                       (W/°F·s, τI in s)
    τc is the tuning parameter: TC_FACTOR 1 is SIMC's "tight but robust"
    setting, applied to θe rather than θ because each limit change waits out
    the throttle and restarts btminer; TC_MIN_MIN keeps a short interval
    from asking for a fast, restart-hungry loop. kd is not tuned: SIMC on a
    FOPDT model is PI, the derivative on a 0.1125°F probe polled every 15 s
    is mostly quantisation, and the v1.8.2 predictor already does the
    anticipating. The configured kd always stays in force.

    Buckets combine by a weighted mean of their kp and ki, weighted by the
    PID-mode time spent in each bucket (falling back to fit counts).

    Suggested horizon for the predictor = θ + tau (the time a cut needs to
    take ~63 % effect), the largest over buckets, clamped to
    HORIZON_BOUNDS_MIN. Published only; the predictor keeps its constant.

Run scoring
    A run is mining with no gap of RUN_GAP_S or more (the restart every limit
    change causes does not split it), capped at RUN_MAX_S. Only ticks with a
    zone calling and the PID or the supply cap in charge are scored (an
    all-idle loop drifting up on the floor is not the gains' doing). Scores:
    peak overshoot above target, time to the ±BAND_F band, steady oscillation
    (half the 5-95 % spread of the error from SETTLE_S after entering the
    band, given 30 min of it), limit commands per hour, minutes at or above
    the supply cap. ``cost`` = overshoot + 2·oscillation + commands/h +
    0.5·minutes over cap.

Active mode
    After a run closes: at most one move per ACTIVE_STEP_INTERVAL_S, each
    parameter at most ACTIVE_STEP_FRAC of its value toward the (clamped)
    suggestion, skipped within ACTIVE_DEADBAND_FRAC. A move needs
    ACTIVE_MIN_FITS good fits and BASELINE_RUNS scored runs. The next
    EVAL_RUNS runs of at least EVAL_MIN_RUN_S are compared with the median
    cost before the move; worse than DEGRADE_RATIO × baseline + DEGRADE_ABS
    rolls the gains back and pauses moves for ROLLBACK_COOLDOWN_S. The
    overlay is dropped when the configured kp/ki change (they are the
    baseline) and ignored outside active mode.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from statistics import median
from typing import Any

MODES = ("off", "observe", "active")

# --- segmentation ------------------------------------------------------------
PREROLL_S = 20 * 60.0
SEGMENT_MIN_S = 20 * 60.0
SEGMENT_MAX_S = 120 * 60.0
STEP_TRIGGER_W = 200.0
SOFT_START_OPEN_S = 5 * 60.0
TRIGGER_BELOW_F = 2.0
SUPPLY_JUMP_F = 5.0  # same as the controller's slope-window reset
SCORED_MODES = ("pid", "safety_cap")
# Control modes that end a segment: the PID is no longer what drives the input.
STOP_MODES = ("safety_cap", "latched", "stopped", "resuming", "fallback", "demand_lockout", "dwell")
# --- fit -----------------------------------------------------------------------
FIT_DT_S = 60.0  # the probe reports once a minute
THETA_MAX_MIN = 15
TAU_MIN_MIN, TAU_MAX_MIN, TAU_RATIO = 1.5, 240.0, 1.4
MIN_U_SPAN_KW = 0.4
MIN_RISE_F = 3.0
K_RANGE = (1.0, 100.0)  # °F per kW
FIT_MAX_RMSE_F = 0.75
FIT_MIN_R2 = 0.9
FITS_PER_BUCKET = 8
MODEL_FITS = 5
REJECTS_KEPT = 10
BUCKETS = ("1", "2", "3+")
# --- tuning rule -----------------------------------------------------------------
TC_FACTOR = 1.0
TC_MIN_MIN = 10.0
MIN_FITS_FOR_SUGGESTION = 2
# Hard bounds on anything autotune suggests or applies.
BOUNDS = {"kp": (20.0, 150.0), "ki": (0.005, 0.3)}  # W/°F, W/°F·s
HORIZON_BOUNDS_MIN = (6.0, 20.0)
# --- runs ------------------------------------------------------------------------
RUN_GAP_S = 5 * 60.0
RUN_MAX_S = 6 * 3600.0
RUN_MIN_S = 30 * 60.0
RUN_SAMPLE_S = 60.0
RUN_HISTORY = 60
BAND_F = 2.0
SETTLE_S = 15 * 60.0
OSC_MIN_SAMPLES = 30
# --- active ----------------------------------------------------------------------
ACTIVE_STEP_FRAC = 0.10
ACTIVE_DEADBAND_FRAC = 0.03
ACTIVE_STEP_INTERVAL_S = 86400.0
ACTIVE_MIN_FITS = 3
BASELINE_RUNS = 3
EVAL_RUNS = 3
EVAL_MIN_RUN_S = 3600.0
DEGRADE_RATIO = 1.25
DEGRADE_ABS = 1.0
ROLLBACK_COOLDOWN_S = 3 * 86400.0
EVENTS_KEPT = 10

STORE_VERSION = 1


@dataclass(frozen=True)
class TunerConfig:
    mode: str
    kp: float
    ki: float
    kd: float
    supply_cap: float
    interval_increase_s: float


@dataclass(frozen=True)
class Obs:
    """What the controller saw and decided this tick (read-only copy)."""

    t: float
    supply: float | None
    target: float
    fresh: bool
    mining: bool
    power_w: float
    calling: int
    mode: str
    soft_start: bool


@dataclass(frozen=True)
class Gains:
    kp: float
    ki: float
    kd: float
    source: str  # "configured" or "autotune"


def bucket_of(calling: int) -> str | None:
    if calling <= 0:
        return None
    return "1" if calling == 1 else ("2" if calling == 2 else "3+")


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


# ------------------------------------------------------------------- the fit


def _solve3(a: list[list[float]], b: list[float]) -> list[float] | None:
    """Gaussian elimination with partial pivoting; None if singular."""
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for c in range(3):
        p = max(range(c, 3), key=lambda r: abs(m[r][c]))
        if abs(m[p][c]) < 1e-12:
            return None
        m[c], m[p] = m[p], m[c]
        for r in range(c + 1, 3):
            f = m[r][c] / m[c][c]
            for k in range(c, 4):
                m[r][k] -= f * m[c][k]
    x = [0.0, 0.0, 0.0]
    for r in (2, 1, 0):
        x[r] = (m[r][3] - sum(m[r][k] * x[k] for k in range(r + 1, 3))) / m[r][r]
    return x


def _resample(samples, t0: float, dt: float):
    """1-per-dt grid: u = mean draw over (t-dt, t], T = last reading ≤ t."""
    t_first, t_last = samples[0][0], samples[-1][0]
    n = int((t_last - t_first) // dt) + 1
    temps: list[float | None] = []
    us: list[float] = []
    j, last_t = 0, None
    for k in range(n):
        g = t_first + k * dt
        acc, cnt = 0.0, 0
        while j < len(samples) and samples[j][0] <= g + 1e-6:
            _, temp, u = samples[j]
            acc += u
            cnt += 1
            if temp is not None:
                last_t = temp
            j += 1
        us.append(acc / cnt if cnt else (us[-1] if us else samples[0][2]))
        temps.append(last_t)
    i0 = max(0, min(n - 1, int(math.ceil((t0 - t_first) / dt - 1e-9))))
    return temps, [u / 1000.0 for u in us], i0


def _evaluate(temps, us, i0, tau_min, theta_min, dt):
    """SSE and (T0, Tb, K) for one (tau, θ); temps centred by the caller."""
    a = math.exp(-dt / (tau_min * 60.0))
    y = [us[0]]
    for u in us[1:]:
        y.append(a * y[-1] + (1 - a) * u)
    d = theta_min * 60.0 / dt
    di, df = int(math.floor(d)), d - math.floor(d)

    def yd(k: int) -> float:
        k0, k1 = k - di, k - di - 1
        v0 = y[k0] if k0 >= 0 else y[0]
        if df == 0.0:
            return v0
        v1 = y[k1] if k1 >= 0 else y[0]
        return (1 - df) * v0 + df * v1

    y0 = yd(i0)
    s = [[0.0] * 3 for _ in range(3)]
    r = [0.0, 0.0, 0.0]
    e = 1.0
    for k in range(i0, len(temps)):
        tk = temps[k]
        if tk is not None:
            x = (e, 1.0 - e, yd(k) - y0 * e)
            for i in range(3):
                r[i] += x[i] * tk
                for j in range(i, 3):
                    s[i][j] += x[i] * x[j]
        e *= a
    for i in range(3):
        for j in range(i):
            s[i][j] = s[j][i]
    beta = _solve3(s, r)
    if beta is None:
        return math.inf, None
    sse = 0.0
    e = 1.0
    for k in range(i0, len(temps)):
        tk = temps[k]
        if tk is not None:
            m = beta[0] * e + beta[1] * (1.0 - e) + beta[2] * (yd(k) - y0 * e)
            sse += (tk - m) ** 2
        e *= a
    return sse, beta


def fit_fopdt(samples: list[tuple[float, float | None, float]], t0: float, dt: float = FIT_DT_S):
    """Fit a FOPDT model to (t, supply °F, draw W) samples from t0 on.

    Samples before t0 are input history only. Returns (fit dict, None) or
    (None, rejection reason).
    """
    if len(samples) < 3 or samples[-1][0] - t0 < SEGMENT_MIN_S:
        return None, "too short"
    temps, us, i0 = _resample(samples, t0, dt)
    window = [v for v in temps[i0:] if v is not None]
    n = len(window)
    if n < SEGMENT_MIN_S / dt * 0.8:
        return None, "too few samples"
    if max(window) - min(window) < MIN_RISE_F:
        return None, "supply barely moved"
    u_hist = us[max(0, i0 - int(THETA_MAX_MIN * 60 / dt)):]
    if max(u_hist) - min(u_hist) < MIN_U_SPAN_KW:
        return None, "input barely moved"
    mean_t = sum(window) / n
    centred = [None if v is None else v - mean_t for v in temps]
    sst = sum((v - mean_t) ** 2 for v in window)

    taus, tau = [], TAU_MIN_MIN
    while tau <= TAU_MAX_MIN * 1.0001:
        taus.append(tau)
        tau *= TAU_RATIO
    best = (math.inf, None, None, None)
    for tau in taus:
        for theta in range(0, THETA_MAX_MIN + 1):
            sse, beta = _evaluate(centred, us, i0, tau, float(theta), dt)
            if sse < best[0]:
                best = (sse, tau, float(theta), beta)
    if best[3] is None:
        return None, "singular"
    _, tau_c, theta_c, _ = best
    for i in range(-4, 5):
        tau = _clamp(tau_c * TAU_RATIO ** (i / 4), TAU_MIN_MIN, TAU_MAX_MIN)
        for j in range(-4, 5):
            theta = _clamp(theta_c + j / 4, 0.0, float(THETA_MAX_MIN))
            sse, beta = _evaluate(centred, us, i0, tau, theta, dt)
            if sse < best[0]:
                best = (sse, tau, theta, beta)
    sse, tau, theta, beta = best
    k = beta[2]
    rmse = math.sqrt(sse / n)
    r2 = 1.0 - sse / sst if sst > 0 else 0.0
    duration_min = (samples[-1][0] - t0) / 60.0
    fit = {
        "t": round(t0, 1),
        "k_f_per_kw": round(k, 3),
        "tau_min": round(tau, 2),
        "theta_min": round(theta, 2),
        "rate_f_min_per_kw": round(k / tau, 4) if tau > 0 else None,
        "rmse_f": round(rmse, 3),
        "r2": round(r2, 4),
        "n": n,
        "duration_min": round(duration_min, 1),
        "steady_state": duration_min >= 2.0 * tau,
        "tau_bounded": tau >= TAU_MAX_MIN * 0.999,
    }
    if theta >= THETA_MAX_MIN - 1e-9:
        return None, f"dead time at the {THETA_MAX_MIN} min limit"
    if tau <= TAU_MIN_MIN + 1e-9:
        return None, "time constant at the grid bottom"
    if not K_RANGE[0] <= k <= K_RANGE[1]:
        return None, f"gain {k:.2f}°F/kW out of range"
    if rmse > FIT_MAX_RMSE_F:
        return None, f"rmse {rmse:.2f}°F"
    if r2 < FIT_MIN_R2:
        return None, f"r2 {r2:.3f}"
    return fit, None


# ------------------------------------------------------------- tuning rule


def simc(model: dict, interval_increase_s: float) -> dict:
    """kp, ki, horizon from one model (see module docstring)."""
    k = model["k_f_per_kw"] / 1000.0  # °F per W
    tau, theta = model["tau_min"], model["theta_min"]
    theta_e = theta + max(0.0, interval_increase_s) / 120.0
    tc = max(TC_FACTOR * theta_e, TC_MIN_MIN)
    kp = tau / (k * (tc + theta_e))
    tau_i = min(tau, 4.0 * (tc + theta_e))
    return {
        "kp": kp,
        "ki": kp / (tau_i * 60.0),
        "horizon_min": theta + tau,
        "tc_min": tc,
        "theta_eff_min": theta_e,
    }


def run_scores(samples: list[tuple[float, float, float]], start: float, end: float,
               commands: int, above_cap_s: float) -> dict:
    """Scores for one run from (t, supply, target) samples."""
    errs = [(t, v - tgt) for t, v, tgt in samples]
    hours = max((end - start) / 3600.0, 1e-6)
    peak = max((e for _, e in errs), default=0.0)
    in_band = next((t for t, e in errs if abs(e) <= BAND_F), None)
    osc = None
    if in_band is not None:
        settled = sorted(e for t, e in errs if t >= in_band + SETTLE_S)
        if len(settled) >= OSC_MIN_SAMPLES:
            lo = settled[int(0.05 * (len(settled) - 1))]
            hi = settled[int(math.ceil(0.95 * (len(settled) - 1)))]
            osc = (hi - lo) / 2.0
    over = max(0.0, peak)
    cph = commands / hours
    cap_min = above_cap_s / 60.0
    cost = over + 2.0 * (osc or 0.0) + cph + 0.5 * cap_min
    return {
        "peak_overshoot_f": round(over, 2),
        "time_to_band_min": round((in_band - start) / 60.0, 1) if in_band is not None else None,
        "oscillation_f": round(osc, 2) if osc is not None else None,
        "commands_per_hour": round(cph, 2),
        "above_cap_min": round(cap_min, 1),
        "cost": round(cost, 3),
    }


# ------------------------------------------------------------------ the tuner


class Autotuner:
    """Learns from what the controller does. Never sends anything itself."""

    def __init__(self, cfg: TunerConfig) -> None:
        self.cfg = cfg
        self._clear_learned()
        self._buf: deque[tuple[float, float | None, float]] = deque()
        self._seg: dict | None = None
        self._run: dict | None = None
        self._last: Obs | None = None
        self._last_supply: float | None = None
        self._ss_edge_at: float | None = None
        self._ss_opened = False
        self._not_mining_since: float | None = None
        self._last_cmd_w: float | None = None
        self.dirty = False

    def _clear_learned(self) -> None:
        self.fits: dict[str, list[dict]] = {b: [] for b in BUCKETS}
        self.rejects: list[dict] = []
        self.runs: list[dict] = []
        self.occupancy: dict[str, float] = {b: 0.0 for b in BUCKETS}
        self.overlay: dict | None = None
        self.events: list[dict] = []
        self.segments = 0
        self.last_fit_at: float | None = None

    @property
    def mode(self) -> str:
        return self.cfg.mode if self.cfg.mode in MODES else "observe"

    # --- persistence ---------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": STORE_VERSION,
            "fits": self.fits,
            "rejects": self.rejects,
            "runs": self.runs,
            "occupancy": self.occupancy,
            "overlay": self.overlay,
            "events": self.events,
            "segments": self.segments,
            "last_fit_at": self.last_fit_at,
        }

    def load(self, data: dict | None) -> str | None:
        """Restore persisted data; returns a note for the log, if any."""
        if not data:
            return None
        self.fits = {
            b: [dict(f) for f in (data.get("fits") or {}).get(b, [])][-FITS_PER_BUCKET:] for b in BUCKETS
        }
        self.rejects = [dict(r) for r in data.get("rejects") or []][-REJECTS_KEPT:]
        self.runs = [dict(r) for r in data.get("runs") or []][-RUN_HISTORY:]
        occ = data.get("occupancy") or {}
        self.occupancy = {b: float(occ.get(b, 0.0)) for b in BUCKETS}
        self.events = [dict(e) for e in data.get("events") or []][-EVENTS_KEPT:]
        self.segments = int(data.get("segments") or 0)
        lf = data.get("last_fit_at")
        self.last_fit_at = float(lf) if lf is not None else None
        overlay = data.get("overlay")
        note = None
        if overlay:
            base = overlay.get("base") or []
            if len(base) == 2 and math.isclose(base[0], self.cfg.kp) and math.isclose(base[1], self.cfg.ki):
                overlay["kp"], overlay["ki"] = float(overlay["kp"]), float(overlay["ki"])
                self.overlay = overlay
            else:
                note = "configured kp/ki changed since the autotune overlay was made — overlay dropped"
                self.dirty = True
        return note

    def reset(self) -> None:
        self._clear_learned()
        self._seg = None
        self._run = None
        self.dirty = True

    # --- gains -----------------------------------------------------------------

    def effective(self) -> Gains:
        ov = self.overlay
        if self.mode == "active" and ov is not None:
            return Gains(ov["kp"], ov["ki"], self.cfg.kd, "autotune")
        return Gains(self.cfg.kp, self.cfg.ki, self.cfg.kd, "configured")

    def bucket_models(self) -> dict[str, dict]:
        out = {}
        for b in BUCKETS:
            recent = self.fits[b][-MODEL_FITS:]
            if not recent:
                continue
            out[b] = {
                key: round(median(f[key] for f in recent), 4)
                for key in ("k_f_per_kw", "tau_min", "theta_min", "rate_f_min_per_kw")
            }
            out[b]["fits"] = len(self.fits[b])
        return out

    def suggestion(self) -> dict | None:
        models = self.bucket_models()
        if sum(len(self.fits[b]) for b in BUCKETS) < MIN_FITS_FOR_SUGGESTION or not models:
            return None
        per = {b: simc(m, self.cfg.interval_increase_s) for b, m in models.items()}
        weights = {b: self.occupancy.get(b, 0.0) for b in per}
        if sum(weights.values()) <= 0:
            weights = {b: float(len(self.fits[b])) for b in per}
        total = sum(weights.values())
        kp = sum(per[b]["kp"] * w for b, w in weights.items()) / total
        ki = sum(per[b]["ki"] * w for b, w in weights.items()) / total
        horizon = max(p["horizon_min"] for p in per.values())
        return {
            "kp": round(_clamp(kp, *BOUNDS["kp"]), 3),
            "ki": round(_clamp(ki, *BOUNDS["ki"]), 5),
            "kd": None,  # not tuned: the configured kd stays
            "horizon_min": round(_clamp(horizon, *HORIZON_BOUNDS_MIN), 1),
            "kp_unclamped": round(kp, 3),
            "ki_unclamped": round(ki, 5),
            "horizon_unclamped_min": round(horizon, 1),
            "weights": {b: round(w / total, 3) for b, w in weights.items()},
        }

    # --- feeding -------------------------------------------------------------------

    def note_command(self, t: float, watts: float) -> None:
        """A limit command went out (after success)."""
        prev = self._last_cmd_w
        self._last_cmd_w = float(watts)
        if self._run is not None:
            self._run["cmds"] += 1
        last = self._last
        if last is None or self._seg is not None:
            return
        if prev is None:
            prev = last.power_w if last.mining else None
        if (
            prev is not None and watts >= prev + STEP_TRIGGER_W and last.supply is not None
            and last.supply < last.target - TRIGGER_BELOW_F and bucket_of(last.calling)
        ):
            self._open_segment(t, "step_up", bucket_of(last.calling))

    def observe(self, obs: Obs) -> Gains | None:
        """One tick. Returns new gains only when active mode moved them."""
        prev = self._last
        dt = min(obs.t - prev.t, 120.0) if prev is not None else 0.0
        self._last = obs
        if not obs.fresh:
            return None
        u = float(obs.power_w) if obs.mining else 0.0
        supply = obs.supply
        glitch = (
            supply is not None and self._last_supply is not None
            and abs(supply - self._last_supply) > SUPPLY_JUMP_F
        )
        if supply is not None:
            self._last_supply = supply
        bucket = bucket_of(obs.calling)
        if obs.mining and obs.mode == "pid" and bucket is not None:
            self.occupancy[bucket] += max(0.0, dt)

        self._buf.append((obs.t, None if glitch else supply, u))
        while self._buf and obs.t - self._buf[0][0] > PREROLL_S:
            self._buf.popleft()

        if self._seg is not None:
            self._segment_tick(obs, supply, u, bucket, glitch)
        # Soft-start rising edge. The first ticks after a start can still be
        # in a stop mode (resuming), so opening is retried for a few minutes.
        if obs.soft_start and obs.mining:
            if self._ss_edge_at is None:
                self._ss_edge_at = obs.t
            if self._seg is None and not self._ss_opened and obs.t - self._ss_edge_at <= SOFT_START_OPEN_S:
                self._ss_opened = self._try_open(obs, "soft_start", bucket)
        elif not obs.soft_start:
            self._ss_edge_at, self._ss_opened = None, False
        return self._run_tick(obs, supply, dt)

    def close(self, t: float) -> None:
        """HA is unloading: keep what the open segment and run already show."""
        if self._seg is not None:
            self._end_segment(t, "restart")
        if self._run is not None:
            self._close_run(t, "restart", apply=False)

    # --- segments --------------------------------------------------------------

    def _try_open(self, obs: Obs, why: str, bucket: str | None) -> bool:
        if bucket is None or obs.supply is None or obs.supply >= obs.target - TRIGGER_BELOW_F:
            return False
        if obs.mode in STOP_MODES:
            return False
        self._open_segment(obs.t, why, bucket)
        return True

    def _open_segment(self, t: float, why: str, bucket: str) -> None:
        self._seg = {"start": t, "trigger": why, "bucket": bucket, "samples": list(self._buf), "off_since": None}

    def _segment_tick(self, obs: Obs, supply, u: float, bucket, glitch: bool) -> None:
        seg = self._seg
        if glitch:
            return self._end_segment(obs.t, "probe_glitch")
        if supply is None:
            return self._end_segment(obs.t, "probe_lost")
        if bucket != seg["bucket"]:
            return self._end_segment(obs.t, "zones_changed")
        if obs.mode in STOP_MODES:
            return self._end_segment(obs.t, obs.mode)
        if not obs.mining:
            seg["off_since"] = seg["off_since"] or obs.t
            if obs.t - seg["off_since"] >= RUN_GAP_S:
                return self._end_segment(obs.t, "stopped")
        else:
            seg["off_since"] = None
        seg["samples"].append((obs.t, supply, u))
        if obs.t - seg["start"] >= SEGMENT_MAX_S:
            self._end_segment(obs.t, "complete")

    def _end_segment(self, t: float, reason: str) -> None:
        seg, self._seg = self._seg, None
        self.segments += 1
        self.dirty = True
        try:
            fit, why = fit_fopdt(seg["samples"], seg["start"])
        except (ArithmeticError, ValueError, IndexError) as err:
            fit, why = None, f"fit error: {err}"
        meta = {"trigger": seg["trigger"], "end_reason": reason, "bucket": seg["bucket"]}
        if fit is None:
            self.rejects.append({"t": round(seg["start"], 1), "reason": why, **meta})
            del self.rejects[:-REJECTS_KEPT]
            return
        fit.update(meta)
        fits = self.fits[seg["bucket"]]
        fits.append(fit)
        del fits[:-FITS_PER_BUCKET]
        self.last_fit_at = t

    # --- runs ---------------------------------------------------------------------

    def _run_tick(self, obs: Obs, supply, dt: float) -> Gains | None:
        run = self._run
        if obs.mining:
            self._not_mining_since = None
            if run is None:
                run = self._run = {
                    "start": obs.t, "end": obs.t, "samples": [], "cmds": 0, "cap_s": 0.0,
                    "start_supply": supply, "calling": [],
                }
            run["end"] = obs.t
            # Only ticks the gains are answerable for (see the docstring).
            if supply is not None and obs.calling > 0 and obs.mode in SCORED_MODES:
                if not run["samples"] or obs.t - run["samples"][-1][0] >= RUN_SAMPLE_S - 1e-6:
                    run["samples"].append((obs.t, supply, obs.target))
                    run["calling"].append(obs.calling)
                if supply >= self.cfg.supply_cap:
                    run["cap_s"] += dt
            if obs.t - run["start"] >= RUN_MAX_S:
                return self._close_run(obs.t, "max_length")
            return None
        if run is None:
            return None
        if self._not_mining_since is None:
            self._not_mining_since = obs.t
        elif obs.t - self._not_mining_since >= RUN_GAP_S:
            return self._close_run(run["end"], "stopped")
        return None

    def _close_run(self, t: float, reason: str, apply: bool = True) -> Gains | None:
        run, self._run = self._run, None
        self._not_mining_since = None
        if run is None or t - run["start"] < RUN_MIN_S or not run["samples"]:
            return None
        g = self.effective()
        rec = {
            "start": round(run["start"], 1),
            "duration_min": round((t - run["start"]) / 60.0, 1),
            "end_reason": reason,
            "start_supply": run["start_supply"],
            "zones_calling": median(run["calling"]) if run["calling"] else 0,
            "commands": run["cmds"],
            "kp": round(g.kp, 3),
            "ki": round(g.ki, 5),
            "gains": g.source,
            **run_scores(run["samples"], run["start"], t, run["cmds"], run["cap_s"]),
        }
        self.runs.append(rec)
        del self.runs[:-RUN_HISTORY]
        self.dirty = True
        if apply and self.mode == "active":
            return self._active_step(t, rec)
        return None

    # --- active mode -----------------------------------------------------------------

    def _event(self, t: float, kind: str, **kw) -> None:
        self.events.append({"t": round(t, 1), "event": kind, **kw})
        del self.events[:-EVENTS_KEPT]

    def _active_step(self, t: float, rec: dict) -> Gains | None:
        if self.mode != "active":
            return None
        before = self.effective()
        ov = self.overlay
        if ov is not None and ov.get("eval") is not None:
            if rec["duration_min"] * 60.0 >= EVAL_MIN_RUN_S:
                ov["eval"].append(rec["cost"])
            if len(ov["eval"]) < EVAL_RUNS:
                return None
            after = median(ov["eval"])
            limit = DEGRADE_RATIO * ov["baseline"] + DEGRADE_ABS
            if after > limit:
                prev = ov["prev"]
                self._event(t, "rollback", cost=round(after, 3), baseline=ov["baseline"],
                            from_kp=ov["kp"], from_ki=ov["ki"], to_kp=prev[0], to_ki=prev[1])
                if math.isclose(prev[0], self.cfg.kp) and math.isclose(prev[1], self.cfg.ki):
                    self.overlay = {"kp": prev[0], "ki": prev[1], "base": [self.cfg.kp, self.cfg.ki],
                                    "applied_at": t, "prev": prev, "eval": None, "baseline": None,
                                    "cooldown_until": t + ROLLBACK_COOLDOWN_S}
                else:
                    ov.update(kp=prev[0], ki=prev[1], eval=None, applied_at=t,
                              cooldown_until=t + ROLLBACK_COOLDOWN_S)
            else:
                self._event(t, "kept", cost=round(after, 3), baseline=ov["baseline"])
                ov["eval"] = None
            self.dirty = True
            return self._changed(before)
        if ov is not None:
            if t < (ov.get("cooldown_until") or 0.0) or t - ov.get("applied_at", 0.0) < ACTIVE_STEP_INTERVAL_S:
                return None
        sugg = self.suggestion()
        if sugg is None or sum(len(f) for f in self.fits.values()) < ACTIVE_MIN_FITS:
            return None
        recent = [r["cost"] for r in self.runs[-BASELINE_RUNS - 1:-1] if r.get("cost") is not None]
        recent.append(rec["cost"])
        if len(recent) < BASELINE_RUNS:
            return None
        cur = {"kp": before.kp, "ki": before.ki}
        new = dict(cur)
        for key in ("kp", "ki"):
            c, goal = cur[key], sugg[key]
            delta = goal - c
            if c <= 0 or abs(delta) < ACTIVE_DEADBAND_FRAC * c:
                continue
            step = _clamp(delta, -ACTIVE_STEP_FRAC * c, ACTIVE_STEP_FRAC * c)
            value = c + step
            lo, hi = BOUNDS[key]
            if lo <= c <= hi:
                value = _clamp(value, lo, hi)
            new[key] = value
        if new == cur:
            return None
        self.overlay = {
            "kp": new["kp"], "ki": new["ki"], "base": [self.cfg.kp, self.cfg.ki],
            "applied_at": t, "prev": [cur["kp"], cur["ki"]], "eval": [],
            "baseline": round(median(recent[-BASELINE_RUNS:]), 3), "cooldown_until": None,
        }
        self._event(t, "step", from_kp=round(cur["kp"], 3), from_ki=round(cur["ki"], 5),
                    to_kp=round(new["kp"], 3), to_ki=round(new["ki"], 5))
        self.dirty = True
        return self._changed(before)

    def _changed(self, before: Gains) -> Gains | None:
        after = self.effective()
        return after if (after.kp, after.ki) != (before.kp, before.ki) else None

    # --- publishing ------------------------------------------------------------------

    def status(self) -> str:
        if self.mode == "off":
            return "off"
        ov = self.overlay
        if self.mode == "active" and ov is not None:
            if ov.get("eval") is not None:
                return "evaluating"
            if ov.get("cooldown_until") and self._last is not None and self._last.t < ov["cooldown_until"]:
                return "rolled_back"
            return "active"
        return "ready" if self.suggestion() is not None else "learning"

    def snapshot(self) -> dict[str, Any]:
        g = self.effective()
        fits = sum(len(f) for f in self.fits.values())
        last_run = self.runs[-1] if self.runs else None
        return {
            "state": self.status(),
            "mode": self.mode,
            "runs": len(self.runs),
            "fits": fits,
            "segments": self.segments,
            "last_fit_at": self.last_fit_at,
            "segment_open": self._seg is not None,
            "models": self.bucket_models(),
            "suggested": self.suggestion(),
            "effective": {"kp": g.kp, "ki": g.ki, "kd": g.kd, "source": g.source},
            "configured": {"kp": self.cfg.kp, "ki": self.cfg.ki, "kd": self.cfg.kd},
            "last_run": last_run,
            "last_fit": next((self.fits[b][-1] for b in sorted(
                BUCKETS, key=lambda b: self.fits[b][-1]["t"] if self.fits[b] else -1, reverse=True)
                if self.fits[b]), None),
            "last_reject": self.rejects[-1] if self.rejects else None,
            "overlay": None if self.overlay is None else {
                k: self.overlay.get(k) for k in ("kp", "ki", "applied_at", "prev", "eval", "baseline", "cooldown_until")
            },
            "last_event": self.events[-1] if self.events else None,
        }
