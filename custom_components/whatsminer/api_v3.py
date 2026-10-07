"""Whatsminer API v3 client (TCP 4433), used to stop and start mining.

Why this exists: on the M64 firmware (20250409.15.REL) the v2 power_off does
not keep the miner off. btminer restarts and resumes hashing within minutes,
so the supply lockout and the demand shutoff could not stop it (2026-10-07:
boiler loop to 143°F). v3 set.miner.service stop holds: hashing goes to zero
within ~15 s and stays there while the 4028 API keeps answering (Elapsed keeps
counting, v2 status reports mineroff "by whatsminer api"). start resumes
hashing within ~10 s.

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

    async def set_miner_service(self, action: str) -> dict:
        """set.miner.service stop|start. Returns the response; raises unless code 0.

        A -4 (write API closed) triggers an unlock and one retry per admin
        password tried (the configured one, then the default).
        """
        cmd = "set.miner.service"
        response = await self._write(cmd, action)
        if response.get("code") == CODE_NO_WRITE_PERMISSION:
            _LOGGER.info("v3 %s %s on %s: write API closed, unlocking", cmd, action, self.host)
            for index, password in enumerate(self._admin_candidates()):
                try:
                    await self._unlock_once(password)
                except WhatsminerV3Error as err:
                    # A wrong password may just drop the socket; try the next one.
                    _LOGGER.debug("open_write_api attempt %d on %s failed: %s", index + 1, self.host, err)
                    continue
                response = await self._write(cmd, action)
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
                f"v3 {cmd} {action} returned code {response.get('code')}: {response.get('msg')}",
                response.get("code"),
            )
        return response
