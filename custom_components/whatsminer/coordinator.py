"""Whatsminer DataUpdateCoordinator."""
from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import logging
import re
from datetime import timedelta, datetime
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)

try:
    from passlib.hash import md5_crypt
except ImportError:
    md5_crypt = None

try:
    from Crypto.Cipher import AES
except ImportError:
    AES = None

_LOGGER = logging.getLogger(__name__)

DEFAULT_DATA = {
    "hostname": None,
    "mac": None,
    "make": "Whatsminer",
    "model": "Unknown",
    "ip": None,
    "is_mining": False,
    "fw_ver": None,
    "hashrate": 0,
    "expected_hashrate": 0,
    "temperature_avg": 0,
    "wattage": 0,
    "wattage_limit": 0,
    "efficiency": 0.0,
    "uptime": 0,
    "accepted": 0,
    "rejected": 0,
    "hashboards": [],
    "fans": [],
}


def _md5_crypt(word: str, salt: str) -> str:
    """Perform MD5 crypt with the given salt."""
    if md5_crypt is None:
        raise Exception("passlib is required for Whatsminer authentication")
    
    standard_salt = re.compile(r'\s*\$(\d+)\$([\w\./]*)\$')
    match = standard_salt.match(salt)
    if not match:
        raise ValueError(f"salt format is not correct: {salt}")
    extra_str = match.group(2)
    result = md5_crypt.hash(word, salt=extra_str)
    return result


def _add_to_16(s: str) -> bytes:
    """Pad string to 16-byte boundary for AES encryption."""
    while len(s) % 16 != 0:
        s += '\0'
    return s.encode()


