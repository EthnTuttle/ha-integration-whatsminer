#!/usr/bin/env python3
"""Capture a PID tuning run from Home Assistant as a single JSON bundle.

Usage:
    HA='http://homeassistant.local:8123' TOKEN='...' \\
        ./scripts/pid-capture.py [--minutes N] [--external ENTITY_ID] [--slug heatcore]

Output:
    pid-run-YYYYMMDD-HHMMSS.json  — one file containing:
        - metadata (slug, external sensor, window, timestamps)
        - current states of all relevant entities (target temp, PID internals, etc.)
        - config-entry options if the WS API is reachable (Kp/Ki/Kd/interval/step)
        - history window for all captured series

Paste the file in chat. That's it — no other values to copy.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import urllib.parse
import urllib.request


def env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        print(f"error: {name} not set in environment", file=sys.stderr)
        sys.exit(1)
    return val


def http_get(url: str, token: str) -> bytes:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def http_get_json(url: str, token: str):
    return json.loads(http_get(url, token))


def discover_external_sensor(ha: str, token: str, slug: str) -> str | None:
    """First-pass guess: prefer entity_ids ending in _temperature_probe over board/esp."""
    states = http_get_json(f"{ha}/api/states", token)
    cands = [
        s["entity_id"]
        for s in states
        if s.get("attributes", {}).get("device_class") == "temperature"
        and slug not in s["entity_id"]
        and s.get("state") not in ("unknown", "unavailable", None)
    ]
    probe = [e for e in cands if e.endswith("_temperature_probe")]
    if probe:
        return probe[0]
    return cands[0] if cands else None


def fetch_config_entry_options(ha: str, token: str) -> dict | None:
    """Try the WS API to grab the whatsminer config-entry options (Kp/Ki/Kd/etc)."""
    try:
        import asyncio

        try:
            import websockets  # type: ignore
        except ImportError:
            return None

        async def _go():
            ws_url = ha.replace("http://", "ws://").replace("https://", "wss://") + "/api/websocket"
            # Default websockets 16.x open_timeout is 10s — too short for HA when
            # the supervisor is busy. Bump to 30s and use ping_timeout so dropped
            # connections fail fast rather than hanging the whole capture.
            async with websockets.connect(
                ws_url, open_timeout=30, ping_timeout=15
            ) as ws:
                await ws.recv()  # auth_required
                await ws.send(json.dumps({"type": "auth", "access_token": token}))
                await ws.recv()  # auth_ok
                await ws.send(json.dumps({"id": 1, "type": "config_entries/get"}))
                resp = json.loads(await ws.recv())
                entries = resp.get("result", [])
                for e in entries:
                    if e.get("domain") == "whatsminer":
                        return {"data": e.get("data", {}), "options": e.get("options", {})}
                return None

        return asyncio.run(_go())
    except Exception as err:
        return {"_error": f"{type(err).__name__}: {err}"}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--minutes", type=int, default=45)
    ap.add_argument("--external", help="external sensor entity_id (auto-discover if omitted)")
    ap.add_argument("--slug", default=os.environ.get("SLUG", "heatcore"))
    ap.add_argument(
        "--demand",
        action="append",
        default=[],
        help="climate entity_id to capture as demand input (repeatable; "
             "auto-pulled from config-entry options if WS API is reachable)",
    )
    args = ap.parse_args()

    ha = env("HA").rstrip("/")
    token = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJmMjcyOGU2MGJlZDk0NmUyOTM4OWFmZmJhYmQ5ODk1NyIsImlhdCI6MTc3NzAzMTI0NSwiZXhwIjoyMDkyMzkxMjQ1fQ.6u3ZtX5vJA3ENRlPmgUo_N455V0QIjrfFepbk6KkoY8"
    slug = args.slug

    now = dt.datetime.now(dt.timezone.utc)
    start = now - dt.timedelta(minutes=args.minutes)
    start_s = start.strftime("%Y-%m-%dT%H:%M:%S+00:00")
    end_s = now.strftime("%Y-%m-%dT%H:%M:%S+00:00")

    external = args.external or discover_external_sensor(ha, token, slug)

    base = [
        f"sensor.{slug}_power_limit",
        f"sensor.{slug}_power_consumption",
        f"sensor.{slug}_temperature",
        f"sensor.{slug}_pid_error",
        f"sensor.{slug}_pid_proportional",
        f"sensor.{slug}_pid_integral",
        f"sensor.{slug}_pid_derivative",
        f"sensor.{slug}_pid_requested_output",
        f"sensor.{slug}_pid_output",
        f"sensor.{slug}_pid_target_temperature",
        f"sensor.{slug}_pid_demand_index",
        f"sensor.{slug}_pid_external_compensation",
        f"sensor.{slug}_pid_out_min_effective",
        f"sensor.{slug}_pid_out_max_effective",
        f"sensor.{slug}_pid_pv_slope",
        f"number.{slug}_pid_target_temperature",
        f"switch.{slug}_pid_mode",
        f"switch.{slug}_mining_control",
        f"binary_sensor.{slug}_pid_safety_engaged",
        f"binary_sensor.{slug}_mining_status",
    ]
    if external:
        base.append(external)

    # Config entry options (best-effort) — fetched here so we can also pull
    # the configured demand entities and capture their history alongside.
    cfg = fetch_config_entry_options(ha, token)
    cfg_demand: list[str] = []
    if isinstance(cfg, dict) and "_error" not in cfg:
        opts = cfg.get("options") or {}
        data = cfg.get("data") or {}
        cfg_demand = list(opts.get("pid_demand_entities") or data.get("pid_demand_entities") or [])

    demand_entities = list(dict.fromkeys(args.demand + cfg_demand))  # de-dup, preserve order
    base.extend(demand_entities)

    print(f"Window:   {start_s}  →  {end_s}  ({args.minutes} min)")
    print(f"Miner:    {slug}")
    print(f"External: {external or '(none)'}")
    print(f"Demand:   {', '.join(demand_entities) if demand_entities else '(none)'}")

    # Current states
    states = http_get_json(f"{ha}/api/states", token)
    by_id = {s["entity_id"]: s for s in states}
    current = {e: by_id.get(e) for e in base}

    if cfg is None:
        print("Config:   (websockets module not available — Kp/Ki/Kd/etc not captured)")
        print("          pip install websockets  # to enable")
    elif "_error" in cfg:
        print(f"Config:   WS API failed ({cfg['_error']})")
    else:
        print("Config:   captured Kp/Ki/Kd + throttle + demand options via WS API")

    # History
    ent_param = urllib.parse.quote(",".join(base), safe=",")
    hist_url = (
        f"{ha}/api/history/period/{urllib.parse.quote(start_s)}"
        f"?end_time={urllib.parse.quote(end_s)}"
        f"&filter_entity_id={ent_param}"
        f"&minimal_response"
    )
    history = http_get_json(hist_url, token)

    bundle = {
        "captured_at": now.isoformat(),
        "window": {"start": start_s, "end": end_s, "minutes": args.minutes},
        "miner_slug": slug,
        "external_sensor": external,
        "demand_entities": demand_entities,
        "config_entry": cfg,
        "current_states": current,
        "history": history,
    }

    out = f"pid-run-{now.strftime('%Y%m%d-%H%M%S')}.json"
    with open(out, "w") as f:
        json.dump(bundle, f, indent=2, default=str)

    size = os.path.getsize(out)
    series = len(history) if isinstance(history, list) else 0
    samples = sum(len(s) for s in history) if isinstance(history, list) else 0

    print()
    print(f"Captured: {series} series, {samples} samples, {size:,} bytes → {out}")
    print()
    print(f"Paste {out} into chat. Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
