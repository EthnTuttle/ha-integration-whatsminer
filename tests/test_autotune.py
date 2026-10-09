"""Self-tuning (1.9.0): FOPDT fit, rejection, observe ≡ off, active guard rails.

The fit is checked on a pure FOPDT plant (exact recovery) and on both
tests/loop_plant.py variants through full controller replays. True values
for the loop plant: K = 1000/(30 + 80·calling) °F/kW and thermal tau the
same number in minutes (C = 1000 W·min/°F), plus the heat-path lag.
"""
from __future__ import annotations

import importlib
import json
import math
import pathlib
import random
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import loop_plant  # noqa: E402
from test_controller_smoke import MIN, T0, Rig, Store, base_config, const, controller_mod, run_async  # noqa: E402
from test_soft_start import OLD, half_flow, resume, resume_rig, satisfy_after_target  # noqa: E402

at = importlib.import_module("wm.autotune")
AT_KEY = "whatsminer.e1.autotune"


def full_flow(plant):
    return 1.0


def fopdt(k, tau_min, theta_min, steps, start, minutes, dt=15.0, quant=None):
    """(t, T, u) from an exact FOPDT plant: tau·dT/dt = −(T − start) + K·u(t − θ)."""
    out, t, temp = [], 0.0, start
    while t <= minutes * 60:
        u = next((w for ts, w in reversed(steps) if t >= ts), 0.0)
        ud = next((w for ts, w in reversed(steps) if t - theta_min * 60 >= ts), 0.0)
        for _ in range(3):
            temp += (-(temp - start) + k * ud / 1000.0) / (tau_min * 60.0) * 5.0
        shown = round(temp / quant) * quant if quant else temp
        out.append((t, shown, u))
        t += dt
    return out


# Resume shape: soft start 1200 W at 20 min, restarts on each step up.
STEPS = [(0, 0.0), (20 * MIN, 1200.0), (40 * MIN, 0.0), (41 * MIN, 2400.0), (70 * MIN, 0.0), (71 * MIN, 3000.0)]


def segment(samples, t0):
    return [s for s in samples if s[0] >= t0 - at.PREROLL_S]


# ------------------------------------------------------------------- the fit


def test_fit_recovers_an_exact_fopdt_plant():
    samples = fopdt(12.0, 10.0, 3.0, STEPS, 88.0, 130, quant=loop_plant.PROBE_STEP)
    fit, why = at.fit_fopdt(segment(samples, 20 * MIN), 20 * MIN)
    assert why is None
    assert fit["k_f_per_kw"] == pytest.approx(12.0, rel=0.03)
    assert fit["tau_min"] == pytest.approx(10.0, rel=0.08)
    assert fit["theta_min"] == pytest.approx(3.0, abs=0.5)
    assert fit["rmse_f"] < 0.1 and fit["r2"] > 0.999


@pytest.mark.parametrize(
    "lag,flow,frac,k_tol",
    [
        ("LAG_FITTED", full_flow, 1.0, 0.05),
        ("LAG_FITTED", half_flow, 0.5, 0.05),
        ("LAG_12MIN", full_flow, 1.0, 0.10),
        ("LAG_12MIN", half_flow, 0.5, 0.25),
    ],
)
def test_fit_on_the_loop_plant_through_a_resume(monkeypatch, lag, flow, frac, k_tol):
    """The resume's soft start is the step test; the plant is not exactly FOPDT."""
    rig = resume_rig(monkeypatch, **OLD["stopgap"])
    run_async(resume(rig, flow, getattr(loop_plant, lag), minutes=150))
    fits = rig.ctl._autotuner.fits["3+"]  # the plant sets all four thermostats
    assert len(fits) == 1, rig.ctl._autotuner.rejects
    fit = fits[0]
    assert fit["trigger"] == "soft_start" and fit["end_reason"] == "complete"
    k_true = 1000.0 / (30.0 + 80.0 * frac)
    dead_s, lag_s = getattr(loop_plant, lag)
    assert fit["k_f_per_kw"] == pytest.approx(k_true, rel=k_tol)
    # tau + θ is the plant's total "63 % time": thermal tau + dead time + lag.
    total = k_true + (dead_s + lag_s) / 60.0
    assert fit["tau_min"] + fit["theta_min"] == pytest.approx(total, rel=0.2)
    assert fit["theta_min"] >= dead_s / 60.0
    assert fit["r2"] > 0.99


