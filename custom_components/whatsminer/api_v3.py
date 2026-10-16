"""Whatsminer API v3 client (TCP 4433): mining stop/start, operating power,
and firmware updates.

Why this exists: on the M64 firmware (20250409.15.REL) the v2 power_off does
not keep the miner off. btminer restarts and resumes hashing within minutes,
so the supply lockout and the demand shutoff could not stop it (2026-10-07:
boiler loop to 143°F). v3 set.miner.service stop holds: hashing goes to zero
within ~15 s and stays there while the 4028 API keeps answering (Elapsed keeps
counting, v2 status reports mineroff "by whatsminer api"). start resumes
hashing within ~10 s.

1.11: v3 set.miner.power changes the operating power without the btminer
restart every v2 adjust_power_limit causes (verified 2026-10-12, see
set_miner_power), and get.miner.status summary reports up-freq-finish, which
says when it takes effect.

Protocol (per asic-rs, whatsminer v3 backend, verified live):
- Request and response are a 4-byte little-endian length prefix + JSON.
  "code": 0 is success.
- Reads need no auth: {"cmd": "get.device.info", "param": "miner"|"salt"}.
- Writes carry account "super", ts = unix seconds and
  token = base64(sha256(cmd + super_password + salt + ts))[:8].
- A write answers code -4 ("no permission for write command") until the
  write API is opened on the v2 port: open_write_api on 4028 (plain JSON),
  then {"token": md5(time + newsalt + UNLOCK_MAGIC + md5crypt(admin, salt))}
  on the same socket. The unlock persists, so it is done only on -4.

Firmware update note (2026-10-15): the init/upload/final flow is the
vendor-documented set.system.update_firmware. Live observation on
20250409.15.REL: the daemon accepts the combined whatsminer-all-*.bin and
answers code 0, but its boot applier silently skips that container (the
miner stayed on the old release; the upgrade only landed via the 8889
single-sub-image path, see ~/network/wmt-re). Expect this service to apply
cleanly on firmware new enough to self-select bundle entries; on older
firmware the final code-0 without a version change is the known symptom.

Passwords and tokens are never logged.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import struct
import time

try:
    from passlib.hash import md5_crypt
except ImportError:
    md5_crypt = None

_LOGGER = logging.getLogger(__name__)

V3_PORT = 4433
DEFAULT_SUPER_PASSWORD = "super"
# asic-rs hardcodes this for the unlock; used when the configured admin
# password is rejected.
FALLBACK_ADMIN_PASSWORD = "admin"
UNLOCK_CLIENT = "heatcore"
UNLOCK_MAGIC = "3804fe31981418ce711a31d94bc69651"
CODE_NO_WRITE_PERMISSION = -4
# A v3 reply is a few hundred bytes; refuse anything absurd rather than
# allocating whatever a garbled length prefix says.
MAX_FRAME = 1 << 20

SERVICE_STOP = "stop"
SERVICE_START = "start"

# set.system.update_firmware (official flow, apidoc.whatsminer.com 3.0.0):
# tokened init JSON -> {"code": 0, "msg": "ready"} -> u32 LE total size +
# raw bundle bytes on the SAME connection -> framed JSON confirmation.
# The miner itself parses the bundle and picks the right sub-image.
# Live-verified on M64 (20250409.15.REL, 2026-10-14): init answered "ready".
CMD_UPDATE_FIRMWARE = "set.system.update_firmware"
UPDATE_MSG_READY = "ready"
UPLOAD_CHUNK = 64 * 1024
# The biggest stock bundle we know is ~43 MB (whatsminer-all-20260908.12);
# refuse anything wildly larger before touching the socket.
MAX_FIRMWARE_BYTES = 64 * 1024 * 1024
# Flashing/verification happens between upload and the final reply.
UPDATE_FINAL_TIMEOUT = 360.0
UPDATE_PROGRESS_STEP = 1 * 1024 * 1024


class WhatsminerV3Error(Exception):
    """A v3 command failed (transport, framing or a non-zero code)."""

    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


def encode_frame(payload: dict) -> bytes:
    """4-byte little-endian length prefix + compact JSON."""
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return struct.pack("<I", len(raw)) + raw


def v3_token(cmd: str, password: str, salt: str, ts: int) -> str:
    """Write token: base64(sha256(cmd + password + salt + ts))[:8]."""
    digest = hashlib.sha256(f"{cmd}{password}{salt}{ts}".encode()).digest()
    return base64.b64encode(digest).decode()[:8]


def unlock_token(admin_password: str, salt: str, newsalt: str, time_str: str) -> str:
    """Token for the 4028 open_write_api handshake."""
    if md5_crypt is None:
        raise WhatsminerV3Error("passlib is required for the Whatsminer write unlock")
    pwd = md5_crypt.using(salt=salt).hash(admin_password).split("$")[3]
    return hashlib.md5(f"{time_str}{newsalt}{UNLOCK_MAGIC}{pwd}".encode()).hexdigest()


async def _read_json(reader: asyncio.StreamReader, timeout: float) -> dict:
    """Read one unframed JSON object (the v2 port has no length prefix)."""
    buf = b""
    while True:
        chunk = await asyncio.wait_for(reader.read(4096), timeout=timeout)
        if not chunk:
            break
        buf += chunk
        try:
            return json.loads(buf.decode("utf-8", errors="ignore").replace("\x00", "").strip())
        except json.JSONDecodeError:
            if len(buf) > MAX_FRAME:
                break
    raise WhatsminerV3Error(f"incomplete response on the v2 port ({len(buf)} bytes)")


class WhatsminerV3API:
    """Async client for the subset of API v3 the integration needs."""

    def __init__(
        self,
        host: str,
        admin_password: str = FALLBACK_ADMIN_PASSWORD,
        super_password: str = DEFAULT_SUPER_PASSWORD,
        port: int = V3_PORT,
        legacy_port: int = 4028,
        timeout: float = 10.0,
    ) -> None:
        self.host = host
        self.port = port
        self.legacy_port = legacy_port
        self.admin_password = admin_password or FALLBACK_ADMIN_PASSWORD
        self.super_password = super_password or DEFAULT_SUPER_PASSWORD
        self.timeout = timeout

    async def _rpc(self, payload: dict) -> dict:
        """One framed request/response on the v3 port."""
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), timeout=self.timeout
            )
        except (asyncio.TimeoutError, OSError) as err:
            raise WhatsminerV3Error(f"cannot connect to {self.host}:{self.port}: {err!r}") from err
        try:
            writer.write(encode_frame(payload))
            await writer.drain()
            header = await asyncio.wait_for(reader.readexactly(4), timeout=self.timeout)
            (length,) = struct.unpack("<I", header)
            if length > MAX_FRAME:
                raise WhatsminerV3Error(f"v3 frame of {length} bytes refused")
            body = await asyncio.wait_for(reader.readexactly(length), timeout=self.timeout)
            response = json.loads(body.decode("utf-8", errors="ignore"))
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, OSError) as err:
            raise WhatsminerV3Error(f"v3 {payload.get('cmd')} to {self.host} failed: {err!r}") from err
        except json.JSONDecodeError as err:
            raise WhatsminerV3Error(f"v3 {payload.get('cmd')} returned invalid JSON: {err}") from err
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
        if not isinstance(response, dict):
            raise WhatsminerV3Error(f"v3 {payload.get('cmd')} returned a non-object")
        return response

    async def get_device_info(self, param: str) -> dict:
        """get.device.info; returns msg. Raises unless code is 0."""
        response = await self._rpc({"cmd": "get.device.info", "param": param})
        if response.get("code") != 0:
            raise WhatsminerV3Error(
                f"get.device.info {param} returned code {response.get('code')}: {response.get('msg')}",
                response.get("code"),
            )
        msg = response.get("msg")
        return msg if isinstance(msg, dict) else {}

    async def probe(self) -> bool:
        """True when the v3 API answers a read."""
        try:
            await self.get_device_info("miner")
        except WhatsminerV3Error as err:
            _LOGGER.debug("v3 API probe on %s failed: %s", self.host, err)
            return False
        return True

    async def get_working(self) -> bool | None:
        """msg.miner.working as a bool, None when absent."""
        miner = (await self.get_device_info("miner")).get("miner") or {}
        working = miner.get("working") if isinstance(miner, dict) else None
        if working is None:
            return None
        return str(working).lower() == "true"

    async def _write(self, cmd: str, param) -> dict:
        salt = (await self.get_device_info("salt")).get("salt")
        if not salt:
            raise WhatsminerV3Error("v3 salt missing from get.device.info")
        ts = int(time.time())
        return await self._rpc({
            "cmd": cmd,
            "param": param,
            "token": v3_token(cmd, self.super_password, salt, ts),
            "account": "super",
            "ts": ts,
        })

    def _admin_candidates(self) -> list[str]:
        candidates = [self.admin_password]
        if self.admin_password != FALLBACK_ADMIN_PASSWORD:
            candidates.append(FALLBACK_ADMIN_PASSWORD)
        return candidates

    async def _unlock_once(self, admin_password: str) -> None:
        """open_write_api handshake on the v2 port.

        Raises if the challenge never arrives. The reply to the token is read
        best-effort and only logged: like asic-rs and wm-v3-service.py, the
        caller retries the v3 write and lets its code decide, since the reply's
        format ("API command OK" as JSON, text, split reads) is not something
        a stop should hinge on.
        """
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.legacy_port), timeout=self.timeout
            )
        except (asyncio.TimeoutError, OSError) as err:
            raise WhatsminerV3Error(f"cannot connect to {self.host}:{self.legacy_port}: {err!r}") from err
        try:
            writer.write(json.dumps(
                {"command": "open_write_api", "client": UNLOCK_CLIENT, "enable": True}
            ).encode())
            await writer.drain()
            msg = (await _read_json(reader, self.timeout)).get("Msg")
            if not isinstance(msg, dict) or not all(k in msg for k in ("salt", "newsalt", "time")):
                raise WhatsminerV3Error(f"open_write_api on {self.host}: unexpected challenge {msg}")
            token = unlock_token(admin_password, str(msg["salt"]), str(msg["newsalt"]), str(msg["time"]))
            writer.write(json.dumps({"token": token}).encode())
            await writer.drain()
            try:
                reply = await asyncio.wait_for(reader.read(4096), timeout=self.timeout)
            except (asyncio.TimeoutError, OSError) as err:
                reply = repr(err).encode()
            _LOGGER.debug(
                "open_write_api on %s answered: %s", self.host,
                reply.decode("utf-8", errors="replace").replace("\x00", "").strip()[:200],
            )
        except (asyncio.TimeoutError, OSError, ValueError) as err:
            raise WhatsminerV3Error(f"open_write_api on {self.host} failed: {err!r}") from err
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

    async def _write_unlocked(self, cmd: str, param) -> dict:
        """A tokened write; returns the response, raises unless code 0.

        A -4 (write API closed) triggers an unlock and one retry per admin
        password tried (the configured one, then the default).
        """
        response = await self._write(cmd, param)
        if response.get("code") == CODE_NO_WRITE_PERMISSION:
            _LOGGER.info("v3 %s %s on %s: write API closed, unlocking", cmd, param, self.host)
            for index, password in enumerate(self._admin_candidates()):
                try:
                    await self._unlock_once(password)
                except WhatsminerV3Error as err:
                    # A wrong password may just drop the socket; try the next one.
                    _LOGGER.debug("open_write_api attempt %d on %s failed: %s", index + 1, self.host, err)
                    continue
                response = await self._write(cmd, param)
                if response.get("code") != CODE_NO_WRITE_PERMISSION:
                    _LOGGER.info(
                        "Opened the v3 write API on %s%s", self.host,
                        " (default admin password)" if index else "",
                    )
                    break
            else:
                _LOGGER.warning("open_write_api on %s did not open writes for any admin password tried", self.host)
                raise WhatsminerV3Error(
                    f"v3 write API still closed on {self.host} after open_write_api", CODE_NO_WRITE_PERMISSION
                )
        if response.get("code") != 0:
            raise WhatsminerV3Error(
                f"v3 {cmd} {param} returned code {response.get('code')}: {response.get('msg')}",
                response.get("code"),
            )
        return response

    async def set_miner_service(self, action: str) -> dict:
        """set.miner.service stop|start. Returns the response; raises unless code 0."""
        return await self._write_unlocked("set.miner.service", action)

    async def set_miner_power(self, watts: int) -> dict:
        """set.miner.power: the operating power in watts, without a btminer restart.

        Verified on the M64 (20250409.15.REL, API 3.0.1) on 2026-10-12: code
        0, power ramped 1884 -> 1244 W in ~80 s with elapsed still counting
        (/home/ethan/network/power-test-20261012-220429.log). The param is a
        JSON number, not a string. It is only "dynamic effective" once
        frequency tuning has finished (summary up-freq-finish 1); during
        tuning the firmware defers it to the next boot. On API < 3.0.3 it is
        not persisted, and a v2 adjust_power_limit discards it (the operating
        power resets to the limit). Raises unless code 0.
        """
        try:
            value = int(watts)
        except (TypeError, ValueError) as err:
            raise WhatsminerV3Error(f"set.miner.power needs whole watts, got {watts!r}") from err
        if value <= 0:
            raise WhatsminerV3Error(f"set.miner.power needs a positive wattage, got {value}")
        return await self._write_unlocked("set.miner.power", value)

    async def get_miner_status(self, param: str = "summary") -> dict:
        """get.miner.status (no auth); returns msg[param] as a dict. Raises unless code 0.

        The summary carries up-freq-finish (1 once frequency tuning is done),
        which gates set.miner.power.
        """
        response = await self._rpc({"cmd": "get.miner.status", "param": param})
        if response.get("code") != 0:
            raise WhatsminerV3Error(
                f"get.miner.status {param} returned code {response.get('code')}: {response.get('msg')}",
                response.get("code"),
            )
        msg = response.get("msg")
        section = msg.get(param) if isinstance(msg, dict) else None
        return section if isinstance(section, dict) else {}

    async def _update_firmware_once(self, data: bytes, progress) -> dict:
        """One full update_firmware attempt on ONE connection.

        Returns the init response when init was refused (caller decides);
        after bytes were streamed, returns the final confirmation instead.
        """
        salt = (await self.get_device_info("salt")).get("salt")
        if not salt:
            raise WhatsminerV3Error("v3 salt missing from get.device.info")
        ts = int(time.time())
        init = {
            "cmd": CMD_UPDATE_FIRMWARE,
            "token": v3_token(CMD_UPDATE_FIRMWARE, self.super_password, salt, ts),
            "account": "super",
            "ts": ts,
        }
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), timeout=self.timeout
            )
        except (asyncio.TimeoutError, OSError) as err:
            raise WhatsminerV3Error(f"cannot connect to {self.host}:{self.port}: {err!r}") from err
        try:
            writer.write(encode_frame(init))
            await writer.drain()
            header = await asyncio.wait_for(reader.readexactly(4), timeout=self.timeout)
            (length,) = struct.unpack("<I", header)
            if length > MAX_FRAME:
                raise WhatsminerV3Error(f"v3 frame of {length} bytes refused")
            body = await asyncio.wait_for(reader.readexactly(length), timeout=self.timeout)
            reply = json.loads(body.decode("utf-8", errors="ignore"))
            if reply.get("code") == CODE_NO_WRITE_PERMISSION:
                # Caller unlocks the write API and retries the whole flow.
                return reply
            if not (reply.get("code") == 0
                    and str(reply.get("msg", "")).lower() == UPDATE_MSG_READY):
                # Refused before any byte was staged: raise now, so a
                # code-0 "busy" can never masquerade as success.
                raise WhatsminerV3Error(
                    f"v3 update_firmware on {self.host} refused at init: "
                    f"code {reply.get('code')}: {reply.get('msg')}",
                    reply.get("code"),
                )
            _LOGGER.info("v3 firmware upload to %s: %d bytes", self.host, len(data))
            writer.write(struct.pack("<I", len(data)))
            sent = 0
            start = time.monotonic()
            for off in range(0, len(data), UPLOAD_CHUNK):
                writer.write(data[off:off + UPLOAD_CHUNK])
                await writer.drain()
                sent = min(off + UPLOAD_CHUNK, len(data))
                if progress is not None and (
                        sent % UPDATE_PROGRESS_STEP < UPLOAD_CHUNK or sent == len(data)):
                    progress(sent, len(data))
            _LOGGER.info(
                "v3 firmware upload to %s: streamed %d bytes in %.1f s; awaiting flash result",
                self.host, len(data), time.monotonic() - start,
            )
            header = await asyncio.wait_for(reader.readexactly(4), timeout=UPDATE_FINAL_TIMEOUT)
            (length,) = struct.unpack("<I", header)
            if length > MAX_FRAME:
                raise WhatsminerV3Error(f"v3 frame of {length} bytes refused")
            body = await asyncio.wait_for(reader.readexactly(length), timeout=UPDATE_FINAL_TIMEOUT)
            return json.loads(body.decode("utf-8", errors="ignore"))
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, OSError) as err:
            raise WhatsminerV3Error(
                f"v3 firmware upload to {self.host} failed mid-flow: {err!r}") from err
        except json.JSONDecodeError as err:
            raise WhatsminerV3Error(f"v3 firmware update on {self.host} returned invalid JSON: {err}") from err
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

    async def update_firmware(self, data: bytes, progress=None) -> dict:
        """Upload a complete official firmware bundle (set.system.update_firmware).

        ``data`` is the whole ``whatsminer-all-*.bin`` file; the miner parses
        it and picks its own sub-image. progress(sent, total) is called as
        chunks are written. A -4 (write API closed) triggers the v2
        open_write_api unlock and one retry per admin password, exactly like
        the other writes. The init is refused BEFORE any byte is uploaded on
        wrong credentials or a busy miner. Raises unless the final code is 0.
        Returns the final response.
        """
        if not data:
            raise WhatsminerV3Error("empty firmware image")
        if len(data) > MAX_FIRMWARE_BYTES:
            raise WhatsminerV3Error(
                f"firmware image of {len(data)} bytes exceeds the {MAX_FIRMWARE_BYTES}-byte sanity cap"
            )
        response = await self._update_firmware_once(data, progress)
        if response.get("code") == CODE_NO_WRITE_PERMISSION:
            _LOGGER.info("v3 update_firmware on %s: write API closed, unlocking", self.host)
            for index, password in enumerate(self._admin_candidates()):
                try:
                    await self._unlock_once(password)
                except WhatsminerV3Error as err:
                    _LOGGER.debug("open_write_api attempt %d on %s failed: %s", index + 1, self.host, err)
                    continue
                response = await self._update_firmware_once(data, progress)
                if response.get("code") != CODE_NO_WRITE_PERMISSION:
                    break
            else:
                raise WhatsminerV3Error(
                    f"v3 write API still closed on {self.host} after open_write_api",
                    CODE_NO_WRITE_PERMISSION,
                )
        if response.get("code") != 0:
            raise WhatsminerV3Error(
                f"v3 update_firmware on {self.host} returned code {response.get('code')}: "
                f"{response.get('msg')}",
                response.get("code"),
            )
        return response
