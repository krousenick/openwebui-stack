"""Read MaxMind DB files with the standard library.

GeoIP lookups for keycloak_login_tracker.py. The playbook fetches DB-IP's
City Lite database (tasks/geoip.yml), which uses the same file format and
record layout as MaxMind's GeoIP2/GeoLite2 City, so either works here.
Format: https://maxmind.github.io/MaxMind-DB/

A file is a binary search tree over the address bits, 16 zero bytes, a data
section of typed values, and a metadata map after the marker
\\xab\\xcd\\xefMaxMind.com near the end. The file is memory-mapped, so a
150 MB database costs page cache, not process memory, and a lookup touches
a few dozen pages.

Standard library only, like every script in this directory.
"""

from __future__ import annotations

import ipaddress
import mmap
import struct
from pathlib import Path
from typing import Any

METADATA_MARKER = b"\xab\xcd\xefMaxMind.com"
# The metadata section is at most this long; the marker is searched for there.
METADATA_MAX_SIZE = 128 * 1024
# Size offsets for the 29/30/31 size escapes, and the pointer value offsets.
_SIZE_BASE = (29, 285, 65821)
_POINTER_BASE = (0, 2048, 526336, 0)
# Unsigned integer types and their largest payload in bytes.
_UNSIGNED = {5: 2, 6: 4, 9: 8, 10: 16}
_CACHE_LIMIT = 4096


class InvalidDatabaseError(Exception):
    """The file is not a MaxMind DB this reader understands, or it is damaged."""


class Decoder:
    """Decodes the typed values of a data section that starts at `base` in `buffer`."""

    def __init__(self, buffer: bytes | mmap.mmap, base: int):
        self.buffer = buffer
        self.base = base

    def _bytes(self, position: int, size: int) -> bytes:
        chunk = self.buffer[position : position + size]
        if len(chunk) != size:
            raise InvalidDatabaseError("a value runs past the end of the file")
        return bytes(chunk)

    def decode(self, offset: int, *, follow: bool = True) -> tuple[Any, int]:
        """Return (value, offset of the next value), offsets relative to the section start."""
        position = self.base + offset
        control = self._bytes(position, 1)[0]
        position += 1
        kind = control >> 5
        if kind == 1:
            # Pointer: 001SSVVV, then SS + 1 bytes (the VVV bits are unused for SS = 3).
            size = (control >> 3) & 0x3
            raw = self._bytes(position, size + 1)
            position += size + 1
            value = control & 0x7 if size < 3 else 0
            target = (value << (8 * (size + 1))) | int.from_bytes(raw, "big")
            if size < 3:
                target += _POINTER_BASE[size]
            if not follow:
                raise InvalidDatabaseError("a pointer points at another pointer")
            resolved, _ = self.decode(target, follow=False)
            return resolved, position - self.base
        if kind == 0:
            # Extended type: the next byte holds the type minus 7.
            kind = 7 + self._bytes(position, 1)[0]
            position += 1
            if kind < 8:
                raise InvalidDatabaseError(f"invalid extended type {kind}")
        size = control & 0x1F
        if size >= 29:
            extra = size - 28
            size = _SIZE_BASE[extra - 1] + int.from_bytes(self._bytes(position, extra), "big")
            position += extra

        if kind == 2:
            try:
                value = self._bytes(position, size).decode("utf-8")
            except UnicodeDecodeError as error:
                raise InvalidDatabaseError(f"a string is not UTF-8: {error}") from error
            return value, position + size - self.base
        if kind == 3:
            if size != 8:
                raise InvalidDatabaseError(f"a double has {size} bytes")
            return struct.unpack(">d", self._bytes(position, 8))[0], position + 8 - self.base
        if kind == 4:
            return self._bytes(position, size), position + size - self.base
        if kind in _UNSIGNED:
            if size > _UNSIGNED[kind]:
                raise InvalidDatabaseError(f"an unsigned integer of type {kind} has {size} bytes")
            return int.from_bytes(self._bytes(position, size), "big"), position + size - self.base
        if kind == 7:
            result: dict[str, Any] = {}
            next_offset = position - self.base
            for _ in range(size):
                key, next_offset = self.decode(next_offset)
                if not isinstance(key, str):
                    raise InvalidDatabaseError("a map key is not a string")
                result[key], next_offset = self.decode(next_offset)
            return result, next_offset
        if kind == 8:
            if size > 4:
                raise InvalidDatabaseError(f"an int32 has {size} bytes")
            # Shorter than 4 bytes means positive (the leading bytes are zero).
            return int.from_bytes(self._bytes(position, size), "big", signed=size == 4), position + size - self.base
        if kind == 11:
            items: list[Any] = []
            next_offset = position - self.base
            for _ in range(size):
                item, next_offset = self.decode(next_offset)
                items.append(item)
            return items, next_offset
        if kind == 14:
            if size > 1:
                raise InvalidDatabaseError(f"a boolean has the value {size}")
            return bool(size), position - self.base
        if kind == 15:
            if size != 4:
                raise InvalidDatabaseError(f"a float has {size} bytes")
            return struct.unpack(">f", self._bytes(position, 4))[0], position + 4 - self.base
        raise InvalidDatabaseError(f"unexpected type {kind} in the data section")