def test_fit_runs_fast_enough_for_the_event_loop():
    import time

    samples = fopdt(12.0, 10.0, 3.0, STEPS, 88.0, 20 + at.SEGMENT_MAX_S / 60, quant=loop_plant.PROBE_STEP)
    t = time.perf_counter()
    at.fit_fopdt(segment(samples, 20 * MIN), 20 * MIN)
    assert time.perf_counter() - t < 0.5  # ~30 ms here; once per segment


# ---------------------------------------------------------------- rejection


def test_no_input_change_is_rejected():
    samples = fopdt(12.0, 10.0, 3.0, [(0, 2000.0)], 88.0, 60)
    fit, why = at.fit_fopdt(samples, 10 * MIN)
    assert fit is None and why in ("input barely moved", "supply barely moved")


def test_short_segment_is_rejected():
    samples = fopdt(12.0, 10.0, 3.0, STEPS, 88.0, 35)
    fit, why = at.fit_fopdt(segment(samples, 20 * MIN), 20 * MIN)
    assert fit is None and why == "too short"


def test_noisy_segment_is_rejected():
    rnd = random.Random(1)
    clean = fopdt(12.0, 10.0, 3.0, STEPS, 88.0, 110)
    noisy = [(t, temp + rnd.uniform(-4, 4), u) for t, temp, u in clean]
    fit, why = at.fit_fopdt(segment(noisy, 20 * MIN), 20 * MIN)
    assert fit is None and (why.startswith("rmse") or why.startswith("r2"))


def test_wrong_sign_response_is_rejected():
    samples = fopdt(-12.0, 10.0, 3.0, STEPS, 120.0, 110)
    fit, why = at.fit_fopdt(segment(samples, 20 * MIN), 20 * MIN)
    assert fit is None and "gain" in why


def obs(t, supply, **kw):
    base = dict(target=104.0, fresh=True, mining=True, power_w=2000.0, calling=3, mode="pid", soft_start=False)
    base.update(kw)
    return at.Obs(t=t, supply=supply, **base)


def tuner(mode="observe", kp=60.0, ki=0.06):
    return at.Autotuner(at.TunerConfig(mode=mode, kp=kp, ki=ki, kd=55.556, supply_cap=122.0,
                                       interval_increase_s=300.0))


def test_probe_glitch_ends_the_segment_and_it_is_rejected():
    tu = tuner()
    tu.observe(obs(T0, 88.0, mining=False, power_w=0.0))
    tu.observe(obs(T0 + 15, 88.0, soft_start=True))
    assert tu._seg is not None
    for i in range(1, 20):
        tu.observe(obs(T0 + 15 + 60 * i, 88.0 + 0.3 * i, soft_start=True))
    tu.observe(obs(T0 + 1300, 102.0, soft_start=True))  # +8°F in one poll
    assert tu._seg is None and tu.rejects[-1]["end_reason"] == "probe_glitch"
    assert tu.rejects[-1]["reason"] == "too short" and not any(tu.fits.values())


def test_no_segment_without_a_calling_zone_or_near_target():
    tu = tuner()
    tu.observe(obs(T0, 88.0, soft_start=True, calling=0))
    assert tu._seg is None
    tu.observe(obs(T0 + 15, 88.0, soft_start=False))
    tu.observe(obs(T0 + 30, 103.0, soft_start=True))  # already near target
    assert tu._seg is None


def test_soft_start_edge_in_a_stop_mode_opens_on_a_later_tick():
    tu = tuner()
    tu.observe(obs(T0, 88.0, soft_start=True, mode="resuming"))
    assert tu._seg is None
    tu.observe(obs(T0 + 15, 88.0, soft_start=True))
    assert tu._seg is not None and tu._seg["trigger"] == "soft_start"
    tu._seg = None  # opens once per soft start, not again on later ticks
    tu.observe(obs(T0 + 30, 88.0, soft_start=True))
    assert tu._seg is None


