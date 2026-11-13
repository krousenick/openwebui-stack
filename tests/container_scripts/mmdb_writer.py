"""Write small MaxMind DB files for the GeoIP tests.

Written from the format specification (https://maxmind.github.io/MaxMind-DB/),
independently of container-scripts/mmdb.py, so the reader is checked against
files it did not produce itself.
"""

from __future__ import annotations

import dataclasses
import ipaddress
import struct
from pathlib import Path
from typing import Any

MARKER = b"\xab\xcd\xefMaxMind.com"


@dataclasses.dataclass(frozen=True)
class Pointer:
    """A pointer to a value already in the data section, at its offset.

    form picks the encoding (0-3: 1 to 4 bytes after the control byte);
    by default the shortest one that fits.
    """

    offset: int
    form: int | None = None


@dataclasses.dataclass(frozen=True)
class Typed:
    """A number stored as a specific type (5 uint16, 6 uint32, 8 int32, 9 uint64, 10 uint128, 15 float)."""

    kind: int
    value: int | float


def _control(kind: int, size: int) -> bytes:
    if size < 29:
        bits, extra = size, b""
    elif size < 285:
        bits, extra = 29, bytes([size - 29])
    elif size < 65821:
        bits, extra = 30, (size - 285).to_bytes(2, "big")
    else:
        bits, extra = 31, (size - 65821).to_bytes(3, "big")
    if kind <= 7:
        return bytes([(kind << 5) | bits]) + extra
    # Extended types: type bits 0, then a byte with the type minus 7.
    return bytes([bits, kind - 7]) + extra


def _pointer(offset: int, form: int | None) -> bytes:
    if form is None:
        form = 0 if offset < 2048 else 1 if offset < 526336 else 2 if offset < 526336 + (1 << 27) else 3
    if form == 0 and offset < 2048:
        return bytes([0x20 | (offset >> 8), offset & 0xFF])
    if form == 1 and 2048 <= offset < 526336:
        value = offset - 2048
        return bytes([0x28 | (value >> 16)]) + (value & 0xFFFF).to_bytes(2, "big")
    if form == 2 and 526336 <= offset < 526336 + (1 << 27):
        value = offset - 526336
        return bytes([0x30 | (value >> 24)]) + (value & 0xFFFFFF).to_bytes(3, "big")
    if form == 3:
        return bytes([0x38]) + offset.to_bytes(4, "big")
    raise ValueError(f"offset {offset} does not fit pointer form {form}")


def _unsigned(value: int) -> bytes:
    return value.to_bytes((value.bit_length() + 7) // 8, "big")


def encode(value: Any) -> bytes:
    if isinstance(value, Pointer):
        return _pointer(value.offset, value.form)
    if isinstance(value, Typed):
        if value.kind == 15:
            return _control(15, 4) + struct.pack(">f", value.value)
        if value.kind == 8:
            number = int(value.value)
            payload = number.to_bytes(4, "big", signed=True) if number < 0 else _unsigned(number)
            return _control(8, len(payload)) + payload
        payload = _unsigned(int(value.value))
        return _control(value.kind, len(payload)) + payload
    if isinstance(value, bool):
        return _control(14, int(value))
    if isinstance(value, int):
        payload = _unsigned(value)
        return _control(6 if value < 1 << 32 else 9, len(payload)) + payload
    if isinstance(value, float):
        return _control(3, 8) + struct.pack(">d", value)
    if isinstance(value, str):
        raw = value.encode()
        return _control(2, len(raw)) + raw
    if isinstance(value, bytes):
        return _control(4, len(value)) + value
    if isinstance(value, dict):
        return _control(7, len(value)) + b"".join(encode(str(key)) + encode(item) for key, item in value.items())
    if isinstance(value, list):
        return _control(11, len(value)) + b"".join(encode(item) for item in value)
    raise TypeError(f"cannot encode {value!r}")


def build(
    networks: list[tuple[str, Any]],
    *,
    ip_version: int = 6,
    record_size: int = 24,
    database_type: str = "Test-City",
    metadata: dict[str, Any] | None = None,
) -> bytes:
    """A database mapping each network to its value (values are written in list order)."""
    data = bytearray()
    root: list[Any] = [None, None]
    nodes = [root]
    width = 128 if ip_version == 6 else 32
    for network, value in networks:
        net = ipaddress.ip_network(network)
        offset = len(data)
        data += encode(value)
        bits = net.prefixlen
        if net.version == 4 and ip_version == 6:
            bits += 96  # IPv4 lives in the ::/96 subtree
        elif net.version != ip_version:
            raise ValueError(f"{network} does not fit an IPv{ip_version} tree")
        address = int(net.network_address)
        node = root
        for index in range(bits):
            bit = (address >> (width - 1 - index)) & 1
            if index == bits - 1:
                node[bit] = ("data", offset)
                break
            if node[bit] is None:
                node[bit] = [None, None]
                nodes.append(node[bit])
            if isinstance(node[bit], tuple):
                raise ValueError(f"{network} overlaps an earlier network")
            node = node[bit]
    count = len(nodes)
    number = {id(node): index for index, node in enumerate(nodes)}

    def record(child: Any) -> int:
        if child is None:
            return count
        if isinstance(child, tuple):
            return count + 16 + child[1]
        return number[id(child)]

    tree = bytearray()
    for node in nodes:
        left, right = record(node[0]), record(node[1])
        if record_size == 24:
            tree += left.to_bytes(3, "big") + right.to_bytes(3, "big")
        elif record_size == 28:
            tree += (left & 0xFFFFFF).to_bytes(3, "big")
            tree += bytes([((left >> 24) << 4) | (right >> 24)])
            tree += (right & 0xFFFFFF).to_bytes(3, "big")
        else:
            tree += left.to_bytes(4, "big") + right.to_bytes(4, "big")
    meta = {
        "node_count": Typed(6, count),
        "record_size": Typed(5, record_size),
        "ip_version": Typed(5, ip_version),
        "database_type": database_type,
        "languages": ["en"],
        "binary_format_major_version": Typed(5, 2),
        "binary_format_minor_version": Typed(5, 0),
        "build_epoch": Typed(9, 1_762_000_000),
        "description": {"en": "test database"},
        **(metadata or {}),
    }
    return bytes(tree) + bytes(16) + bytes(data) + MARKER + encode(meta)


def write(path: Path, networks: list[tuple[str, Any]], **options: Any) -> Path:
    path.write_bytes(build(networks, **options))
    return path


def city(name: str, region: str, country: str, code: str, lat: float, lon: float) -> dict[str, Any]:
    """A record shaped like DB-IP City Lite's (and GeoLite2 City's)."""
    return {
        "city": {"names": {"en": name}},
        "continent": {"code": "XX", "names": {"en": "Somewhere"}},
        "country": {"iso_code": code, "names": {"en": country}},
        "location": {"latitude": lat, "longitude": lon},
        "subdivisions": [{"names": {"en": region}}],
    }
