"""Braiins Pool account data over its read-only web API.

Docs: https://academy.braiins.com/en/braiins-pool/monitoring/ — an access
profile token goes in the ``Pool-Auth-Token`` header and roughly one request
per 5 s is the documented safe rate. We fetch the account profile and the
worker list every 5 min, so two requests per poll.

The API reports hashrates as a number plus a unit string (usually ``Gh/s``)
and money as BTC strings; everything here is normalised to TH/s and float
BTC so the sensors and the dashboard see one unit each.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

try:
    import aiohttp
except ImportError:  # the pure test harness has no aiohttp; only the client needs it
    aiohttp = None

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

_LOGGER = logging.getLogger(__name__)

BASE_URL = "https://pool.braiins.com"
PROFILE_PATH = "/accounts/profile/json/btc/"
WORKERS_PATH = "/accounts/workers/json/btc"
AUTH_HEADER = "Pool-Auth-Token"
SCAN_INTERVAL = timedelta(minutes=5)
REQUEST_TIMEOUT_S = 20
# Gap between the two requests of one poll, to stay under the documented rate.
REQUEST_GAP_S = 2.0

WORKER_STATES = ["ok", "low", "off", "dis"]

_HASH_RATE_TO_TH = {
    "h/s": 1e-12,
    "kh/s": 1e-9,
    "mh/s": 1e-6,
    "gh/s": 1e-3,
    "th/s": 1.0,
    "ph/s": 1e3,
    "eh/s": 1e6,
}

PROFILE_HASH_RATE_KEYS = ("hash_rate_5m", "hash_rate_60m", "hash_rate_24h", "hash_rate_scoring")
PROFILE_BTC_KEYS = ("today_reward", "estimated_reward", "current_balance", "all_time_reward")
PROFILE_COUNT_KEYS = ("ok_workers", "low_workers", "off_workers", "dis_workers")
WORKER_HASH_RATE_KEYS = ("hash_rate_5m", "hash_rate_60m", "hash_rate_24h", "hash_rate_scoring")
WORKER_COUNT_KEYS = ("shares_5m", "shares_60m", "shares_24h")


class BraiinsPoolError(Exception):
    """The pool API could not be read."""


class BraiinsPoolAuthError(BraiinsPoolError):
    """The pool rejected the access token."""


def to_th(value: Any, unit: str | None) -> float | None:
    """Convert a pool hashrate (number + unit string) to TH/s."""
    if value is None or unit is None:
        return None
    factor = _HASH_RATE_TO_TH.get(str(unit).strip().lower())
    if factor is None:
        return None
    try:
        return float(value) * factor
    except (TypeError, ValueError):
        return None


def to_btc(value: Any) -> float | None:
    """BTC amounts arrive as strings ("0.00012345"); None stays None."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _unwrap(raw: Any) -> dict:
    """Responses are wrapped in a coin key: {"btc": {...}}."""
    if not isinstance(raw, dict):
        return {}
    inner = raw.get("btc")
    return inner if isinstance(inner, dict) else raw


def parse_profile(raw: Any) -> dict[str, Any]:
    """Account-level numbers in TH/s, BTC and counts."""
    data = _unwrap(raw)
    unit = data.get("hash_rate_unit")
    out: dict[str, Any] = {"username": data.get("username")}
    for key in PROFILE_HASH_RATE_KEYS:
        out[key] = to_th(data.get(key), unit)
    for key in PROFILE_BTC_KEYS:
        out[key] = to_btc(data.get(key))
    for key in PROFILE_COUNT_KEYS:
        out[key] = _to_int(data.get(key))
    return out


def pick_worker(names: list[str], preferred: str | None) -> str | None:
    """Choose which pool worker is this miner.

    Pool worker keys are ``username.worker``. A configured name matches the
    whole key or the part after the dot; with nothing configured a
    single-worker account is unambiguous and anything else is left unset.
    """
    if preferred:
        wanted = preferred.strip()
        for name in names:
            if name == wanted:
                return name
        for name in names:
            if name.split(".", 1)[-1] == wanted:
                return name
        return None
    return names[0] if len(names) == 1 else None


