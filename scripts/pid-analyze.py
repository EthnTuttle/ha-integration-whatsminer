#!/usr/bin/env python3
"""Analyze one or two pid-run-*.json captures and print a tuning report.

Usage:
    python3 scripts/pid-analyze.py RUN.json
    python3 scripts/pid-analyze.py OLD.json NEW.json   # side-by-side compare

Reads a capture produced by scripts/pid-capture.py and reports run shape,
setpoint segments, tracking quality, oscillation, saturation, integral
trajectory, term balance, and actuation frequency.

Error is computed natively in °C from (target_C − PV_C); the pid_error
sensor stored in the capture is ignored because HA applies an absolute
F = 9/5·C + 32 conversion to the delta and shifts it by +32°F.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import statistics as S
import sys
from collections import Counter
from typing import Any


Point = tuple[dt.datetime, float]


def f_to_c(f: float) -> float:
    return (f - 32.0) * 5.0 / 9.0


def series_items(series: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return the raw entries as-is; each already has state/last_changed."""
    return series


def series_entity_id(series: list[dict[str, Any]]) -> str | None:
    for entry in series:
        eid = entry.get("entity_id")
        if eid:
            return eid
    return None


def series_unit(series: list[dict[str, Any]]) -> str | None:
    for entry in series:
        attrs = entry.get("attributes")
        if attrs and attrs.get("unit_of_measurement"):
            return attrs["unit_of_measurement"]
    return None


def numeric_points(series: list[dict[str, Any]]) -> list[Point]:
    out: list[Point] = []
    for entry in series:
        s = entry.get("state")
        if s in (None, "unavailable", "unknown"):
            continue
        try:
            v = float(s)
        except (TypeError, ValueError):
            continue
        t = entry.get("last_changed") or entry.get("last_updated")
        if not t:
            continue
        try:
            out.append((dt.datetime.fromisoformat(t), v))
        except ValueError:
            continue
    return out


def string_points(series: list[dict[str, Any]]) -> list[tuple[dt.datetime, str]]:
    out: list[tuple[dt.datetime, str]] = []
    for entry in series:
        s = entry.get("state")
        if s in (None, "unavailable", "unknown"):
            continue
        t = entry.get("last_changed") or entry.get("last_updated")
        if not t:
            continue
        try:
            out.append((dt.datetime.fromisoformat(t), str(s)))
        except ValueError:
            continue
    return out


