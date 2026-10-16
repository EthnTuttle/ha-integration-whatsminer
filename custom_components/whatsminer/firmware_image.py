"""Preflight validation for official WhatsMiner firmware bundles.

A firmware service must never offer a garbage file to the miner, so the
bundle structure is checked BEFORE the update socket opens. The format and
all constants are clean-room, from the wmt-re project
(~/network/wmt-re/docs/01-firmware-bundle-format.md and 02): verified against
WhatsMinerTool 9.2.5's own validator (FUN_004a7290) and stock bundles.

Layout (little-endian):
  0x00  u32  magic 0xAE640A3F
  0x08  u32  build timestamp
  0x10  u32  payload size
  0x14  u32  entry count (<= 0x41)
  0x18  entry table, 0x68 bytes each: chip[16] platform[16] u32 offset,
        u32 size, u32 pi_version
  0x1A98 u32  header CRC: zlib.crc32(header[:0x1A98]) ^ 0xFFFFFFFF
  0x1A9C payloads; each sub-image carries a 0xB8 header:
        u32 ih_magic 0x9A5BA57C, u32 tag in {0x8C790AFE, 0x22FA87BD},
        u32 encsize @0x20 with 0xB8 + encsize == entry size, u32 rsv @0x38 == 1.
"""
from __future__ import annotations

import hashlib
import struct
import zlib

BUNDLE_MAGIC = 0xAE640A3F
BUNDLE_HDR_CRC_OFF = 0x1A98
BUNDLE_PAYLOAD_OFF = 0x1A9C
BUNDLE_ENTRY_SIZE = 0x68
BUNDLE_MAX_ENTRIES = 0x41
IH_MAGIC = 0x9A5BA57C
IH_TAGS = (0x8C790AFE, 0x22FA87BD)
IH_HDR_SIZE = 0xB8


class FirmwareImageError(Exception):
    """The given file is not a plausible official WhatsMiner bundle."""


def inspect_bundle(data: bytes) -> dict:
    """Validate a complete bundle; return a summary dict. Pure; no I/O."""
    if len(data) < BUNDLE_PAYLOAD_OFF:
        raise FirmwareImageError(f"file too small ({len(data)} bytes) for a bundle header")
    (magic,) = struct.unpack_from("<I", data, 0)
    if magic != BUNDLE_MAGIC:
        raise FirmwareImageError(f"bad magic {magic:#010x} (expected {BUNDLE_MAGIC:#010x})")
    (stored_crc,) = struct.unpack_from("<I", data, BUNDLE_HDR_CRC_OFF)
    calc_crc = zlib.crc32(data[:BUNDLE_HDR_CRC_OFF]) ^ 0xFFFFFFFF
    if calc_crc != stored_crc:
        raise FirmwareImageError(
            f"header CRC mismatch (calc {calc_crc:#x}, stored {stored_crc:#x})")
    (build_ts,) = struct.unpack_from("<I", data, 0x08)
    (entry_count,) = struct.unpack_from("<I", data, 0x14)
    if entry_count < 1 or entry_count > BUNDLE_MAX_ENTRIES:
        raise FirmwareImageError(f"implausible entry count {entry_count}")
    entries = []
    for i in range(entry_count):
        o = 0x18 + i * BUNDLE_ENTRY_SIZE
        chip = data[o:o + 0x10].split(b"\0")[0].decode("ascii", "replace").strip()
        plat = data[o + 0x10:o + 0x20].split(b"\0")[0].decode("ascii", "replace").strip()
        offset, size, pi_version = struct.unpack_from("<III", data, o + 0x20)
        key = "%s-%sv%d%s" % (chip or "0", plat, pi_version,
                              "-btrom" if chip == "1011200000" else "")
        _check_subimage(data, offset, size, key)
        entries.append({"key": key, "offset": offset, "size": size})
    return {
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "build_ts": build_ts,
        "entries": entries,
    }


def _check_subimage(data: bytes, offset: int, size: int, key: str) -> None:
    if offset < BUNDLE_PAYLOAD_OFF or offset + size > len(data):
        raise FirmwareImageError(
            f"entry {key}: offset/size outside the file ({offset:#x}+{size:#x})")
    if size < IH_HDR_SIZE:
        raise FirmwareImageError(f"entry {key}: smaller than its 0xB8 header")
    magic, tag, _ver = struct.unpack_from("<III", data, offset)
    (encsize,) = struct.unpack_from("<I", data, offset + 0x20)
    (rsv,) = struct.unpack_from("<I", data, offset + 0x38)
    if magic != IH_MAGIC:
        raise FirmwareImageError(f"entry {key}: sub-image magic mismatch")
    if tag not in IH_TAGS:
        raise FirmwareImageError(f"entry {key}: sub-image tag {tag:#010x} unknown")
    if IH_HDR_SIZE + encsize != size:
        raise FirmwareImageError(f"entry {key}: size != 0xB8 + encsize")
    if rsv != 1:
        raise FirmwareImageError(f"entry {key}: reserved field unexpected")