def test_zone_change_ends_a_segment():
    tu = tuner()
    tu.observe(obs(T0, 88.0, soft_start=True, calling=1))
    assert tu._seg["bucket"] == "1"
    tu.observe(obs(T0 + 60, 88.5, soft_start=True, calling=2))
    assert tu._seg is None and tu.rejects[-1]["end_reason"] == "zones_changed"


# ------------------------------------------------------------- observe ≡ off


def _replay(monkeypatch, autotune_mode, scenario, break_autotune=False):
    tune, flow, lag, minutes = scenario
    rig = resume_rig(monkeypatch, **OLD[tune])
    rig.ctl._autotuner.cfg = at.TunerConfig(**{**rig.ctl._autotuner.cfg.__dict__, "mode": autotune_mode})
    if break_autotune:
        def boom(*_a, **_k):
            raise RuntimeError("autotune exploded")
        monkeypatch.setattr(at.Autotuner, "observe", boom)
        monkeypatch.setattr(at.Autotuner, "note_command", boom)
        monkeypatch.setattr(at.Autotuner, "snapshot", boom)
    trace = []

    def each(plant):
        p = rig.pid_state
        trace.append((round(plant.t, 3), p.get("output"), p.get("integral"), p.get("control_mode"),
                      p.get("predictive"), p.get("soft_start"), rig.ctl._kp))

    plant = run_async(resume(rig, flow, getattr(loop_plant, lag), minutes=minutes, each_tick=each))
    return rig, plant, trace


SCENARIOS = {
    "12min_half_stopgap": ("stopgap", half_flow, "LAG_12MIN", 480),
    "fitted_satisfy_kp": ("kp", satisfy_after_target, "LAG_FITTED", 480),
    "fitted_full_stopgap": ("stopgap", full_flow, "LAG_FITTED", 480),
}


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_observe_sends_exactly_what_off_sends(monkeypatch, name):
    """Bit-for-bit: same commands at the same times and the same PID internals every tick."""
    rig_off, plant_off, trace_off = _replay(monkeypatch, "off", SCENARIOS[name])
    rig_obs, plant_obs, trace_obs = _replay(monkeypatch, "observe", SCENARIOS[name])
    assert plant_obs.commands == plant_off.commands
    assert trace_obs == trace_off
    assert rig_off.ctl._autotuner.segments == 0 and rig_off.pid_state["autotune"]["state"] == "off"
    # ...and observe really did learn while doing it.
    tu = rig_obs.ctl._autotuner
    assert tu.segments >= 1 and rig_obs.pid_state["autotune"]["mode"] == "observe"
    assert rig_obs.pid_state["pid_kp"] == OLD[SCENARIOS[name][0]]["kp"]


def test_active_with_nothing_learned_also_matches_off(monkeypatch):
    sc = SCENARIOS["12min_half_stopgap"]
    _, plant_off, trace_off = _replay(monkeypatch, "off", sc)
    _, plant_act, trace_act = _replay(monkeypatch, "active", sc)
    assert plant_act.commands == plant_off.commands and trace_act == trace_off


def test_an_exception_in_autotune_never_touches_control(monkeypatch, caplog):
    sc = SCENARIOS["fitted_satisfy_kp"]
    _, plant_off, trace_off = _replay(monkeypatch, "off", sc)
    rig, plant_bad, trace_bad = _replay(monkeypatch, "observe", sc, break_autotune=True)
    assert plant_bad.commands == plant_off.commands and trace_bad == trace_off
    assert "Autotune failed" in caplog.text
    assert rig.pid_state["pid_kp"] == OLD["kp"]["kp"]  # gains still published


def test_fit_failure_inside_a_segment_is_contained(monkeypatch):
    monkeypatch.setattr(at, "fit_fopdt", lambda *_a, **_k: 1 / 0)
    sc = SCENARIOS["12min_half_stopgap"]
    rig, plant, _ = _replay(monkeypatch, "observe", sc)
    assert plant.commands  # control carried on
    assert rig.ctl._autotuner.rejects[-1]["reason"].startswith("fit error")


# ------------------------------------------------------------------ active


