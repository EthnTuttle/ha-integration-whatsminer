"""Whatsminer DataUpdateCoordinator."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
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


class WhatsminerAPI:
    """Direct API communication with Whatsminer via TCP."""

    def __init__(self, host: str, port: int, password: str = "admin"):
        """Initialize API."""
        self.host = host
        self.port = port
        self.password = password
        self._token_cache: dict[str, Any] | None = None

    async def send_command(self, command: str, timeout: int = 5) -> dict | None:
        """Send command to miner and get response."""
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port),
                timeout=timeout
            )
            
            # Whatsminer API format: {"command": "summary"}
            message = json.dumps({"command": command})
            _LOGGER.debug(f"Sending to {self.host}: {message}")
            
            writer.write(message.encode('utf-8'))
            await writer.drain()
            
            # Read response
            data = await asyncio.wait_for(reader.read(8192), timeout=timeout)
            writer.close()
            await writer.wait_closed()
            
            if not data:
                _LOGGER.warning(f"Empty response from {self.host}")
                return None
            
            # Parse JSON response
            response_str = data.decode('utf-8', errors='ignore').strip()
            _LOGGER.debug(f"Received from {self.host}: {response_str[:200]}")
            
            # Remove any null bytes
            response_str = response_str.replace('\x00', '')
            
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

    async def get_stats(self) -> dict | None:
        """Get detailed stats including temps and fans."""
        return await self.send_command("stats")

    async def get_token(self) -> dict[str, Any]:
        """Get authentication token from miner.
        
        Returns a dict with token info needed for privileged commands.
        """
        # Check cache (tokens valid for 30 min)
        if self._token_cache:
            age = datetime.now() - self._token_cache.get("timestamp", datetime.min)
            if age.total_seconds() < 1800:  # 30 minutes
                _LOGGER.debug(f"Using cached token for {self.host}")
                return self._token_cache
        
        _LOGGER.debug(f"Requesting new token from {self.host}")
        token_data = await self.send_command("get_token")
        
        if not token_data:
            raise Exception("Failed to get token from miner - no response")
        
        if "Msg" not in token_data:
            _LOGGER.error(f"Token response missing 'Msg': {token_data}")
            raise Exception("Failed to get token from miner - invalid response")
        
        msg = token_data.get("Msg", {})
        salt = msg.get("salt", "")
        newsalt = msg.get("newsalt", "")
        time_str = msg.get("time", "")
        
        if not all([salt, newsalt, time_str]):
            raise Exception(f"Token response missing required fields: salt={salt}, newsalt={newsalt}, time={time_str}")
        
        # Encrypt password with salt using MD5 crypt
        if md5_crypt:
            pwd = md5_crypt.using(salt=salt).hash(self.password)
            pwd_parts = pwd.split("$")
            host_passwd_md5 = pwd_parts[3] if len(pwd_parts) > 3 else pwd
            
            # Encrypt again with time and newsalt
            tmp = md5_crypt.using(salt=newsalt).hash(host_passwd_md5 + time_str)
            tmp_parts = tmp.split("$")
            host_sign = tmp_parts[3] if len(tmp_parts) > 3 else tmp
        else:
            # Fallback MD5 (less secure but works)
            _LOGGER.warning("passlib not available, using basic MD5 hashing")
            host_passwd_md5 = hashlib.md5(f"{salt}{self.password}".encode()).hexdigest()
            host_sign = hashlib.md5(f"{newsalt}{host_passwd_md5}{time_str}".encode()).hexdigest()
        
        self._token_cache = {
            "host_sign": host_sign,
            "host_passwd_md5": host_passwd_md5,
            "time": time_str,
            "timestamp": datetime.now()
        }
        
        _LOGGER.debug(f"Got new token for {self.host}")
        return self._token_cache

    def _build_token_string(self, token_info: dict[str, Any]) -> str:
        """Build the token string for authenticated commands.
        
        Format: admin$time$host_sign
        """
        return f"admin${token_info['time']}${token_info['host_sign']}"

    async def send_privileged_command(self, cmd: str, **kwargs) -> dict:
        """Send a privileged command requiring authentication.
        
        Args:
            cmd: The command to send (e.g., 'power_off', 'power_on')
            **kwargs: Additional parameters for the command
        
        Returns:
            The response from the miner
        """
        _LOGGER.info(f"Sending privileged command '{cmd}' to {self.host}")
        
        try:
            # Get authentication token
            token_info = await self.get_token()
            token_str = self._build_token_string(token_info)
            
            # Build command with token
            cmd_data = {
                "cmd": cmd,
                "token": token_str,
                **kwargs
            }
            cmd_str = json.dumps(cmd_data)
            
            _LOGGER.debug(f"Sending authenticated command to {self.host}: {cmd_str}")
            
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port),
                timeout=15
            )
            
            writer.write(cmd_str.encode('utf-8'))
            await writer.drain()
            
            # Read response
            data = await asyncio.wait_for(reader.read(8192), timeout=15)
            writer.close()
            await writer.wait_closed()
            
            if not data:
                raise Exception("Empty response from command")
            
            # Parse response
            response_str = data.decode('utf-8', errors='ignore').strip().replace('\x00', '')
            _LOGGER.info(f"Response from {self.host}: {response_str[:300]}")
            
            response = json.loads(response_str)
            
            # Check response status
            if "STATUS" in response:
                status_list = response.get("STATUS", [])
                if isinstance(status_list, list) and len(status_list) > 0:
                    status = status_list[0]
                    if status.get("STATUS") == "S":
                        _LOGGER.info(f"Command '{cmd}' successful on {self.host}")
                        return response
                    elif status.get("STATUS") == "E":
                        error_msg = status.get("Msg", "Unknown error")
                        _LOGGER.error(f"Command '{cmd}' failed on {self.host}: {error_msg}")
                        
                        # If token error, clear cache and retry once
                        if "token" in error_msg.lower() or "auth" in error_msg.lower():
                            _LOGGER.info("Token error detected, clearing cache and retrying...")
                            self._token_cache = None
                            return await self._retry_privileged_command(cmd, **kwargs)
                        
                        raise Exception(f"Miner returned error: {error_msg}")
                    else:
                        # Other status, log and return
                        _LOGGER.warning(f"Command '{cmd}' returned status: {status}")
                        return response
            
            # If no STATUS field, return response as-is
            return response
            
        except Exception as err:
            _LOGGER.error(f"Failed to execute command '{cmd}' on {self.host}: {err}")
            raise

    async def _retry_privileged_command(self, cmd: str, **kwargs) -> dict:
        """Retry a privileged command after token refresh."""
        token_info = await self.get_token()
        token_str = self._build_token_string(token_info)
        
        cmd_data = {
            "cmd": cmd,
            "token": token_str,
            **kwargs
        }
        cmd_str = json.dumps(cmd_data)
        
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port),
            timeout=15
        )
        
        writer.write(cmd_str.encode('utf-8'))
        await writer.drain()
        
        data = await asyncio.wait_for(reader.read(8192), timeout=15)
        writer.close()
        await writer.wait_closed()
        
        if not data:
            raise Exception("Empty response from retry command")
        
        response_str = data.decode('utf-8', errors='ignore').strip().replace('\x00', '')
        return json.loads(response_str)

    async def power_on(self) -> dict:
        """Power on hashboards and start mining."""
        return await self.send_privileged_command("power_on")

    async def power_off(self) -> dict:
        """Power off hashboards and stop mining."""
        return await self.send_privileged_command("power_off")

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

    def _parse_summary(self, data: dict) -> dict:
        """Parse summary response."""
        result = {}
        
        if not data or "SUMMARY" not in data:
            return result
        
        summary_list = data.get("SUMMARY", [])
        if not summary_list:
            return result
        
        summary = summary_list[0] if isinstance(summary_list, list) else summary_list
        
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
        result["accepted"] = summary.get("Accepted", 0)
        result["rejected"] = summary.get("Rejected", 0)
        
        return result

    def _parse_stats(self, data: dict) -> dict:
        """Parse stats response for temps, fans, power."""
        result = {}
        
        if not data or "STATS" not in data:
            return result
        
        stats_list = data.get("STATS", [])
        if not stats_list or len(stats_list) < 2:
            return result
        
        # Usually stats[1] has the miner details
        stats = stats_list[1] if len(stats_list) > 1 else stats_list[0]
        
        # Model and firmware
        result["model"] = stats.get("Type", "Unknown")
        result["fw_ver"] = stats.get("Software", "Unknown")
        
        # Power
        result["wattage"] = stats.get("Power", 0)
        result["wattage_limit"] = stats.get("Power Limit", 0)
        
        # Temperature - try multiple field names
        temps = []
        for i in range(10):  # Check up to 10 temp sensors
            temp_key = f"Temperature{i}" if i > 0 else "Temperature"
            if temp_key in stats:
                temps.append(stats[temp_key])
        
        if temps:
            result["temperature_avg"] = sum(temps) / len(temps)
            result["temperature_max"] = max(temps)
        
        # Fans - collect all fan speeds
        fans = []
        for i in range(10):  # Check up to 10 fans
            fan_key = f"Fan Speed {i+1}"
            if fan_key in stats:
                fans.append({"speed": stats[fan_key]})
        
        result["fans"] = fans
        
        # Calculate efficiency if we have both hashrate and power
        if "hashrate" in result and result.get("wattage", 0) > 0:
            result["efficiency"] = result["wattage"] / result["hashrate"]
        
        return result

    def _parse_devs(self, data: dict) -> list:
        """Parse device (hashboard) data."""
        hashboards = []
        
        if not data or "DEVS" not in data:
            return hashboards
        
        devs = data.get("DEVS", [])
        
        for idx, dev in enumerate(devs):
            hashrate = dev.get("MHS av", 0) / 1_000_000  # MH/s to TH/s
            
            # Whatsminer uses "Chip Temp Avg", otherwise fallback to "Temperature"
            chip_temp = dev.get("Chip Temp Avg", 0)
            if chip_temp == 0:
                chip_temp = dev.get("Chip Temp", 0)
            
            # PCB temperature
            temp = dev.get("Temperature", chip_temp)
            
            hashboards.append({
                "slot": idx,
                "temp": temp,
                "chip_temp": chip_temp,
                "hashrate": round(hashrate, 2),
                "status": dev.get("Status", "Unknown"),
            })
        
        return hashboards

    async def _async_update_data(self) -> dict:
        """Fetch data from miner."""
        try:
            _LOGGER.debug(f"Fetching data from {self.miner_ip}")
            
            # Fetch all data in parallel
            summary_data, stats_data, devs_data = await asyncio.gather(
                self.api.get_summary(),
                self.api.get_stats(),
                self.api.get_devs(),
                return_exceptions=True
            )
            
            # Check if all requests failed
            if all(x is None or isinstance(x, Exception) for x in [summary_data, stats_data, devs_data]):
                self._failure_count += 1
                
                if self._failure_count == 1:
                    _LOGGER.warning(f"Miner at {self.miner_ip} is offline - returning zeroed data")
                    return DEFAULT_DATA.copy()
                
                raise UpdateFailed(f"Miner at {self.miner_ip} is offline")
            
            # Parse responses
            data = DEFAULT_DATA.copy()
            data["ip"] = self.miner_ip
            data["mac"] = f"whatsminer_{self.miner_ip.replace('.', '_')}"
            
            if summary_data and not isinstance(summary_data, Exception):
                data.update(self._parse_summary(summary_data))
            
            if stats_data and not isinstance(stats_data, Exception):
                data.update(self._parse_stats(stats_data))
            
            if devs_data and not isinstance(devs_data, Exception):
                data["hashboards"] = self._parse_devs(devs_data)
            
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
