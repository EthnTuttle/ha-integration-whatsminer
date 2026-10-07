#!/usr/bin/env python3
"""Whatsminer v3 API (TCP 4433): stop/start/enable/disable the btminer service.

Usage: wm-v3-service.py HOST {stop|start|enable|disable|status}
Password comes from WM_SUPER_PASSWORD or a prompt (account "super").
Protocol per asic-rs (whatsminer v3 backend): 4-byte LE length prefix + JSON;
token = base64(sha256(cmd + password + salt + ts))[:8].
"""
import base64
import getpass
import hashlib
import json
import os
import socket
import struct
import sys
import time

from passlib.hash import md5_crypt

# asic-rs whatsminer v3 rpc.rs: write commands on 4433 answer code -4 ("no
# permission for write command") until open_write_api is done on 4028.
UNLOCK_CLIENT = "heatcore"
UNLOCK_MAGIC = "3804fe31981418ce711a31d94bc69651"


def rpc(host: str, payload: dict) -> dict:
    with socket.create_connection((host, 4433), timeout=10) as s:
        raw = json.dumps(payload).encode()
        s.sendall(struct.pack("<I", len(raw)) + raw)
        hdr = b""
        while len(hdr) < 4:
            chunk = s.recv(4 - len(hdr))
            if not chunk:
                raise ConnectionError("connection closed before response")
            hdr += chunk
        n = struct.unpack("<I", hdr)[0]
        body = b""
        while len(body) < n:
            chunk = s.recv(n - len(body))
            if not chunk:
                break
            body += chunk
    return json.loads(body)


def unlock_write(host: str, admin_password: str = "admin") -> str:
    with socket.create_connection((host, 4028), timeout=10) as s:
        s.sendall(json.dumps({"command": "open_write_api", "client": UNLOCK_CLIENT, "enable": True}).encode())
        msg = json.loads(s.recv(4096).decode().strip())["Msg"]
        pwd = md5_crypt.using(salt=msg["salt"]).hash(admin_password).split("$")[3]
        token = hashlib.md5(f"{msg['time']}{msg['newsalt']}{UNLOCK_MAGIC}{pwd}".encode()).hexdigest()
        s.sendall(json.dumps({"token": token}).encode())
        return s.recv(4096).decode(errors="replace").strip()


def send_service(host: str, password: str, action: str) -> dict:
    salt = rpc(host, {"cmd": "get.device.info", "param": "salt"})["msg"]["salt"]
    cmd = "set.miner.service"
    ts = int(time.time())
    token = base64.b64encode(hashlib.sha256(f"{cmd}{password}{salt}{ts}".encode()).digest()).decode()[:8]
    return rpc(host, {"cmd": cmd, "param": action, "token": token, "account": "super", "ts": ts})


def main() -> None:
    if len(sys.argv) != 3 or sys.argv[2] not in ("stop", "start", "enable", "disable", "status"):
        sys.exit(__doc__)
    host, action = sys.argv[1], sys.argv[2]
    if action == "status":
        msg = rpc(host, {"cmd": "get.device.info", "param": "miner"}).get("msg", {})
        print("working:", msg.get("miner", {}).get("working"))
        return
    password = os.environ.get("WM_SUPER_PASSWORD") or getpass.getpass("super password: ")
    resp = send_service(host, password, action)
    if resp.get("code") == -4:
        print("unlock:", unlock_write(host, os.environ.get("WM_ADMIN_PASSWORD", "admin")))
        resp = send_service(host, password, action)
    print(json.dumps(resp))


if __name__ == "__main__":
    main()