def parse_workers(raw: Any, preferred: str | None) -> dict[str, Any]:
    """This miner's worker row (TH/s, counts, state) plus the account's worker list."""
    data = _unwrap(raw)
    workers = data.get("workers")
    if not isinstance(workers, dict):
        workers = {}
    names = sorted(workers)
    name = pick_worker(names, preferred)
    out: dict[str, Any] = {
        "worker": name,
        "workers": names,
        "workers_total": len(names),
        "state": None,
        "last_share": None,
    }
    for key in WORKER_HASH_RATE_KEYS + WORKER_COUNT_KEYS:
        out[key] = None
    if name is None:
        return out
    row = workers.get(name) or {}
    unit = row.get("hash_rate_unit")
    state = row.get("state")
    out["state"] = state if state in WORKER_STATES else None
    out["last_share"] = _to_int(row.get("last_share"))
    for key in WORKER_HASH_RATE_KEYS:
        out[key] = to_th(row.get(key), unit)
    for key in WORKER_COUNT_KEYS:
        out[key] = _to_int(row.get(key))
    return out


class BraiinsPoolClient:
    """Thin GET wrapper; the session is HA's shared aiohttp client."""

    def __init__(self, session: Any, token: str, base_url: str = BASE_URL) -> None:
        self._session = session
        self._token = token
        self._base_url = base_url.rstrip("/")

    async def _get(self, path: str) -> Any:
        if aiohttp is None:
            raise BraiinsPoolError("aiohttp is required for the Braiins Pool client")
        url = f"{self._base_url}{path}"
        try:
            async with self._session.get(
                url,
                headers={AUTH_HEADER: self._token},
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_S),
            ) as resp:
                if resp.status in (401, 403):
                    raise BraiinsPoolAuthError(f"token rejected ({resp.status})")
                if resp.status != 200:
                    raise BraiinsPoolError(f"HTTP {resp.status} from {path}")
                return await resp.json(content_type=None)
        except BraiinsPoolError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as err:
            raise BraiinsPoolError(f"{path}: {err!r}") from err

    async def profile(self) -> dict[str, Any]:
        return await self._get(PROFILE_PATH)

    async def workers(self) -> dict[str, Any]:
        return await self._get(WORKERS_PATH)


class BraiinsPoolCoordinator(DataUpdateCoordinator):
    """Polls the pool account every 5 min; data = {"profile": {...}, "worker": {...}}."""

    def __init__(
        self, hass: HomeAssistant, client: BraiinsPoolClient, worker: str | None, name: str
    ) -> None:
        super().__init__(
            hass, _LOGGER, name=f"{name} Braiins Pool", update_interval=SCAN_INTERVAL
        )
        self.client = client
        self.worker = worker
        self._worker_warned = False

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            profile_raw = await self.client.profile()
            await asyncio.sleep(REQUEST_GAP_S)
            workers_raw = await self.client.workers()
        except BraiinsPoolAuthError as err:
            raise UpdateFailed(f"Braiins Pool rejected the access token: {err}") from err
        except BraiinsPoolError as err:
            raise UpdateFailed(f"Braiins Pool unreachable: {err}") from err
        profile = parse_profile(profile_raw)
        worker = parse_workers(workers_raw, self.worker)
        if worker["worker"] is None and worker["workers"] and not self._worker_warned:
            self._worker_warned = True
            _LOGGER.warning(
                "Braiins Pool account has %d workers (%s) and none matches %r — set the "
                "pool worker name in Configure to get per-worker sensors",
                worker["workers_total"], ", ".join(worker["workers"]), self.worker,
            )
        return {"profile": profile, "worker": worker}
