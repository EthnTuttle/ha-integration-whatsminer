"""Braiins Pool parsing and the coordinator's merge/error handling.

The API shapes come from https://academy.braiins.com/en/braiins-pool/monitoring/.
No network: the client is replaced by a fake with canned responses.
"""
from __future__ import annotations

import importlib
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_controller_smoke import run_async  # noqa: E402  (installs the HA stubs)

braiins = importlib.import_module("wm.braiins")
UpdateFailed = sys.modules["homeassistant.helpers.update_coordinator"].UpdateFailed

PROFILE = {
    "btc": {
        "username": "garyray-k",
        "all_time_reward": "0.01234567",
        "hash_rate_unit": "Gh/s",
        "hash_rate_5m": 103000.0,
        "hash_rate_60m": 101000.5,
        "hash_rate_24h": 98000.0,
        "hash_rate_scoring": 100500.0,
        "ok_workers": 1,
        "low_workers": 0,
        "off_workers": 1,
        "dis_workers": 0,
        "current_balance": "0.00012345",
        "today_reward": "0.00001234",
        "estimated_reward": "0.00005000",
    }
}

WORKERS = {
    "btc": {
        "workers": {
            "garyray-k.heater": {
                "state": "ok",
                "last_share": 1791353877,
                "hash_rate_unit": "Gh/s",
                "hash_rate_scoring": 100000.0,
                "hash_rate_5m": 103000.0,
                "hash_rate_60m": 101000.0,
                "hash_rate_24h": 98000.0,
                "shares_5m": 120,
                "shares_60m": 1400,
                "shares_24h": 33000,
            },
            "garyray-k.spare": {
                "state": "off",
                "last_share": 1791000000,
                "hash_rate_unit": "Gh/s",
                "hash_rate_scoring": 0,
                "hash_rate_5m": 0,
                "hash_rate_60m": 0,
                "hash_rate_24h": 0,
                "shares_5m": 0,
                "shares_60m": 0,
                "shares_24h": 0,
            },
        }
    }
}


def test_profile_normalises_units_and_money():
    p = braiins.parse_profile(PROFILE)
    assert p["username"] == "garyray-k"
    assert p["hash_rate_5m"] == pytest.approx(103.0)
    assert p["hash_rate_60m"] == pytest.approx(101.0005)
    assert p["hash_rate_24h"] == pytest.approx(98.0)
    assert p["today_reward"] == pytest.approx(0.00001234)
    assert p["current_balance"] == pytest.approx(0.00012345)
    assert p["all_time_reward"] == pytest.approx(0.01234567)
    assert p["estimated_reward"] == pytest.approx(0.00005)
    assert p["ok_workers"] == 1 and p["off_workers"] == 1


def test_profile_tolerates_missing_fields_and_unknown_units():
    p = braiins.parse_profile({"btc": {"username": "x", "hash_rate_unit": "furlongs", "hash_rate_5m": 1}})
    assert p["username"] == "x"
    assert p["hash_rate_5m"] is None
    assert p["today_reward"] is None and p["ok_workers"] is None
    assert braiins.parse_profile(None) == braiins.parse_profile({})
    assert braiins.parse_profile({"username": "unwrapped"})["username"] == "unwrapped"


@pytest.mark.parametrize(
    "unit,factor",
    [("Gh/s", 1e-3), ("Th/s", 1.0), ("Ph/s", 1e3), ("Mh/s", 1e-6), ("h/s", 1e-12), ("TH/S", 1.0)],
)
def test_to_th_units(unit, factor):
    assert braiins.to_th(1000, unit) == pytest.approx(1000 * factor)


def test_to_btc_and_bad_values():
    assert braiins.to_btc("0.5") == 0.5
    assert braiins.to_btc("") is None
    assert braiins.to_btc("abc") is None
    assert braiins.to_th("abc", "Gh/s") is None
    assert braiins.to_th(1, None) is None