class WhatsminerAPI:
    """Direct API communication with Whatsminer via TCP.
    
    Implements the Whatsminer API protocol which requires:
    - Read-only commands: sent as plaintext JSON with {"cmd": "command"}
    - Privileged commands: AES-256-ECB encrypted, base64 encoded
    """

    def __init__(self, host: str, port: int, password: str = "admin"):
        """Initialize API."""
        self.host = host
        self.port = port
        self.password = password
        self._cipher = None
        self._sign: str | None = None
        self._token_timestamp: datetime | None = None

    async def _send_raw(self, message: str, timeout: int = 10) -> bytes:
        """Send raw message to miner and get response bytes."""
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port),
            timeout=timeout
        )
        
        _LOGGER.debug(f"Sending to {self.host}: {message[:200]}")
        writer.write(message.encode('utf-8'))
        await writer.drain()
        
        # Read response - read until connection closes or we get enough data
        chunks = []
        try:
            while True:
                chunk = await asyncio.wait_for(reader.read(4096), timeout=timeout)
                if not chunk:
                    break
                chunks.append(chunk)
                # If we got a complete JSON response, we can stop
                data = b''.join(chunks)
                try:
                    # Try to parse - if it works, we have complete data
                    data.decode('utf-8', errors='ignore').strip().replace('\x00', '')
                    if data.endswith(b'}') or data.endswith(b'}\x00'):
                        break
                except:
                    pass
        except asyncio.TimeoutError:
            pass  # Timeout is expected when done reading
        
        writer.close()
        await writer.wait_closed()
        
        return b''.join(chunks)

    async def send_command(self, cmd: str, timeout: int = 5) -> dict | None:
        """Send read-only command to miner and get response.
        
        Uses the format: {"cmd": "command_name"}
        """
        try:
            message = json.dumps({"cmd": cmd})
            data = await self._send_raw(message, timeout)
            
            if not data:
                _LOGGER.warning(f"Empty response from {self.host}")
                return None
            
            # Parse JSON response
            response_str = data.decode('utf-8', errors='ignore').strip().replace('\x00', '')
            _LOGGER.debug(f"Received from {self.host}: {response_str[:200]}")
            
            return json.loads(response_str)
            
        except asyncio.TimeoutError:
            _LOGGER.error(f"Timeout connecting to {self.host}:{self.port}")
            return None
        except ConnectionRefusedError:
            _LOGGER.error(f"Connection refused to {self.host}:{self.port}")
            return None
        except json.JSONDecodeError as err:
            _LOGGER.error(f"Invalid JSON response from {self.host}: {err}")
            return None
        except Exception as err:
            _LOGGER.exception(f"Error communicating with {self.host}: {err}")
            return None

    async def get_summary(self) -> dict | None:
        """Get summary stats."""
        return await self.send_command("summary")

    async def get_pools(self) -> dict | None:
        """Get pool information."""
        return await self.send_command("pools")

    async def get_devs(self) -> dict | None:
        """Get device (hashboard) details."""
        return await self.send_command("devs")

    async def get_miner_info(self) -> dict | None:
        """Get miner device info (hostname, MAC, IP)."""
        return await self.send_command("get_miner_info")

    async def _initialize_write_access(self) -> None:
        """Initialize write access by getting token and setting up encryption.
        
        The Whatsminer API requires:
        1. Get token (salt, newsalt, time) from miner
        2. Create AES key from password + salt
        3. Create sign from key + time + newsalt
        4. Use AES-256-ECB encryption for all privileged commands
        """
        if AES is None:
            raise Exception("pycryptodome is required for Whatsminer write commands. Install with: pip install pycryptodome")
        
        if md5_crypt is None:
            raise Exception("passlib is required for Whatsminer authentication. Install with: pip install passlib")
        
        _LOGGER.debug(f"Initializing write access for {self.host}")
        
        # Get token from miner
        token_data = await self.send_command("get_token")
        
        if not token_data:
            raise Exception("Failed to get token from miner - no response")
        
        msg = token_data.get("Msg", {})
        if msg == "over max connect":
            raise Exception("Miner returned 'over max connect' - too many connections")
        
        if not isinstance(msg, dict):
            raise Exception(f"Invalid token response: {token_data}")
        
        salt = msg.get("salt", "")
        newsalt = msg.get("newsalt", "")
        time_str = msg.get("time", "")
        
        if not all([salt, newsalt, time_str]):
            raise Exception(f"Token response missing required fields: {msg}")
        
        # Make the encrypted key from the admin password and the salt
        pwd = _md5_crypt(self.password, "$1$" + salt + "$")
        pwd_parts = pwd.split("$")
        key = pwd_parts[3] if len(pwd_parts) > 3 else pwd
        
        # Make the aeskey from the key computed above and prep the AES cipher
        aeskey = hashlib.sha256(key.encode()).hexdigest()
        aeskey_bytes = binascii.unhexlify(aeskey.encode())
        self._cipher = AES.new(aeskey_bytes, AES.MODE_ECB)
        
        # Make the 'sign' that is passed in as 'token'
        tmp = _md5_crypt(key + time_str, "$1$" + newsalt + "$")
        tmp_parts = tmp.split("$")
        self._sign = tmp_parts[3] if len(tmp_parts) > 3 else tmp
        
        self._token_timestamp = datetime.now()
        _LOGGER.debug(f"Write access initialized for {self.host}")

    def _has_valid_write_access(self) -> bool:
        """Check if we have valid write access (token not expired)."""
        if self._cipher is None or self._sign is None or self._token_timestamp is None:
            return False
        
        # Token expires after 30 minutes
        age = (datetime.now() - self._token_timestamp).total_seconds()
        return age < 1800  # 30 minutes

    async def send_privileged_command(self, cmd: str, **kwargs) -> dict:
        """Send a privileged command requiring authentication.
        
        Privileged commands are AES-256-ECB encrypted and base64 encoded.
        Format: {"enc": 1, "data": "<base64_encrypted_payload>"}
        
        Args:
            cmd: The command to send (e.g., 'power_off', 'power_on')
            **kwargs: Additional parameters for the command
        
        Returns:
            The response from the miner
        """
        _LOGGER.info(f"Sending privileged command '{cmd}' to {self.host}")
        
        try:
            # Ensure we have valid write access
            if not self._has_valid_write_access():
                await self._initialize_write_access()
            
            # Build the plaintext command
            cmd_data = {"cmd": cmd, "token": self._sign}
            cmd_data.update(kwargs)
            api_cmd = json.dumps(cmd_data)
            
            _LOGGER.debug(f"Plaintext command: {api_cmd}")
            
            # Encrypt with AES-256-ECB and base64 encode
            encrypted = self._cipher.encrypt(_add_to_16(api_cmd))
            enc_str = base64.encodebytes(encrypted).decode('utf-8').replace('\n', '')
            
            # Build the transport packet
            packet = json.dumps({"enc": 1, "data": enc_str})
            
            _LOGGER.debug(f"Sending encrypted command to {self.host}")
            
            # Send and receive
            data = await self._send_raw(packet, timeout=15)
            
            if not data:
                raise Exception("Empty response from command")
            
            # Parse response
            response_str = data.decode('utf-8', errors='ignore').strip().replace('\x00', '')
            _LOGGER.debug(f"Raw response: {response_str[:300]}")
            
            response = json.loads(response_str)
            
            # Check for error status (unencrypted error response)
            if response.get("STATUS") == "E":
                error_msg = response.get("Msg", "Unknown error")
                _LOGGER.error(f"Command '{cmd}' failed on {self.host}: {error_msg}")
                
                # If token/auth error, clear cache and retry once
                if "token" in error_msg.lower() or "auth" in error_msg.lower() or "invalid" in error_msg.lower():
                    _LOGGER.info("Token error detected, reinitializing and retrying...")
                    self._cipher = None
                    self._sign = None
                    await self._initialize_write_access()
                    return await self._send_privileged_command_internal(cmd, **kwargs)
                
                raise Exception(f"Miner returned error: {error_msg}")
            
            # Decrypt the response if it's encrypted
            if "enc" in response:
                resp_ciphertext = base64.b64decode(response["enc"])
                resp_plaintext = self._cipher.decrypt(resp_ciphertext).decode().split("\x00")[0]
                response = json.loads(resp_plaintext)
                _LOGGER.debug(f"Decrypted response: {response}")
            
            _LOGGER.info(f"Command '{cmd}' successful on {self.host}")
            return response
            
        except Exception as err:
            _LOGGER.error(f"Failed to execute command '{cmd}' on {self.host}: {err}")
            raise

    async def _send_privileged_command_internal(self, cmd: str, **kwargs) -> dict:
        """Internal method to send privileged command without retry logic."""
        cmd_data = {"cmd": cmd, "token": self._sign}
        cmd_data.update(kwargs)
        api_cmd = json.dumps(cmd_data)
        
        encrypted = self._cipher.encrypt(_add_to_16(api_cmd))
        enc_str = base64.encodebytes(encrypted).decode('utf-8').replace('\n', '')
        packet = json.dumps({"enc": 1, "data": enc_str})
        
        data = await self._send_raw(packet, timeout=15)
        
        if not data:
            raise Exception("Empty response from command")
        
        response_str = data.decode('utf-8', errors='ignore').strip().replace('\x00', '')
        response = json.loads(response_str)
        
        if response.get("STATUS") == "E":
            raise Exception(f"Miner returned error: {response.get('Msg', 'Unknown error')}")
        
        if "enc" in response:
            resp_ciphertext = base64.b64decode(response["enc"])
            resp_plaintext = self._cipher.decrypt(resp_ciphertext).decode().split("\x00")[0]
            response = json.loads(resp_plaintext)
        
        return response

    async def power_on(self) -> dict:
        """Power on hashboards and start mining."""
        return await self.send_privileged_command("power_on", respbefore="true")

    async def power_off(self) -> dict:
        """Power off hashboards and stop mining."""
        return await self.send_privileged_command("power_off", respbefore="true")

    async def set_power_limit(self, power_limit: int) -> dict:
        """Set the power limit in watts."""
        return await self.send_privileged_command(
            "adjust_power_limit",
            power_limit=str(power_limit)
        )

    async def test_connection(self) -> bool:
        """Test if the miner is reachable."""
        try:
            result = await self.get_summary()
            return result is not None
        except Exception:
            return False