def seeded(mode="active", k=9.0, tau=10.0, theta=1.5, fits=3, kp=60.0, ki=0.06):
    tu = tuner(mode, kp, ki)
    for i in range(fits):
        tu.fits["3+"].append({"t": T0 + i, "k_f_per_kw": k, "tau_min": tau, "theta_min": theta,
                              "rate_f_min_per_kw": k / tau})
    tu.occupancy["3+"] = 3600.0
    for i in range(at.BASELINE_RUNS):
        tu.runs.append({"cost": 2.0, "duration_min": 120.0})
    return tu


def finish(tu, t, cost, minutes=120.0):
    rec = {"cost": cost, "duration_min": minutes}
    tu.runs.append(rec)
    tu._last = obs(t, 104.0)
    return tu._active_step(t, rec)


def test_simc_suggestion_for_a_known_model():
    tu = seeded()
    s = tu.suggestion()
    # θe = 1.5 + 300/120 = 4 min, τc = max(4, 10) = 10 → kp = 10/(0.009·14) = 79.4 W/°F,
    # τI = min(10, 56) = 10 min → ki = 79.4/600 = 0.132 W/°F·s; horizon θ + tau.
    assert s["kp"] == pytest.approx(79.37, abs=0.05)
    assert s["ki"] == pytest.approx(0.1323, abs=0.0005)
    assert s["horizon_min"] == pytest.approx(11.5) and s["kd"] is None


def test_observe_never_moves_gains():
    tu = seeded(mode="observe")
    assert finish(tu, T0, 2.0) is None
    assert tu.overlay is None and tu.effective().source == "configured"


def test_active_moves_at_most_ten_percent_once_a_day():
    tu = seeded()
    g = finish(tu, T0, 2.0)
    assert g.kp == pytest.approx(66.0) and g.ki == pytest.approx(0.066) and g.source == "autotune"
    for i in range(at.EVAL_RUNS):  # evaluation: no further move, then kept
        assert finish(tu, T0 + 3600 * (i + 1), 2.1) is None
    assert tu.events[-1]["event"] == "kept"
    assert finish(tu, T0 + 20 * 3600, 2.0) is None  # inside 24 h
    g = finish(tu, T0 + 86400, 2.0)
    assert g.kp == pytest.approx(72.6) and g.ki == pytest.approx(0.0726)


def test_active_never_leaves_the_hard_bounds():
    lo, hi = at.BOUNDS["kp"]
    # K 1°F/kW asks for kp ≈ 714, K 90°F/kW for kp ≈ 7.9: both far outside.
    for k, expect in ((1.0, hi), (90.0, lo)):
        tu = seeded(k=k, tau=10.0)
        t = T0
        for _ in range(60):
            finish(tu, t, 2.0)
            for i in range(at.EVAL_RUNS):
                finish(tu, t + 3600 * (i + 1), 2.0)
            t += 86400
            g = tu.effective()
            assert lo <= g.kp <= hi and at.BOUNDS["ki"][0] <= g.ki <= at.BOUNDS["ki"][1]
        assert tu.effective().kp == pytest.approx(expect)


def test_active_rolls_back_when_runs_get_worse_and_cools_down():
    tu = seeded()
    finish(tu, T0, 2.0)
    assert tu.effective().kp == pytest.approx(66.0)
    finish(tu, T0 + 3600, 9.0, minutes=30)  # too short to count
    assert len(tu.overlay["eval"]) == 0
    finish(tu, T0 + 7200, 9.0)
    finish(tu, T0 + 10800, 9.0)
    g = finish(tu, T0 + 14400, 9.0)
    assert g.kp == pytest.approx(60.0) and g.ki == pytest.approx(0.06)
    assert tu.events[-1]["event"] == "rollback" and tu.status() == "rolled_back"
    assert finish(tu, T0 + 2 * 86400, 2.0) is None  # cooldown (3 days)
    assert finish(tu, T0 + 4 * 86400, 2.0).kp == pytest.approx(66.0)


def test_overlay_is_dropped_when_the_configured_gains_change():
    tu = seeded()
    finish(tu, T0, 2.0)
    data = json.loads(json.dumps(tu.to_dict()))
    again = tuner("active", kp=80.0)
    assert "dropped" in again.load(data)
    assert again.overlay is None and again.effective().kp == 80.0


# ------------------------------------------------------------- persistence