class Reader:
    """A memory-mapped MaxMind DB. get(address) returns the record for it, or None."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        with self.path.open("rb") as handle:
            try:
                self._buffer = mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ)
            except ValueError as error:
                raise InvalidDatabaseError(f"{self.path} is empty") from error
        try:
            self._open()
        except Exception:
            self._buffer.close()
            raise

    def _open(self) -> None:
        size = len(self._buffer)
        marker = self._buffer.rfind(METADATA_MARKER, max(0, size - METADATA_MAX_SIZE))
        if marker < 0:
            raise InvalidDatabaseError(f"{self.path} has no MaxMind DB metadata")
        metadata, _ = Decoder(self._buffer, marker + len(METADATA_MARKER)).decode(0)
        if not isinstance(metadata, dict):
            raise InvalidDatabaseError("the metadata is not a map")
        self.metadata: dict[str, Any] = metadata
        try:
            self.node_count = int(metadata["node_count"])
            self.record_size = int(metadata["record_size"])
            self.ip_version = int(metadata["ip_version"])
            major = int(metadata.get("binary_format_major_version", 0))
        except (KeyError, TypeError, ValueError) as error:
            raise InvalidDatabaseError(f"incomplete metadata: {error!r}") from error
        if major != 2:
            raise InvalidDatabaseError(f"unsupported format version {major}")
        if self.record_size not in (24, 28, 32):
            raise InvalidDatabaseError(f"unsupported record size {self.record_size}")
        if self.ip_version not in (4, 6):
            raise InvalidDatabaseError(f"unsupported IP version {self.ip_version}")
        self._node_bytes = self.record_size // 4
        tree_size = self._node_bytes * self.node_count
        if tree_size + 16 > marker:
            raise InvalidDatabaseError("the search tree runs into the metadata")
        self._decoder = Decoder(self._buffer, tree_size + 16)
        self._cache: dict[int, Any] = {}
        # IPv4 addresses live in the ::/96 subtree of an IPv6 database.
        node = 0
        if self.ip_version == 6:
            for _ in range(96):
                if node >= self.node_count:
                    break
                node = self._record(node, 0)
        self._ipv4_start = node

    @property
    def database_type(self) -> str:
        return str(self.metadata.get("database_type") or "")

    @property
    def build_epoch(self) -> int:
        return int(self.metadata.get("build_epoch") or 0)

    def _record(self, node: int, bit: int) -> int:
        start = node * self._node_bytes
        buffer = self._buffer
        if self.record_size == 24:
            offset = start + 3 * bit
            return int.from_bytes(buffer[offset : offset + 3], "big")
        if self.record_size == 28:
            middle = buffer[start + 3]
            if bit == 0:
                return ((middle & 0xF0) << 20) | int.from_bytes(buffer[start : start + 3], "big")
            return ((middle & 0x0F) << 24) | int.from_bytes(buffer[start + 4 : start + 7], "big")
        offset = start + 4 * bit
        return int.from_bytes(buffer[offset : offset + 4], "big")

    def get(self, address: str | ipaddress.IPv4Address | ipaddress.IPv6Address) -> Any:
        """The record for an address, or None when the database has none.

        Raises ValueError for something that is not an address, or an IPv6
        address in an IPv4-only database. The returned value is shared
        between lookups: treat it as read-only.
        """
        ip = ipaddress.ip_address(address)
        if ip.version == 6:
            if self.ip_version == 4:
                raise ValueError(f"{ip} is IPv6 and {self.path.name} holds only IPv4")
            node, bits = 0, 128
        else:
            node, bits = self._ipv4_start, 32
        value = int(ip)
        for index in range(bits):
            if node >= self.node_count:
                break
            node = self._record(node, (value >> (bits - 1 - index)) & 1)
        if node == self.node_count:
            return None
        if node < self.node_count:
            raise InvalidDatabaseError(f"the search tree for {ip} ends inside the tree")
        offset = node - self.node_count - 16
        if offset < 0:
            raise InvalidDatabaseError(f"the search tree for {ip} points before the data section")
        if offset not in self._cache:
            if len(self._cache) >= _CACHE_LIMIT:
                self._cache.clear()
            self._cache[offset], _ = self._decoder.decode(offset)
        return self._cache[offset]

    def close(self) -> None:
        self._buffer.close()

    def __enter__(self) -> Reader:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
