"""API v3 mining stop/start: framing, tokens, the write unlock, v2 fallback.

Incident (2026-10-07): v2 power_off did not keep the M64 (20250409.15.REL)
off. btminer restarted and resumed hashing, so the supply lockout could not
stop it and the boiler loop reached 143°F. v3 set.miner.service stop holds.

Everything here talks to fake miners on 127.0.0.1; nothing reaches a real host.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import pathlib
import struct
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_controller_smoke import Rig, controller_mod, run_async  # noqa: E402

v3 = importlib.import_module("wm.api_v3")
coordinator_mod = importlib.import_module("wm.coordinator")

SALT = "Abc123xy"
UNLOCK_SALT = "BQ5hoXV9"
UNLOCK_NEWSALT = "nEwSaLt1"
UNLOCK_TIME = "1759800000"


class FakeMiner:
    """A v3 port (framed) and a v2 port (plain JSON) on loopback.

    Writes on v3 answer -4 until open_write_api succeeds on the v2 port with
    ``admin_password``. ``working`` follows set.miner.service. ``refuse_v3``
    drops v3 connections unanswered (btminer restarting). ``unlock_reply``
    is how the token is answered: "json", "text" or "none" (socket left open).
    """

    def __init__(
        self, admin_password="admin", super_password="super", locked=True, v3_up=True,
        unlock_reply="json",
    ):
        self.admin_password = admin_password
        self.super_password = super_password
        self.locked = locked
        self.v3_up = v3_up
        self.refuse_v3 = False
        self.unlock_reply = unlock_reply
        self.working = True
        self.v3_requests: list[dict] = []
        self.v2_requests: list[dict] = []
        self.unlock_attempts = 0
        self.services: list[str] = []
        self.v3_port = 0
        self.v2_port = 0
        self._servers: list[asyncio.base_events.Server] = []

    async def __aenter__(self):
        v2 = await asyncio.start_server(self._v2, "127.0.0.1", 0)
        self.v2_port = v2.sockets[0].getsockname()[1]
        self._servers.append(v2)
        v3_srv = await asyncio.start_server(self._v3, "127.0.0.1", 0)
        self.v3_port = v3_srv.sockets[0].getsockname()[1]
        if self.v3_up:
            self._servers.append(v3_srv)
        else:
            v3_srv.close()  # port now refuses connections
            await v3_srv.wait_closed()
        return self

    async def __aexit__(self, *exc):
        for srv in self._servers:
            srv.close()
            await srv.wait_closed()

    def api(self, admin_password="admin", super_password="super") -> "coordinator_mod.WhatsminerAPI":
        api = coordinator_mod.WhatsminerAPI("127.0.0.1", self.v2_port, admin_password, super_password)
        api.v3.port = self.v3_port
        api.v3.timeout = 2.0
        api.v2_sent: list[str] = []

        async def fake_v2_privileged(cmd, **kwargs):
            api.v2_sent.append(cmd)
            return {"STATUS": "S", "Msg": cmd}

        api.send_privileged_command = fake_v2_privileged
        return api

    # --- v3: 4-byte LE length + JSON -----------------------------------------
    async def _v3(self, reader, writer):
        try:
            (length,) = struct.unpack("<I", await reader.readexactly(4))
            req = json.loads(await reader.readexactly(length))
            self.v3_requests.append(req)
            if self.refuse_v3:
                return
            raw = json.dumps(self._v3_reply(req)).encode()
            writer.write(struct.pack("<I", len(raw)) + raw)
            await writer.drain()
        finally:
            writer.close()

    def _v3_reply(self, req):
        cmd = req.get("cmd")
        if cmd == "get.device.info" and req.get("param") == "salt":
            return {"code": 0, "when": 1, "msg": {"salt": SALT}}
        if cmd == "get.device.info" and req.get("param") == "miner":
            return {"code": 0, "msg": {"miner": {"working": "true" if self.working else "false"}}}
        if cmd == "set.miner.service":
            if self.locked:
                return {"code": -4, "msg": "no permission for write command"}
            expected = v3.v3_token(cmd, self.super_password, SALT, req["ts"])
            if req.get("account") != "super" or req.get("token") != expected:
                return {"code": -2, "msg": "invalid token"}
            self.services.append(req["param"])
            self.working = req["param"] == "start"
            return {"code": 0, "msg": "ok"}
        return {"code": -1, "msg": "unknown command"}

    # --- v2: plain JSON, open_write_api handshake ----------------------------
    async def _v2(self, reader, writer):
        try:
            req = await v3._read_json(reader, 2.0)
            self.v2_requests.append(req)
            if req.get("command") != "open_write_api":
                writer.write(json.dumps({"STATUS": "E", "Msg": "unexpected"}).encode())
                return
            self.unlock_attempts += 1
            writer.write(json.dumps({
                "STATUS": "S",
                "Msg": {"salt": UNLOCK_SALT, "newsalt": UNLOCK_NEWSALT, "time": UNLOCK_TIME},
            }).encode())
            await writer.drain()
            token = (await v3._read_json(reader, 2.0)).get("token")
            expected = v3.unlock_token(self.admin_password, UNLOCK_SALT, UNLOCK_NEWSALT, UNLOCK_TIME)
            if token == expected:
                self.locked = False
            if self.unlock_reply == "none":
                await reader.read()  # never answers; waits for the client to hang up
                return
            if self.unlock_reply == "text":
                writer.write(b"API command OK" if token == expected else b"invalid token")
            elif token == expected:
                writer.write(json.dumps({"STATUS": "S", "Msg": "API command OK"}).encode())
            else:
                writer.write(json.dumps({"STATUS": "E", "Msg": "invalid token"}).encode())
            await writer.drain()
        finally:
            writer.close()


# ------------------------------------------------------------ pure helpers


def test_frame_is_le_length_prefixed_json():
    frame = v3.encode_frame({"cmd": "get.device.info", "param": "miner"})
    (length,) = struct.unpack("<I", frame[:4])
    assert length == len(frame) - 4
    assert frame[:4] == len(frame[4:]).to_bytes(4, "little")
    assert json.loads(frame[4:]) == {"cmd": "get.device.info", "param": "miner"}


def test_v3_token_known_vector():
    # base64(sha256("set.miner.service" + "super" + "Abc123xy" + "1759800000"))[:8]
    assert v3.v3_token("set.miner.service", "super", "Abc123xy", 1759800000) == "Cg2iuKaj"


def test_unlock_token_known_vector():
    # md5(time + newsalt + magic + md5crypt("admin", "BQ5hoXV9").split("$")[3])
    assert (
        v3.unlock_token("admin", UNLOCK_SALT, UNLOCK_NEWSALT, UNLOCK_TIME)
        == "e8bfea07db7678e5b8c363b334a4f58d"
    )


def test_firmware_date_parsing():
    api = coordinator_mod.WhatsminerAPI("127.0.0.1", 4028)
    for fw, date in (("20250409.15.REL", 20250409), ("'20190912.15.REL'", 20190912), (None, None), ("x", None)):
        api.firmware_version = fw
        assert api._firmware_date() == date


def test_parse_status_surfaces_mineroff_and_firmware():
    parse = coordinator_mod.WhatsminerCoordinator._parse_status
    out = parse(None, {"STATUS": "S", "Msg": {
        "mineroff": "true", "mineroff_reason": "by whatsminer api",
        "Firmware Version": "'20250409.15.REL'",
    }})
    assert out == {"fw_ver": "20250409.15.REL", "miner_off": True, "miner_off_reason": "by whatsminer api"}
    assert parse(None, {"STATUS": "S", "Msg": {"btmineroff": "false"}}) == {
        "miner_off": False, "miner_off_reason": None,
    }
    assert parse(None, {"STATUS": "E", "Msg": "invalid cmd"}) == {}


# --------------------------------------------------------- client over TCP


def test_framed_read_over_tcp():
    async def go():
        async with FakeMiner() as miner:
            client = miner.api().v3
            assert await client.get_working() is True
            miner.working = False
            assert await client.get_working() is False
            assert miner.v3_requests == [{"cmd": "get.device.info", "param": "miner"}] * 2

    run_async(go())


def test_no_write_permission_unlocks_then_retries_once():
    async def go():
        async with FakeMiner(locked=True) as miner:
            resp = await miner.api().v3.set_miner_service("stop")
            assert resp["code"] == 0
            assert miner.unlock_attempts == 1
            assert miner.services == ["stop"]
            writes = [r for r in miner.v3_requests if r["cmd"] == "set.miner.service"]
            assert len(writes) == 2  # -4, then the retry
            assert all(r["account"] == "super" and len(r["token"]) == 8 for r in writes)
            assert miner.working is False

    run_async(go())


def test_unlock_falls_back_to_default_admin_password():
    async def go():
        async with FakeMiner(admin_password="admin") as miner:
            resp = await miner.api(admin_password="hunter2").v3.set_miner_service("stop")
            assert resp["code"] == 0
            assert miner.unlock_attempts == 2
            assert miner.services == ["stop"]

    run_async(go())


def test_unlock_rejected_raises_after_one_retry_per_password():
    async def go():
        async with FakeMiner(admin_password="other") as miner:
            with pytest.raises(v3.WhatsminerV3Error) as err:
                await miner.api(admin_password="hunter2").v3.set_miner_service("stop")
            assert err.value.code == v3.CODE_NO_WRITE_PERMISSION
            assert miner.unlock_attempts == 2
            # the first write, then one retry after each unlock
            assert len([r for r in miner.v3_requests if r["cmd"] == "set.miner.service"]) == 3
            assert miner.services == []

    run_async(go())


@pytest.mark.parametrize("reply", ["text", "none"])
def test_unlock_reply_format_does_not_decide(reply):
    """The retried write decides, not the token reply (asic-rs ignores it too)."""
    async def go():
        async with FakeMiner(locked=True, unlock_reply=reply) as miner:
            client = miner.api().v3
            client.timeout = 0.3
            resp = await client.set_miner_service("stop")
            assert resp["code"] == 0
            assert miner.unlock_attempts == 1
            assert miner.services == ["stop"]

    run_async(go())


# ------------------------------------------------- WhatsminerAPI routing


def probes(miner):
    return [r for r in miner.v3_requests if r == {"cmd": "get.device.info", "param": "miner"}]


def test_power_off_and_on_use_v3_without_a_probe_on_v3_firmware():
    async def go():
        async with FakeMiner(locked=False) as miner:
            api = miner.api()
            api.firmware_version = "20250409.15.REL"
            await api.power_off()
            assert miner.working is False
            await api.power_on()
            assert miner.working is True
            assert miner.services == ["stop", "start"]
            assert api.v2_sent == []
            assert api.control_api == "v3"
            assert api.v3_fallback is None
            assert probes(miner) == []  # the write's salt fetch is the probe

    run_async(go())


def test_unknown_firmware_probes_once_and_caches_detection():
    async def go():
        async with FakeMiner(locked=False) as miner:
            api = miner.api()
            await api.power_off()
            await api.power_on()
            assert miner.services == ["stop", "start"]
            assert len(probes(miner)) == 1

    run_async(go())


def test_v3_unavailable_falls_back_to_v2_and_reprobes_soon(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(coordinator_mod, "monotonic", lambda: clock[0])

    async def go():
        async with FakeMiner(v3_up=False) as miner:
            api = miner.api()
            await api.power_off()
            assert api.v2_sent == ["power_off"]
            assert api.control_api == "v2"
            assert api.v3_fallback is None  # firmware unknown: v2 may be all it has
            await api.power_on()  # cached miss: no second probe inside V3_REPROBE_S
            assert api.v2_sent == ["power_off", "power_on"]
            assert coordinator_mod.V3_REPROBE_S < controller_mod.LOCKOUT_REASSERT_INTERVAL
            clock[0] += coordinator_mod.V3_REPROBE_S
            calls = api.v3.probe
            reprobed = []

            async def counting_probe():
                reprobed.append(1)
                return await calls()

            api.v3.probe = counting_probe
            await api.power_off()
            assert reprobed == [1]

    run_async(go())


def test_v3_miss_is_not_cached_on_v3_firmware():
    """A refused 4433 (btminer restarting) must not pin later stops to v2 power_off."""
    async def go():
        async with FakeMiner(locked=False) as miner:
            api = miner.api()
            api.firmware_version = "20250409.15.REL"
            miner.refuse_v3 = True
            await api.power_off()
            assert api.v2_sent == ["power_off"]
            assert "v3 set.miner.service stop failed" in api.v3_fallback
            miner.refuse_v3 = False
            await api.power_off()  # the very next re-send tries v3 again
            assert miner.services == ["stop"]
            assert api.v2_sent == ["power_off"]
            assert api.v3_fallback is None
            assert api.control_api == "v3"

    run_async(go())


def test_start_after_a_v3_stop_insists_on_v3():
    """v2 power_on is not known to undo a v3 stop: a failed v3 start raises."""
    async def go():
        async with FakeMiner(locked=False) as miner:
            api = miner.api()  # firmware unknown: only the v3 stop says v3
            await api.power_off()
            assert miner.services == ["stop"]
            miner.refuse_v3 = True
            with pytest.raises(v3.WhatsminerV3Error):
                await api.power_on()
            assert api.v2_sent == ["power_on"]  # sent best-effort, not reported as success
            seen = len(miner.v3_requests)
            with pytest.raises(v3.WhatsminerV3Error):
                await api.power_on()  # a failed probe must not divert it to v2 only
            assert len(miner.v3_requests) > seen
            assert probes(miner)[1:] == []  # no probe gate in front of the start
            miner.refuse_v3 = False
            await api.power_on()
            assert miner.services == ["stop", "start"]
            assert miner.working is True
            assert api.v2_sent == ["power_on", "power_on"]

    run_async(go())


def test_status_mineroff_by_api_counts_as_a_v3_stop():
    async def go():
        async with FakeMiner(locked=False) as miner:
            api = miner.api()
            miner.refuse_v3 = True
            api.note_status(True, "by whatsminer api")
            with pytest.raises(v3.WhatsminerV3Error):
                await api.power_on()
            api.note_status(False, None)
            api._v3_available = False
            api._v3_checked_at = coordinator_mod.monotonic()
            assert await api.power_on() == {"STATUS": "S", "Msg": "power_on"}
            api.firmware_version = "20230601.22.REL"
            api.note_status(True, "by whatsminer api")  # old firmware: not a v3 stop
            assert await api.power_on() == {"STATUS": "S", "Msg": "power_on"}

    run_async(go())


def test_v3_start_failure_falls_back_to_v2_power_on():
    async def go():
        async with FakeMiner(locked=False, super_password="changed") as miner:
            api = miner.api(super_password="super")
            api.firmware_version = "20250409.15.REL"
            await api.power_on()
            assert api.v2_sent == ["power_on"]
            assert miner.services == []
            assert "v3 set.miner.service start failed" in api.v3_fallback
            assert api.control_api == "unknown"

    run_async(go())


def test_v3_write_failure_falls_back_to_v2_and_reprobes():
    async def go():
        async with FakeMiner(locked=False, super_password="changed") as miner:
            api = miner.api(super_password="super")
            await api.power_off()
            assert api.v2_sent == ["power_off"]  # code -2 is not success
            assert miner.services == []
            assert api.control_api == "unknown"  # next command re-probes

    run_async(go())


def test_old_firmware_skips_the_v3_probe():
    async def go():
        async with FakeMiner(locked=False) as miner:
            api = miner.api()
            api.firmware_version = "20230601.22.REL"
            await api.power_off()
            assert api.v2_sent == ["power_off"]
            assert miner.v3_requests == []

    run_async(go())


# ------------------------------------------------------------ controller


def test_lockout_stop_via_v2_fallback_notifies_and_clears(monkeypatch):
    rig = Rig(monkeypatch)
    from test_controller_smoke import pn

    async def go():
        async with FakeMiner(locked=False) as miner:
            api = miner.api()
            api.firmware_version = "20250409.15.REL"
            rig.coord.api = api
            await rig.setup()
            miner.refuse_v3 = True
            rig.set_supply(141.0)
            await rig.tick()
            assert api.v2_sent == ["power_off"]
            assert [c[0] for c in pn.created].count("whatsminer_v2_fallback") == 1
            miner.refuse_v3 = False
            rig.clock.t += controller_mod.LOCKOUT_REASSERT_INTERVAL
            await rig.tick()  # still hashing: reassert, now over v3
            assert miner.services == ["stop"]
            assert "whatsminer_v2_fallback" in pn.dismissed

    run_async(go())


def test_supply_lockout_stops_the_miner_via_v3(monkeypatch):
    rig = Rig(monkeypatch)

    async def go():
        async with FakeMiner(locked=True) as miner:
            api = miner.api()
            rig.coord.api = api
            await rig.setup()
            rig.set_supply(141.0)
            await rig.tick()
            assert rig.pid_state["lockout_latched"] is True
            assert miner.services == ["stop"]
            assert miner.unlock_attempts == 1
            assert api.v2_sent == []
            assert miner.working is False

    run_async(go())


def test_no_floor_command_while_the_firmware_reports_mineroff(monkeypatch):
    """A v3 stop leaves Elapsed counting; that is not a boot to enforce the floor on."""
    rig = Rig(monkeypatch)
    from test_controller_smoke import Store

    Store.saved["whatsminer.e1.controller"] = {"floor_learned": 1500}
    rig.mining(False, limit=1000)
    rig.coord.data["uptime"] = 3000
    rig.coord.data["miner_off"] = True

    async def go():
        await rig.setup()
        await rig.tick()
        await rig.tick()
        assert [c for c in rig.calls if c[0] == "set_power_limit"] == []

    run_async(go())


def test_run_after_v3_start_is_timed_from_the_start_not_from_elapsed(monkeypatch):
    """Elapsed does not reset across a v3 stop/start, so it must not prove a limit."""
    rig = Rig(monkeypatch)
    rig.mining(False, limit=2000)
    rig.coord.data["uptime"] = 5000

    async def go():
        await rig.setup()
        rig.set_supply(104.0)
        await rig.tick()
        rig.mining(True, limit=2000)
        rig.coord.data["uptime"] = 5030
        await rig.tick()
        assert rig.ctl._floor_proven_ok is None
        rig.coord.data["uptime"] = 5000 + controller_mod.FLOOR_STABLE_S
        await rig.tick()
        assert rig.ctl._floor_proven_ok == 2000

    run_async(go())


def test_short_run_after_a_v3_start_counts_toward_the_floor(monkeypatch):
    """A crash 60 s after a v3 start is a short run even though Elapsed is 20000 s."""
    rig = Rig(monkeypatch)
    rig.mining(False, limit=1000)
    rig.coord.data["uptime"] = 20000

    async def go():
        await rig.setup()
        rig.set_supply(104.0)
        await rig.tick()
        rig.mining(True, limit=1000)
        for up in (20030, 20060, 20090):
            rig.coord.data["uptime"] = up
            await rig.tick()
        for up in (10, 40):  # regression, then the confirming poll
            rig.coord.data["uptime"] = up
            await rig.tick()
        assert rig.ctl._floor_short_runs == 1

    run_async(go())


# ------------------------------------------------------------ coordinator


class _PollAPI:
    """Stands in for WhatsminerAPI's reads in the coordinator poll."""

    def __init__(self, status):
        self.status = status
        self.firmware_version = None
        self.noted = []

    async def get_summary(self):
        return {"STATUS": "S", "Msg": {"Elapsed": 3000, "HS RT": 0, "Power Limit": 3300}}

    async def get_miner_info(self):
        return {"STATUS": "S", "Msg": {"mac": "AA:BB:CC:00:11:22", "hostname": "m64", "ip": "127.0.0.1"}}

    async def get_devs(self):
        return None

    async def get_pools(self):
        return None

    async def get_status(self):
        if isinstance(self.status, Exception):
            raise self.status
        return self.status

    def note_status(self, miner_off, reason):
        self.noted.append((miner_off, reason))