def test_worker_matched_by_suffix():
    w = braiins.parse_workers(WORKERS, "heater")
    assert w["worker"] == "garyray-k.heater"
    assert w["state"] == "ok"
    assert w["last_share"] == 1791353877
    assert w["hash_rate_5m"] == pytest.approx(103.0)
    assert w["hash_rate_24h"] == pytest.approx(98.0)
    assert w["shares_24h"] == 33000
    assert w["workers"] == ["garyray-k.heater", "garyray-k.spare"]
    assert w["workers_total"] == 2


def test_worker_matched_by_full_name():
    assert braiins.parse_workers(WORKERS, "garyray-k.spare")["state"] == "off"


def test_worker_unmatched_leaves_row_empty_but_lists_workers():
    w = braiins.parse_workers(WORKERS, "nope")
    assert w["worker"] is None and w["state"] is None and w["hash_rate_5m"] is None
    assert w["workers_total"] == 2
    # Two workers and nothing configured: ambiguous, so unset.
    assert braiins.parse_workers(WORKERS, None)["worker"] is None


def test_single_worker_account_needs_no_name():
    only = {"btc": {"workers": {"garyray-k.heater": WORKERS["btc"]["workers"]["garyray-k.heater"]}}}
    assert braiins.parse_workers(only, None)["worker"] == "garyray-k.heater"
    assert braiins.parse_workers(only, "")["worker"] == "garyray-k.heater"


def test_worker_unknown_state_is_none():
    raw = {"btc": {"workers": {"u.w": {"state": "weird", "hash_rate_unit": "Gh/s", "hash_rate_5m": 10}}}}
    w = braiins.parse_workers(raw, "w")
    assert w["state"] is None and w["hash_rate_5m"] == pytest.approx(0.01)


def test_workers_missing_or_malformed():
    assert braiins.parse_workers({}, "heater")["workers_total"] == 0
    assert braiins.parse_workers({"btc": {"workers": []}}, None)["worker"] is None


class FakeClient:
    def __init__(self, profile=PROFILE, workers=WORKERS, fail=None):
        self._profile, self._workers, self._fail = profile, workers, fail
        self.calls = []

    async def profile(self):
        self.calls.append("profile")
        if self._fail is not None:
            raise self._fail
        return self._profile

    async def workers(self):
        self.calls.append("workers")
        return self._workers


def _coordinator(monkeypatch, client, worker="heater"):
    monkeypatch.setattr(braiins, "REQUEST_GAP_S", 0)
    return braiins.BraiinsPoolCoordinator(object(), client, worker, "heatcore")


def test_coordinator_merges_profile_and_worker(monkeypatch):
    client = FakeClient()
    coord = _coordinator(monkeypatch, client)
    data = run_async(coord._async_update_data())
    assert client.calls == ["profile", "workers"]
    assert data["profile"]["hash_rate_5m"] == pytest.approx(103.0)
    assert data["worker"]["worker"] == "garyray-k.heater"


def test_coordinator_auth_error_is_update_failed(monkeypatch):
    coord = _coordinator(monkeypatch, FakeClient(fail=braiins.BraiinsPoolAuthError("401")))
    with pytest.raises(UpdateFailed, match="rejected the access token"):
        run_async(coord._async_update_data())


def test_coordinator_network_error_is_update_failed(monkeypatch):
    coord = _coordinator(monkeypatch, FakeClient(fail=braiins.BraiinsPoolError("timeout")))
    with pytest.raises(UpdateFailed, match="unreachable"):
        run_async(coord._async_update_data())


def test_coordinator_warns_once_when_no_worker_matches(monkeypatch, caplog):
    coord = _coordinator(monkeypatch, FakeClient(), worker="nope")
    run_async(coord._async_update_data())
    run_async(coord._async_update_data())
    assert sum("none matches" in r.message for r in caplog.records) == 1