class WhatsminerCoordinator(DataUpdateCoordinator):
    """Class to manage fetching Whatsminer data."""

    def __init__(
        self,
        hass: HomeAssistant,
        ip: str,
        password: str,
        port: int,
        scan_interval: int,
        name: str,
    ) -> None:
        """Initialize coordinator."""
        self.miner_ip = ip
        self.password = password
        self.port = port
        self.api = WhatsminerAPI(ip, port, password)
        self._failure_count = 0
        
        super().__init__(
            hass=hass,
            logger=_LOGGER,
            name=name,
            update_interval=timedelta(seconds=scan_interval),
        )

    def _parse_miner_info(self, data: dict) -> dict:
        """Parse get_miner_info response for device identification."""
        result = {}
        if not data:
            return result
        # New firmware: {"STATUS": "S", ..., "Msg": {...}}
        msg = data.get("Msg", {})
        if not isinstance(msg, dict):
            return result
        result["hostname"] = msg.get("hostname")
        result["mac"] = msg.get("mac", "").replace(":", "_").lower()
        result["ip"] = msg.get("ip")
        return result

    def _parse_summary(self, data: dict) -> dict:
        """Parse summary response."""
        result = {}

        if not data:
            return result

        # New firmware: {"STATUS": "S", ..., "Msg": {flat dict}}
        # Old firmware: {"SUMMARY": [{...}], "STATUS": [...]}
        if "Msg" in data and isinstance(data["Msg"], dict):
            summary = data["Msg"]
        elif "SUMMARY" in data:
            summary_list = data.get("SUMMARY", [])
            if not summary_list:
                return result
            summary = summary_list[0] if isinstance(summary_list, list) else summary_list
        else:
            return result
        
        # Hashrate (convert MH/s to TH/s)
        result["hashrate"] = summary.get("MHS av", 0) / 1_000_000
        result["hashrate_5s"] = summary.get("MHS 5s", 0) / 1_000_000
        result["hashrate_1m"] = summary.get("MHS 1m", 0) / 1_000_000
        
        # Expected hashrate - try multiple field names
        if "Target MHS" in summary:
            result["expected_hashrate"] = summary.get("Target MHS", 0) / 1_000_000
        elif "Factory GHS" in summary:
            result["expected_hashrate"] = summary.get("Factory GHS", 0) / 1_000
        
        # Power (Whatsminer specific fields)
        result["wattage"] = summary.get("Power", 0)
        result["wattage_limit"] = summary.get("Power Limit", 0)
        
        # Temperature (use Chip Temp Avg if available, otherwise PCB Temperature)
        if "Chip Temp Avg" in summary:
            result["temperature_avg"] = summary.get("Chip Temp Avg", 0)
        else:
            result["temperature_avg"] = summary.get("Temperature", 0)
        
        # Calculate efficiency (J/TH = W / (TH/s))
        if result.get("wattage", 0) > 0 and result.get("hashrate", 0) > 0:
            result["efficiency"] = round(result["wattage"] / result["hashrate"], 2)
        
        # Fans (Whatsminer specific)
        fans = []
        if "Fan Speed In" in summary:
            fan_in = summary.get("Fan Speed In", 0)
            if fan_in > 0:  # Only add if non-zero
                fans.append({"speed": fan_in})
        if "Fan Speed Out" in summary:
            fan_out = summary.get("Fan Speed Out", 0)
            if fan_out > 0:  # Only add if non-zero
                fans.append({"speed": fan_out})
        result["fans"] = fans
        
        # Mining status - check multiple indicators
        elapsed = summary.get("Elapsed", 0)
        hashrate = result.get("hashrate", 0)
        # Miner is mining if it has uptime AND non-zero hashrate
        result["is_mining"] = elapsed > 0 and hashrate > 0
        
        result["uptime"] = elapsed
        # Accepted/Rejected may be in summary (old firmware) or in pools (new firmware)
        result["accepted"] = summary.get("Accepted", None)
        result["rejected"] = summary.get("Rejected", None)

        return result

    def _parse_pools(self, data: dict) -> dict:
        """Parse pools response to aggregate accepted/rejected share counts."""
        result = {}
        if not data:
            return result
        # New firmware: {"STATUS": [...], "POOLS": [...]}
        pools = data.get("POOLS", [])
        if not pools:
            return result
        accepted = sum(p.get("Accepted", 0) for p in pools)
        rejected = sum(p.get("Rejected", 0) for p in pools)
        result["accepted"] = accepted
        result["rejected"] = rejected
        return result

    def _parse_devs(self, data: dict) -> list:
        """Parse device (hashboard) data."""
        hashboards = []
        
        if not data or "DEVS" not in data:
            return hashboards
        
        devs = data.get("DEVS", [])
        
        for idx, dev in enumerate(devs):
            mhs_av = dev.get("MHS av", 0)
            # Old firmware: MHS av is in MH/s (values ~30,000,000+), convert to TH/s
            # New firmware: MHS av is already in TH/s (values ~30-100)
            hashrate = mhs_av / 1_000_000 if mhs_av > 1000 else mhs_av
            slot = dev.get("Slot", idx)

            # PCB / board temperature
            temp = dev.get("Temperature", 0)

            # New firmware has no per-board chip temp; use Chip Temp Avg/Chip Temp if present
            chip_temp = dev.get("Chip Temp Avg", dev.get("Chip Temp", temp))

            hashboards.append({
                "slot": slot,
                "temp": temp,
                "chip_temp": chip_temp,
                "hashrate": round(hashrate, 2),
                "status": dev.get("Status", "Alive"),
            })
        
        return hashboards

    async def _async_update_data(self) -> dict:
        """Fetch data from miner."""
        try:
            _LOGGER.debug(f"Fetching data from {self.miner_ip}")
            
            # Fetch all data in parallel
            summary_data, miner_info_data, devs_data, pools_data = await asyncio.gather(
                self.api.get_summary(),
                self.api.get_miner_info(),
                self.api.get_devs(),
                self.api.get_pools(),
                return_exceptions=True
            )

            # Check if all requests failed
            if all(x is None or isinstance(x, Exception) for x in [summary_data, miner_info_data, devs_data, pools_data]):
                self._failure_count += 1

                if self._failure_count == 1:
                    _LOGGER.warning(f"Miner at {self.miner_ip} is offline - returning zeroed data")
                    return DEFAULT_DATA.copy()

                raise UpdateFailed(f"Miner at {self.miner_ip} is offline")

            # Parse responses
            data = DEFAULT_DATA.copy()
            data["ip"] = self.miner_ip
            data["mac"] = f"whatsminer_{self.miner_ip.replace('.', '_')}"

            if miner_info_data and not isinstance(miner_info_data, Exception):
                info = self._parse_miner_info(miner_info_data)
                if info.get("mac"):
                    data.update(info)

            if summary_data and not isinstance(summary_data, Exception):
                data.update(self._parse_summary(summary_data))

            if devs_data and not isinstance(devs_data, Exception):
                data["hashboards"] = self._parse_devs(devs_data)

            # Accepted/Rejected: prefer pools aggregation (new firmware), fall back to summary
            if pools_data and not isinstance(pools_data, Exception):
                pool_stats = self._parse_pools(pools_data)
                if pool_stats.get("accepted") is not None:
                    data["accepted"] = pool_stats["accepted"]
                if pool_stats.get("rejected") is not None:
                    data["rejected"] = pool_stats["rejected"]
            # If still None (neither source had it), default to 0
            if data.get("accepted") is None:
                data["accepted"] = 0
            if data.get("rejected") is None:
                data["rejected"] = 0

            # Reset failure count on success
            self._failure_count = 0
            
            _LOGGER.debug(
                f"Got data from {self.miner_ip}: "
                f"hashrate={data.get('hashrate', 0):.2f} TH/s, "
                f"expected={data.get('expected_hashrate', 0):.2f} TH/s, "
                f"temp={data.get('temperature_avg', 0):.1f}°C, "
                f"power={data.get('wattage', 0)}W, "
                f"limit={data.get('wattage_limit', 0)}W, "
                f"efficiency={data.get('efficiency', 0):.2f} J/TH, "
                f"mining={data.get('is_mining', False)}"
            )
            
            return data
            
        except Exception as err:
            self._failure_count += 1
            
            if self._failure_count == 1:
                _LOGGER.warning(f"Error fetching data from {self.miner_ip}: {err}")
                return DEFAULT_DATA.copy()
            
            _LOGGER.exception(f"Failed to fetch data from {self.miner_ip}")
            raise UpdateFailed(f"Error communicating with miner: {err}")

    @property
    def available(self) -> bool:
        """Return if miner is available."""
        return self._failure_count < 2