def _poll(status):
    coord = coordinator_mod.WhatsminerCoordinator(None, "127.0.0.1", "admin", 4028, 30, "m64")
    coord.api = _PollAPI(status)
    return coord, run_async(coord._async_update_data())


def test_poll_merges_status_and_sets_the_firmware_version():
    coord, data = _poll({"STATUS": "S", "Msg": {
        "mineroff": "true", "mineroff_reason": "by whatsminer api",
        "Firmware Version": "'20250409.15.REL'",
    }})
    assert data["miner_off"] is True
    assert data["miner_off_reason"] == "by whatsminer api"
    assert data["fw_ver"] == "20250409.15.REL"
    assert coord.api.firmware_version == "20250409.15.REL"
    assert coord.api.noted == [(True, "by whatsminer api")]
    assert data["mac"] == "aa_bb_cc_00_11_22"


@pytest.mark.parametrize("status", [RuntimeError("over max connect"), {"STATUS": "E", "Msg": "invalid cmd"}, None])
def test_poll_survives_a_failed_status(status):
    coord, data = _poll(status)
    assert data["miner_off"] is None
    assert coord.api.firmware_version is None
    assert data["mac"] == "aa_bb_cc_00_11_22"


# -------------------------------------------------------------- setup


def _setup_with(data, options):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "wm.__init__", pathlib.Path(coordinator_mod.__file__).with_name("__init__.py")
    )
    init_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(init_mod)
    seen = {}

    class Stop(Exception):
        pass

    def fake_coordinator(**kwargs):
        seen.update(kwargs)
        raise Stop

    init_mod.WhatsminerCoordinator = fake_coordinator
    hass = type("Hass", (), {"data": {}})()
    entry = type("Entry", (), {"data": data, "options": options, "title": "m64", "entry_id": "e1"})()
    with pytest.raises(Stop):
        run_async(init_mod.async_setup_entry(hass, entry))
    return seen["super_password"]


def test_super_password_wiring():
    assert _setup_with({"host": "127.0.0.1"}, {}) == "super"
    assert _setup_with({"host": "127.0.0.1", "super_password": "s1"}, {}) == "s1"
    assert _setup_with({"host": "127.0.0.1", "super_password": "s1"}, {"super_password": "s2"}) == "s2"
    assert _setup_with({"host": "127.0.0.1"}, {"super_password": ""}) == "super"


def test_coordinator_hands_the_super_password_to_v3():
    coord = coordinator_mod.WhatsminerCoordinator(None, "127.0.0.1", "admin", 4028, 30, "m64", "s3")
    assert coord.api.v3.super_password == "s3"
    assert coordinator_mod.WhatsminerAPI("127.0.0.1", 4028).v3.super_password == "super"