def index_history(history: list[list[dict[str, Any]]]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for series in history:
        if not series:
            continue
        eid = series_entity_id(series)
        if eid:
            out[eid] = series
    return out


def to_celsius(points: list[Point], unit: str | None) -> list[Point]:
    if unit and unit.lower() in ("°f", "f", "fahrenheit"):
        return [(t, f_to_c(v)) for t, v in points]
    return points


def median_dt_seconds(points: list[Point]) -> float:
    if len(points) < 2:
        return 0.0
    gaps = [(points[i + 1][0] - points[i][0]).total_seconds() for i in range(len(points) - 1)]
    return S.median(gaps)


def detect_setpoint_segments(
    target_c_points: list[Point],
    pv_window: tuple[dt.datetime, dt.datetime] | None = None,
) -> list[tuple[dt.datetime, dt.datetime, float]]:
    """Return [(start, end, setpoint_c), ...].

    Target history typically only contains a few entries (HA only logs changes).
    Extend the first segment back to PV window start and the last segment to
    PV window end, so a stable setpoint over the whole run becomes one segment
    that spans the full PV window.
    """
    if not target_c_points:
        return []
    segs: list[tuple[dt.datetime, dt.datetime, float]] = []
    start_t, start_v = target_c_points[0]
    prev_v = start_v
    for t, v in target_c_points[1:]:
        if abs(v - prev_v) >= 0.05:
            segs.append((start_t, t, prev_v))
            start_t, prev_v = t, v
    # Close the last segment at the PV window end (not the last target update)
    end_t = pv_window[1] if pv_window else target_c_points[-1][0]
    segs.append((start_t, end_t, prev_v))
    # Extend the first segment back to PV window start if it starts later
    if pv_window and segs:
        s0, e0, v0 = segs[0]
        if pv_window[0] < s0:
            segs[0] = (pv_window[0], e0, v0)
    return segs


def slice_by_time(points: list[Point], t0: dt.datetime, t1: dt.datetime) -> list[Point]:
    return [(t, v) for t, v in points if t0 <= t <= t1]


def zero_crossings(signal: list[float]) -> int:
    count = 0
    for a, b in zip(signal, signal[1:]):
        if a == 0 or (a > 0) != (b > 0):
            count += 1
    return count


def settling_time_seconds(pv: list[Point], sp: float, band: float) -> float | None:
    """Return time from t0 until |pv − sp| stays within band for the rest of the segment."""
    if not pv:
        return None
    t0 = pv[0][0]
    # walk from the end backwards; find the last point *outside* the band
    last_bad_idx = -1
    for i in range(len(pv) - 1, -1, -1):
        if abs(pv[i][1] - sp) > band:
            last_bad_idx = i
            break
    if last_bad_idx == -1:
        return 0.0
    if last_bad_idx == len(pv) - 1:
        return None  # never settled
    return (pv[last_bad_idx + 1][0] - t0).total_seconds()


def analyze(path: str) -> dict[str, Any]:
    with open(path) as f:
        data = json.load(f)

    slug = data.get("miner_slug", "heatcore")
    external = data.get("external_sensor")
    window = data.get("window", {})
    cfg = data.get("config_entry") or {}
    cfg_options = (cfg.get("options") or {}) if isinstance(cfg, dict) else {}
    cfg_data = (cfg.get("data") or {}) if isinstance(cfg, dict) else {}
    gains = {
        "kp": cfg_options.get("pid_kp", cfg_data.get("pid_kp")),
        "ki": cfg_options.get("pid_ki", cfg_data.get("pid_ki")),
        "kd": cfg_options.get("pid_kd", cfg_data.get("pid_kd")),
        "min_step": cfg_options.get("pid_min_power_step", cfg_data.get("pid_min_power_step")),
        "min_interval": cfg_options.get("pid_min_adjust_interval", cfg_data.get("pid_min_adjust_interval")),
    }

    hist = index_history(data.get("history", []))

    ext_key = external or f"sensor.{slug}_temperature"
    pv_raw = numeric_points(hist.get(ext_key, []))
    pv_unit = series_unit(hist.get(ext_key, []))
    pv = to_celsius(pv_raw, pv_unit)

    # Prefer number entity (authoritative °C setpoint) over sensor (may be °F-displayed)
    tgt_num_key = f"number.{slug}_pid_target_temperature"
    tgt_sen_key = f"sensor.{slug}_pid_target_temperature"
    tgt_points = numeric_points(hist.get(tgt_num_key, []))
    tgt_unit = series_unit(hist.get(tgt_num_key, []))
    if not tgt_points:
        tgt_points = numeric_points(hist.get(tgt_sen_key, []))
        tgt_unit = series_unit(hist.get(tgt_sen_key, []))
    tgt = to_celsius(tgt_points, tgt_unit)

    # Fall back to current_states snapshot if no target history was captured
    if not tgt:
        cs = data.get("current_states", {}) or {}
        for key in (tgt_num_key, tgt_sen_key):
            st = cs.get(key)
            if not isinstance(st, dict):
                continue
            try:
                v = float(st.get("state"))
            except (TypeError, ValueError):
                continue
            u = (st.get("attributes") or {}).get("unit_of_measurement")
            v_c = f_to_c(v) if (u and u.lower() in ("°f", "f", "fahrenheit")) else v
            if pv:
                tgt = [(pv[0][0], v_c), (pv[-1][0], v_c)]
            break

    integral = numeric_points(hist.get(f"sensor.{slug}_pid_integral", []))
    prop = numeric_points(hist.get(f"sensor.{slug}_pid_proportional", []))
    deriv = numeric_points(hist.get(f"sensor.{slug}_pid_derivative", []))
    requested = numeric_points(hist.get(f"sensor.{slug}_pid_requested_output", []))
    output = numeric_points(hist.get(f"sensor.{slug}_pid_output", []))
    power_limit = numeric_points(hist.get(f"sensor.{slug}_power_limit", []))
    power_cons = numeric_points(hist.get(f"sensor.{slug}_power_consumption", []))

    safety = string_points(hist.get(f"binary_sensor.{slug}_pid_safety_engaged", []))
    mining = string_points(hist.get(f"binary_sensor.{slug}_mining_status", []))

    # Demand entities: capture script appends configured climate entities to
    # history, plus a top-level "demand_entities" list. Compute the fraction
    # of the window where at least one was reporting hvac_action == "heating".
    demand_ids: list[str] = list(data.get("demand_entities") or [])
    demand_in_window_pct: float | None = None
    if demand_ids and pv:
        window_start, window_end = pv[0][0], pv[-1][0]
        # Build a per-entity timeline of hvac_action ("heating" vs other)
        per_ent_timelines = []
        for eid in demand_ids:
            series = hist.get(eid, [])
            timeline = []
            for entry in series:
                t = entry.get("last_changed") or entry.get("last_updated")
                if not t:
                    continue
                try:
                    ts = dt.datetime.fromisoformat(t)
                except ValueError:
                    continue
                attrs = entry.get("attributes") or {}
                action = attrs.get("hvac_action") or entry.get("state")
                timeline.append((ts, str(action) if action is not None else ""))
            per_ent_timelines.append(timeline)

        # Sample at 60s grid; cheap enough for 12h windows
        grid_step = 60.0
        total_secs = (window_end - window_start).total_seconds()
        n_steps = max(1, int(total_secs / grid_step))
        any_heating_steps = 0
        for i in range(n_steps):
            t = window_start + dt.timedelta(seconds=i * grid_step)
            for tl in per_ent_timelines:
                # Last state at-or-before t
                last = None
                for ts, val in tl:
                    if ts <= t:
                        last = val
                    else:
                        break
                if last == "heating":
                    any_heating_steps += 1
                    break
        demand_in_window_pct = 100.0 * any_heating_steps / n_steps

    report: dict[str, Any] = {
        "path": path,
        "slug": slug,
        "external": external,
        "window": window,
        "gains": gains,
        "counts": {
            "pv": len(pv),
            "integral": len(integral),
            "proportional": len(prop),
            "derivative": len(deriv),
            "requested": len(requested),
            "output": len(output),
            "power_limit": len(power_limit),
            "power_consumption": len(power_cons),
        },
        "median_dt_pv_sec": median_dt_seconds(pv),
    }

    if not pv:
        report["error"] = f"No PV samples under {ext_key}"
        return report

    duration_s = (pv[-1][0] - pv[0][0]).total_seconds()
    report["duration_min"] = duration_s / 60.0
    report["pv_start_c"] = pv[0][1]
    report["pv_end_c"] = pv[-1][1]

    pv_window = (pv[0][0], pv[-1][0])
    segments = detect_setpoint_segments(tgt, pv_window) if tgt else []
    report["setpoint_changes"] = max(0, len(segments) - 1)

    seg_reports: list[dict[str, Any]] = []
    for idx, (t0, t1, sp) in enumerate(segments):
        seg_pv = slice_by_time(pv, t0, t1)
        if len(seg_pv) < 5:
            continue
        errors = [sp - v for _, v in seg_pv]
        abs_err = [abs(e) for e in errors]
        overshoot = min(errors)  # most negative error = biggest overshoot above SP
        undershoot = max(errors)  # most positive error = biggest undershoot below SP
        tail_start = int(len(seg_pv) * 0.7)
        tail = errors[tail_start:]
        tail_mean = S.mean(tail) if tail else 0.0
        tail_std = S.stdev(tail) if len(tail) > 1 else 0.0
        tail_pv = [v for _, v in seg_pv[tail_start:]]
        pk_pk = (max(tail_pv) - min(tail_pv)) if tail_pv else 0.0
        seg_reports.append(
            {
                "idx": idx,
                "setpoint_c": sp,
                "duration_min": (t1 - t0).total_seconds() / 60.0,
                "n_samples": len(seg_pv),
                "pv_start_c": seg_pv[0][1],
                "pv_end_c": seg_pv[-1][1],
                "peak_overshoot_c": -overshoot if overshoot < 0 else 0.0,
                "peak_undershoot_c": undershoot if undershoot > 0 else 0.0,
                "steady_mean_err_c": tail_mean,
                "steady_std_err_c": tail_std,
                "steady_pk_pk_c": pk_pk,
                "zero_crossings": zero_crossings(errors),
                "settle_1c_sec": settling_time_seconds(seg_pv, sp, 1.0),
                "settle_2c_sec": settling_time_seconds(seg_pv, sp, 2.0),
                "mean_abs_err_c": S.mean(abs_err),
            }
        )
    report["segments"] = seg_reports

    # Saturation (use requested vs power bounds — assume 1000/5000 if config missing)
    power_min = 1000
    power_max = 5000
    if requested:
        sat_hi = sum(1 for _, v in requested if v >= power_max)
        sat_lo = sum(1 for _, v in requested if v <= power_min)
        report["saturation"] = {
            "pct_hi": 100.0 * sat_hi / len(requested),
            "pct_lo": 100.0 * sat_lo / len(requested),
            "mean_requested_w": S.mean([v for _, v in requested]),
            "median_requested_w": S.median([v for _, v in requested]),
        }
    if output and requested:
        # Align by nearest timestamp
        out_vals = sorted(output)
        pairs = 0
        diverge = 0
        j = 0
        for t_req, v_req in requested:
            # advance j to closest output
            while j + 1 < len(out_vals) and abs((out_vals[j + 1][0] - t_req).total_seconds()) < abs((out_vals[j][0] - t_req).total_seconds()):
                j += 1
            v_out = out_vals[j][1]
            if abs((out_vals[j][0] - t_req).total_seconds()) > 300:
                continue
            pairs += 1
            if abs(v_out - v_req) > 1.0:
                diverge += 1
        report["output_clamping_pct"] = (100.0 * diverge / pairs) if pairs else 0.0

    # Integral trajectory
    if integral:
        vals = [v for _, v in integral]
        report["integral"] = {
            "start": integral[0][1],
            "end": integral[-1][1],
            "min": min(vals),
            "max": max(vals),
            "range": max(vals) - min(vals),
        }

    # Term balance — use tail of run
    def tail_mean_abs(series: list[Point]) -> float:
        if not series:
            return 0.0
        tail = series[int(len(series) * 0.7):]
        return S.mean([abs(v) for _, v in tail]) if tail else 0.0

    report["term_balance"] = {
        "mean_abs_p_tail": tail_mean_abs(prop),
        "mean_abs_i_tail": tail_mean_abs(integral),
        "mean_abs_d_tail": tail_mean_abs(deriv),
    }

    # Actuation
    if power_limit:
        steps = [abs(power_limit[i][1] - power_limit[i - 1][1]) for i in range(1, len(power_limit))]
        nonzero = [s for s in steps if s > 0]
        report["actuation"] = {
            "changes": len(nonzero),
            "mean_step_w": S.mean(nonzero) if nonzero else 0.0,
            "max_step_w": max(nonzero) if nonzero else 0.0,
            "n_power_limit_zero": sum(1 for _, v in power_limit if v == 0),
        }

    # Safety + mining
    report["safety_engaged_events"] = sum(1 for _, s in safety if s == "on")
    report["mining_off_events"] = sum(1 for _, s in mining if s == "off")
    report["demand_entities"] = demand_ids
    report["demand_in_window_pct"] = demand_in_window_pct
    if power_cons:
        report["power_consumption_mean_w"] = S.mean([v for _, v in power_cons])

    return report


def fmt(v: float | None, fmt_spec: str = "+.2f") -> str:
    if v is None:
        return "—"
    return format(v, fmt_spec)


def print_report(r: dict[str, Any]) -> None:
    print(f"\n=== {r['path']} ===")
    if "error" in r:
        print(f"ERROR: {r['error']}")
        return
    w = r["window"]
    print(f"Slug: {r['slug']}   External: {r['external']}")
    print(f"Window: {w.get('start')} → {w.get('end')}  ({w.get('minutes')} min requested)")
    print(f"Duration (PV): {r['duration_min']:.1f} min   median dt: {r['median_dt_pv_sec']:.1f}s   samples: {r['counts']['pv']}")
    g = r["gains"]
    print(f"Gains (from config): Kp={g.get('kp')}  Ki={g.get('ki')}  Kd={g.get('kd')}   min_step={g.get('min_step')}W  min_interval={g.get('min_interval')}s")
    print(f"PV: start={r['pv_start_c']:.2f}°C  end={r['pv_end_c']:.2f}°C")
    print(f"Setpoint changes in window: {r['setpoint_changes']}")

    for seg in r.get("segments", []):
        print()
        print(f"  Segment #{seg['idx']}: SP={seg['setpoint_c']:.2f}°C  dur={seg['duration_min']:.1f} min  n={seg['n_samples']}")
        print(f"    PV: {seg['pv_start_c']:.2f} → {seg['pv_end_c']:.2f} °C")
        print(f"    Peak overshoot: {seg['peak_overshoot_c']:.2f}°C   peak undershoot: {seg['peak_undershoot_c']:.2f}°C")
        print(f"    Steady state (last 30%): mean err {seg['steady_mean_err_c']:+.2f}°C   std {seg['steady_std_err_c']:.2f}°C   pk-pk {seg['steady_pk_pk_c']:.2f}°C")
        print(f"    Settle to ±1°C: {fmt(seg['settle_1c_sec'], '.0f') if seg['settle_1c_sec'] is not None else 'never'}s   ±2°C: {fmt(seg['settle_2c_sec'], '.0f') if seg['settle_2c_sec'] is not None else 'never'}s")
        print(f"    Error zero-crossings: {seg['zero_crossings']}   mean |err|: {seg['mean_abs_err_c']:.2f}°C")

    if "saturation" in r:
        s = r["saturation"]
        print()
        print(f"Saturation (requested vs [1000, 5000]W):  hi={s['pct_hi']:.1f}%  lo={s['pct_lo']:.1f}%  mean={s['mean_requested_w']:.0f}W")
    if "output_clamping_pct" in r:
        print(f"Output ≠ requested (safety/throttle clamping): {r['output_clamping_pct']:.1f}% of samples")

    if "integral" in r:
        i = r["integral"]
        print(f"Integral trajectory: start={i['start']:.0f}  end={i['end']:.0f}  min={i['min']:.0f}  max={i['max']:.0f}  range={i['range']:.0f}")

    tb = r["term_balance"]
    ratio = (tb["mean_abs_i_tail"] / tb["mean_abs_p_tail"]) if tb["mean_abs_p_tail"] > 0 else float("inf")
    flag = "  ← I dominates" if ratio > 2.0 else ""
    print(f"Term balance (tail mean |·|): P={tb['mean_abs_p_tail']:.0f}W  I={tb['mean_abs_i_tail']:.0f}W  D={tb['mean_abs_d_tail']:.2f}W   I/P={ratio:.1f}{flag}")

    if "actuation" in r:
        a = r["actuation"]
        print(f"Actuation: {a['changes']} changes  mean step {a['mean_step_w']:.0f}W  max step {a['max_step_w']:.0f}W  power=0 samples {a['n_power_limit_zero']}")
    if "power_consumption_mean_w" in r:
        print(f"Mean power consumption: {r['power_consumption_mean_w']:.0f}W")
    print(f"Safety-engaged events: {r['safety_engaged_events']}   mining-off events: {r['mining_off_events']}")
    if r.get("demand_entities"):
        pct = r.get("demand_in_window_pct")
        pct_s = f"{pct:.1f}%" if pct is not None else "—"
        print(f"Demand entities ({pct_s} of window in active heating): {', '.join(r['demand_entities'])}")


def compare(a: dict[str, Any], b: dict[str, Any]) -> None:
    def cell(x, fmtspec="+.2f"):
        return fmt(x, fmtspec) if isinstance(x, (int, float)) else str(x)

    def seg_tail(r):
        segs = r.get("segments", [])
        return max(segs, key=lambda s: s["duration_min"]) if segs else {}

    sa, sb = seg_tail(a), seg_tail(b)
    rows = [
        ("File",                     a["path"].split("/")[-1], b["path"].split("/")[-1]),
        ("Duration (min)",           f"{a.get('duration_min',0):.0f}", f"{b.get('duration_min',0):.0f}"),
        ("Setpoint segments",        a.get("setpoint_changes", 0) + 1, b.get("setpoint_changes", 0) + 1),
        ("Largest seg SP (°C)",      cell(sa.get("setpoint_c")), cell(sb.get("setpoint_c"))),
        ("  Peak overshoot",         cell(sa.get("peak_overshoot_c"), ".2f"), cell(sb.get("peak_overshoot_c"), ".2f")),
        ("  Steady mean err",        cell(sa.get("steady_mean_err_c")), cell(sb.get("steady_mean_err_c"))),
        ("  Steady std err",         cell(sa.get("steady_std_err_c"), ".2f"), cell(sb.get("steady_std_err_c"), ".2f")),
        ("  Steady pk-pk",           cell(sa.get("steady_pk_pk_c"), ".2f"), cell(sb.get("steady_pk_pk_c"), ".2f")),
        ("Sat hi %",                 cell((a.get("saturation") or {}).get("pct_hi"), ".1f"), cell((b.get("saturation") or {}).get("pct_hi"), ".1f")),
        ("Sat lo %",                 cell((a.get("saturation") or {}).get("pct_lo"), ".1f"), cell((b.get("saturation") or {}).get("pct_lo"), ".1f")),
        ("Integral max",             cell((a.get("integral") or {}).get("max"), ".0f"), cell((b.get("integral") or {}).get("max"), ".0f")),
        ("Safety events",            a.get("safety_engaged_events", 0), b.get("safety_engaged_events", 0)),
        ("Mining-off events",        a.get("mining_off_events", 0), b.get("mining_off_events", 0)),
    ]
    width = max(len(str(row[0])) for row in rows)
    print("\n=== Comparison ===")
    for label, va, vb in rows:
        print(f"  {str(label):<{width}}  |  {str(va):>24}  |  {str(vb):>24}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="+", help="one or two pid-run-*.json files")
    args = ap.parse_args(argv)

    if len(args.files) > 2:
        print("error: pass at most 2 files", file=sys.stderr)
        return 2

    reports = [analyze(p) for p in args.files]
    for r in reports:
        print_report(r)
    if len(reports) == 2:
        compare(reports[0], reports[1])
    return 0


if __name__ == "__main__":
    sys.exit(main())
