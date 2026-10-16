"""Firmware update path: set.system.update_firmware streaming + bundle preflight.

Everything talks to fake miners on 127.0.0.1; nothing reaches a real host.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import pathlib
import struct
import sys
import zlib

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_controller_smoke import run_async  # noqa: E402

v3 = importlib.import_module("wm.api_v3")
fwimg = importlib.import_module("wm.firmware_image")

SALT = "Abc123xy"
UNLOCK_SALT = "BQ5hoXV9"
UNLOCK_NEWSALT = "nEwSaLt1"
UNLOCK_TIME = "1759800000"


def patterned_bytes(n: int) -> bytes:
    return (bytes(range(256)) * (n // 256 + 1))[:n]


class FakeFlashMiner:
    """v3 framed port supporting the multi-stage update_firmware flow,
    plus a v2 open_write_api challenge port."""

    def __init__(self, admin_password="admin", super_password="super", locked=False):
        self.admin_password = admin_password
        self.super_password = super_password
        self.locked = locked
        self.inits = 0
        self.init_code = 0
        self.init_msg = "ready"
        self.final_code = 0
        self.received = b""
        self.announced = -1
        self.unlock_attempts = 0
        self._servers = []

    async def __aenter__(self):
        v2 = await asyncio.start_server(self._v2, "127.0.0.1", 0)
        self.v2_port = v2.sockets[0].getsockname()[1]
        self._servers.append(v2)
        v3srv = await asyncio.start_server(self._v3, "127.0.0.1", 0)
        self.v3_port = v3srv.sockets[0].getsockname()[1]
        self._servers.append(v3srv)
        return self

    async def __aexit__(self, *exc):
        for srv in self._servers:
            srv.close()
            await srv.wait_closed()

    def client(self) -> "v3.WhatsminerV3API":
        return v3.WhatsminerV3API(
            "127.0.0.1",
            admin_password=self.admin_password,
            super_password="super",
            port=self.v3_port,
            legacy_port=self.v2_port,
            timeout=2.0,
        )

    @staticmethod
    async def _reply(writer, obj):
        raw = json.dumps(obj).encode()
        writer.write(struct.pack("<I", len(raw)) + raw)
        await writer.drain()

    async def _v3(self, reader, writer):
        try:
            while True:
                try:
                    header = await reader.readexactly(4)
                except (asyncio.IncompleteReadError, ConnectionResetError):
                    return
                (length,) = struct.unpack("<I", header)
                req = json.loads(await reader.readexactly(length))
                cmd = req.get("cmd")
                if cmd == "get.device.info" and req.get("param") == "salt":
                    await self._reply(writer, {"code": 0, "msg": {"salt": SALT}})
                    continue
                if cmd == "set.system.update_firmware":
                    self.inits += 1
                    if self.locked:
                        await self._reply(
                            writer, {"code": -4, "msg": "no permission for write command"})
                        continue
                    expected = v3.v3_token(cmd, self.super_password, SALT, req.get("ts", 0))
                    if req.get("account") != "super" or req.get("token") != expected:
                        await self._reply(
                            writer, {"code": -2, "msg": "invalid json or password is wrong"})
                        continue
                    await self._reply(
                        writer, {"code": self.init_code, "msg": self.init_msg, "desc": cmd})
                    if self.init_code == 0 and self.init_msg == "ready":
                        (size,) = struct.unpack("<I", await reader.readexactly(4))
                        self.announced = size
                        got = b""
                        while len(got) < size:
                            got += await reader.readexactly(min(1 << 16, size - len(got)))
                        self.received = got
                        await self._reply(writer, {
                            "code": self.final_code,
                            "msg": "ok" if self.final_code == 0 else "bad image",
                            "desc": cmd,
                        })
                    continue
                await self._reply(writer, {"code": -1, "msg": "unknown command"})
        finally:
            writer.close()

    async def _v2(self, reader, writer):
        try:
            req = await v3._read_json(reader, 2.0)
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
                writer.write(json.dumps({"STATUS": "S", "Msg": "API command OK"}).encode())
            else:
                writer.write(json.dumps({"STATUS": "E", "Msg": "invalid token"}).encode())
            await writer.drain()
        finally:
            writer.close()


# ---------------------------------------------------------------- streaming


def test_update_firmware_happy():
    async def go():
        async with FakeFlashMiner() as miner:
            client = miner.client()
            payload = patterned_bytes(200_000)
            events = []
            resp = await client.update_firmware(payload, lambda s, t: events.append((s, t)))
            assert resp["code"] == 0
            assert miner.announced == len(payload)
            assert miner.received == payload
            assert events and events[-1] == (len(payload), len(payload))
            assert all(a[0] < b[0] for a, b in zip(events, events[1:]))
            assert miner.inits == 1
    run_async(go())


def test_update_firmware_wrong_super_password_stages_nothing():
    async def go():
        async with FakeFlashMiner(super_password="changed") as miner:
            client = miner.client()
            with pytest.raises(v3.WhatsminerV3Error):
                await client.update_firmware(patterned_bytes(1 << 16))
            assert miner.received == b""
            assert miner.inits == 1
    run_async(go())


def test_update_firmware_busy_miner_stages_nothing():
    async def go():
        async with FakeFlashMiner() as miner:
            miner.init_code = 0
            miner.init_msg = "busy"
            with pytest.raises(v3.WhatsminerV3Error):
                await miner.client().update_firmware(patterned_bytes(1 << 16))
            assert miner.received == b""
    run_async(go())


def test_update_firmware_unlocks_write_api_then_uploads():
    async def go():
        async with FakeFlashMiner(locked=True) as miner:
            payload = patterned_bytes(70_000)
            resp = await miner.client().update_firmware(payload)
            assert resp["code"] == 0
            assert miner.unlock_attempts == 1
            assert miner.inits == 2  # -4, unlock, retry
            assert miner.received == payload
    run_async(go())


def test_update_firmware_final_failure_raises():
    async def go():
        async with FakeFlashMiner() as miner:
            miner.final_code = -1
            payload = patterned_bytes(1 << 16)
            with pytest.raises(v3.WhatsminerV3Error):
                await miner.client().update_firmware(payload)
            # The miner got the bytes; it just rejected them afterwards.
            assert miner.received == payload
    run_async(go())


def test_update_firmware_rejects_oversize_before_connecting(monkeypatch):
    monkeypatch.setattr(v3, "MAX_FIRMWARE_BYTES", 1024)
    client = v3.WhatsminerV3API("127.0.0.1", port=1, legacy_port=1, timeout=0.5)

    async def go():
        with pytest.raises(v3.WhatsminerV3Error):
            await client.update_firmware(patterned_bytes(2048))
    run_async(go())


def test_update_firmware_rejects_empty():
    async def go():
        client = v3.WhatsminerV3API("127.0.0.1", port=1, legacy_port=1, timeout=0.5)
        with pytest.raises(v3.WhatsminerV3Error):
            await client.update_firmware(b"")
    run_async(go())


# ------------------------------------------------------- bundle validation


def make_bundle(pi_version=2, encsize=0x100):
    size = fwimg.IH_HDR_SIZE + encsize
    hdr = bytearray(fwimg.BUNDLE_PAYLOAD_OFF)
    struct.pack_into("<I", hdr, 0, fwimg.BUNDLE_MAGIC)
    struct.pack_into("<I", hdr, 8, 1750000000)
    struct.pack_into("<I", hdr, 0x10, size)
    struct.pack_into("<I", hdr, 0x14, 1)
    entry = bytearray(fwimg.BUNDLE_ENTRY_SIZE)
    entry[0:2] = b"0\0"
    entry[0x10:0x15] = b"h616\0"
    struct.pack_into("<III", entry, 0x20, fwimg.BUNDLE_PAYLOAD_OFF, size, pi_version)
    hdr[0x18:0x18 + fwimg.BUNDLE_ENTRY_SIZE] = entry
    crc = zlib.crc32(bytes(hdr[:fwimg.BUNDLE_HDR_CRC_OFF])) ^ 0xFFFFFFFF
    struct.pack_into("<I", hdr, fwimg.BUNDLE_HDR_CRC_OFF, crc)
    sub = bytearray(size)
    struct.pack_into("<III", sub, 0, fwimg.IH_MAGIC, fwimg.IH_TAGS[0], 0)
    struct.pack_into("<I", sub, 0x20, encsize)
    struct.pack_into("<I", sub, 0x38, 1)
    return bytes(hdr) + bytes(sub)


def test_inspect_bundle_accepts_synthetic():
    info = fwimg.inspect_bundle(make_bundle())
    assert info["entries"][0]["key"] == "0-h616v2"
    assert len(info["sha256"]) == 64


def test_inspect_bundle_bad_magic():
    blob = bytearray(make_bundle())
    struct.pack_into("<I", blob, 0, 0xDEADBEEF)
    with pytest.raises(fwimg.FirmwareImageError):
        fwimg.inspect_bundle(bytes(blob))


def test_inspect_bundle_bad_header_crc():
    blob = bytearray(make_bundle())
    blob[0x1A97] ^= 0xFF  # corrupt inside the crc-protected range
    with pytest.raises(fwimg.FirmwareImageError):
        fwimg.inspect_bundle(bytes(blob))


def test_inspect_bundle_size_identity():
    blob = bytearray(make_bundle(encsize=0x100))
    struct.pack_into("<I", blob, fwimg.BUNDLE_PAYLOAD_OFF + 0x20, 0x77)  # lie
    with pytest.raises(fwimg.FirmwareImageError):
        fwimg.inspect_bundle(bytes(blob))


def test_inspect_bundle_entry_count_zero():
    blob = bytearray(make_bundle())
    struct.pack_into("<I", blob, 0x14, 0)
    crc = zlib.crc32(bytes(blob[:fwimg.BUNDLE_HDR_CRC_OFF])) ^ 0xFFFFFFFF
    struct.pack_into("<I", blob, fwimg.BUNDLE_HDR_CRC_OFF, crc)
    with pytest.raises(fwimg.FirmwareImageError):
        fwimg.inspect_bundle(bytes(blob))


def test_inspect_bundle_truncated():
    with pytest.raises(fwimg.FirmwareImageError):
        fwimg.inspect_bundle(b"\x3f\x0a\x64\xae" + b"\0" * 40)
