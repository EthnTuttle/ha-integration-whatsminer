#!/usr/bin/env python3
"""Export a storage-mode Lovelace dashboard to YAML for version control.

Usage: export-dashboard.py [URL_PATH] [OUT_FILE]
Defaults: heatcore-overview -> dashboards/heatcore-overview.yaml
HA_URL and HA_TOKEN come from the environment, or from an env file named by
HA_ENV_FILE. The exported file is a reference copy; HA does not load it.
"""
import asyncio
import json
import os
import sys
from pathlib import Path

import websockets
import yaml


def load_env() -> tuple[str, str]:
    env = dict(os.environ)
    env_file = env.get("HA_ENV_FILE")
    if env_file and Path(env_file).exists():
        for line in Path(env_file).read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env.setdefault(k.strip(), v.strip().strip("'\""))
    return env["HA_URL"].rstrip("/"), env["HA_TOKEN"]


async def fetch(url_path: str) -> dict:
    base, token = load_env()
    ws_url = base.replace("https://", "wss://").replace("http://", "ws://") + "/api/websocket"
    async with websockets.connect(ws_url, max_size=None) as ws:
        await ws.recv()
        await ws.send(json.dumps({"type": "auth", "access_token": token}))
        if json.loads(await ws.recv()).get("type") != "auth_ok":
            raise SystemExit("auth failed")
        await ws.send(json.dumps({"id": 1, "type": "lovelace/config", "url_path": url_path}))
        resp = json.loads(await ws.recv())
        if not resp.get("success"):
            raise SystemExit(f"lovelace/config failed: {resp.get('error')}")
        return resp["result"]


def main() -> None:
    url_path = sys.argv[1] if len(sys.argv) > 1 else "heatcore-overview"
    out = Path(sys.argv[2] if len(sys.argv) > 2 else Path(__file__).parent.parent / "dashboards" / f"{url_path}.yaml")
    config = asyncio.run(fetch(url_path))
    header = (
        f"# Lovelace dashboard '{url_path}', exported from Home Assistant.\n"
        "# Reference copy only: HA stores this dashboard in .storage and does not read this file.\n"
        "# To restore: dashboard ⋮ → Edit → Raw configuration editor, paste, save.\n"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(header + yaml.safe_dump(config, sort_keys=False, allow_unicode=True, width=120))
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