def test_tuner_round_trip_through_json():
    tu = seeded()
    finish(tu, T0, 2.0)
    tu.rejects.append({"t": T0, "reason": "too short"})
    data = json.loads(json.dumps(tu.to_dict()))
    again = tuner("active")
    assert again.load(data) is None
    assert again.to_dict() == data
    assert again.effective() == tu.effective()


def test_controller_persists_and_restores_across_a_restart(monkeypatch):
    rig = resume_rig(monkeypatch, **OLD["stopgap"])
    run_async(resume(rig, half_flow, loop_plant.LAG_12MIN, minutes=240))
    tu = rig.ctl._autotuner
    assert sum(len(f) for f in tu.fits.values()) == 1
    run_async(rig.ctl.async_unload())
    saved = json.loads(json.dumps(Store.saved[AT_KEY]))
    assert len(saved["runs"]) == 1 and saved["runs"][0]["end_reason"] == "restart"
    assert saved["fits"]["3+"][0]["k_f_per_kw"] > 0

    Store.saved[AT_KEY] = saved
    pid_state = {"target": 104.0}
    ctl = controller_mod.WhatsminerController(rig.hass, rig.ctl.entry, rig.coord, pid_state, base_config())
    run_async(ctl.async_setup())
    assert ctl._autotuner.fits == saved["fits"] and ctl._autotuner.runs == saved["runs"]
    assert pid_state["autotune"]["fits"] == 1 and pid_state["autotune"]["runs"] == 1


def test_active_overlay_is_applied_at_startup_and_reset_restores_configured(monkeypatch):
    tu = seeded()
    finish(tu, T0, 2.0)
    rig = Rig(monkeypatch, **{const.CONF_PID_KP: 60.0, const.CONF_PID_KI: 0.06,
                              const.CONF_PID_AUTOTUNE_MODE: "active"})
    Store.saved[AT_KEY] = json.loads(json.dumps(tu.to_dict()))
    run_async(rig.setup())
    assert rig.ctl._kp == pytest.approx(66.0) and rig.ctl._pid._Kp == pytest.approx(66.0)
    assert rig.ctl._pid._Ki == pytest.approx(0.066)
    assert rig.pid_state["pid_kp"] == pytest.approx(66.0)
    assert rig.pid_state["autotune"]["effective"]["source"] == "autotune"
    run_async(rig.ctl.async_reset_autotune())
    assert rig.ctl._kp == 60.0 and rig.ctl._pid._Kp == 60.0 and rig.ctl._pid._Ki == 0.06
    assert Store.saved[AT_KEY]["overlay"] is None and Store.saved[AT_KEY]["fits"]["3+"] == []
    assert rig.pid_state["autotune"]["fits"] == 0


def test_observe_ignores_a_stored_overlay(monkeypatch):
    tu = seeded()
    finish(tu, T0, 2.0)
    rig = Rig(monkeypatch, **{const.CONF_PID_KP: 60.0, const.CONF_PID_KI: 0.06})
    Store.saved[AT_KEY] = json.loads(json.dumps(tu.to_dict()))
    run_async(rig.setup())
    assert rig.ctl._kp == 60.0 and rig.ctl._pid._Ki == 0.06
    assert rig.pid_state["autotune"]["mode"] == "observe"  # the default


def test_corrupt_autotune_store_starts_fresh(monkeypatch):
    rig = Rig(monkeypatch)
    Store.saved[AT_KEY] = {"fits": {"3+": [1, 2]}, "runs": "nope", "occupancy": {"1": "x"}}
    run_async(rig.setup())
    assert rig.pid_state["autotune"]["fits"] == 0
    assert rig.ctl._kp == float(rig.ctl._autotuner.cfg.kp)


def test_bump_free_gain_change_keeps_p_plus_i(monkeypatch):
    rig = Rig(monkeypatch, **{const.CONF_PID_KP: 60.0, const.CONF_PID_KI: 0.06})
    pid = rig.ctl._pid
    pid._error, pid.integral = 4.0, 1500.0
    before = 60.0 * 4.0 + 1500.0
    rig.ctl._apply_gains(at.Gains(66.0, 0.066, 55.556, "autotune"))
    assert 66.0 * 4.0 + pid.integral == pytest.approx(before)
    assert math.isclose(rig.ctl._kp, 66.0)
