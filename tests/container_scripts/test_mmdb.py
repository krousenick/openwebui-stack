"""mmdb.py: the standard-library MaxMind DB reader behind the login tracker's GeoIP lookups."""

from __future__ import annotations

import pytest

import mmdb
import mmdb_writer
from mmdb_writer import Pointer, Typed

LONDON = mmdb_writer.city("London", "England", "United Kingdom", "GB", 51.5142, -0.0931)
SEATTLE = mmdb_writer.city("Seattle", "Washington", "United States", "US", 47.6062, -122.3321)
STOCKHOLM = mmdb_writer.city("Stockholm", "Stockholm", "Sweden", "SE", 59.3293, 18.0686)


@pytest.mark.parametrize("record_size", [24, 28, 32])
def test_finds_ipv4_and_ipv6_networks_for_every_record_size(tmp_path, record_size):
    path = mmdb_writer.write(
        tmp_path / "city.mmdb",
        [("81.2.69.0/24", LONDON), ("216.160.83.56/29", SEATTLE), ("2a02:aa1::/32", STOCKHOLM)],
        record_size=record_size,
    )
    with mmdb.Reader(path) as reader:
        assert reader.record_size == record_size
        assert reader.get("81.2.69.142") == LONDON
        assert reader.get("81.2.69.0") == LONDON
        assert reader.get("216.160.83.60") == SEATTLE
        assert reader.get("2a02:aa1:1234::1") == STOCKHOLM
        assert reader.get("81.2.70.1") is None
        assert reader.get("216.160.83.64") is None
        assert reader.get("2001:db8::1") is None


def test_an_ipv4_database_answers_ipv4_and_refuses_ipv6(tmp_path):
    path = mmdb_writer.write(tmp_path / "v4.mmdb", [("81.2.69.0/24", LONDON)], ip_version=4)
    with mmdb.Reader(path) as reader:
        assert reader.get("81.2.69.1") == LONDON
        assert reader.get("8.8.8.8") is None
        with pytest.raises(ValueError, match="IPv6"):
            reader.get("2a02:aa1::1")


def test_an_ipv6_database_without_ipv4_networks_finds_no_ipv4_address(tmp_path):
    path = mmdb_writer.write(tmp_path / "v6.mmdb", [("2a02:aa1::/32", STOCKHOLM)])
    with mmdb.Reader(path) as reader:
        assert reader.get("81.2.69.1") is None
        assert reader.get("2a02:aa1::5") == STOCKHOLM


def test_metadata(tmp_path):
    path = mmdb_writer.write(tmp_path / "city.mmdb", [("81.2.69.0/24", LONDON)], database_type="DBIP-City-Lite")
    with mmdb.Reader(path) as reader:
        assert reader.database_type == "DBIP-City-Lite"
        assert reader.build_epoch == 1_762_000_000
        assert reader.ip_version == 6
        assert reader.metadata["languages"] == ["en"]


def test_decodes_every_data_type(tmp_path):
    value = {
        "string": "Zürich",
        "empty": "",
        "double": -122.3321,
        "float": Typed(15, 1.5),
        "bytes": b"\x00\xff",
        "uint16": Typed(5, 65535),
        "uint32": Typed(6, 4_000_000_000),
        "uint64": Typed(9, 2**63 + 5),
        "uint128": Typed(10, 2**127 + 1),
        "zero": Typed(6, 0),
        "int32_negative": Typed(8, -5),
        "int32_short": Typed(8, 300),
        "true": True,
        "false": False,
        "array": [1, "two", [3.0], {"four": 4}],
        "nested": {"a": {"b": {"c": "d"}}},
        # Lengths past the 29/30/31 size escapes.
        "long_29": "x" * 100,
        "long_30": "y" * 1000,
        "long_31": "z" * 70_000,
    }
    path = mmdb_writer.write(tmp_path / "types.mmdb", [("81.2.69.0/24", value)])
    with mmdb.Reader(path) as reader:
        got = reader.get("81.2.69.9")
    assert got["string"] == "Zürich"
    assert got["empty"] == ""
    assert got["double"] == -122.3321
    assert got["float"] == 1.5
    assert got["bytes"] == b"\x00\xff"
    assert got["uint16"] == 65535
    assert got["uint32"] == 4_000_000_000
    assert got["uint64"] == 2**63 + 5
    assert got["uint128"] == 2**127 + 1
    assert got["zero"] == 0
    assert got["int32_negative"] == -5
    assert got["int32_short"] == 300
    assert got["true"] is True
    assert got["false"] is False
    assert got["array"] == [1, "two", [3.0], {"four": 4}]
    assert got["nested"] == {"a": {"b": {"c": "d"}}}
    assert got["long_29"] == "x" * 100
    assert got["long_30"] == "y" * 1000
    assert got["long_31"] == "z" * 70_000


def test_follows_pointers_of_each_size(tmp_path):
    # Padding moves the shared values past each pointer form's reach:
    # 2048 bytes for the 1-byte form, 526336 for the 2-byte form.
    near = {"names": {"en": "Near"}}
    middle = {"names": {"en": "Middle"}}
    far = {"names": {"en": "Far"}}
    small_pad, big_pad = "p" * 3000, "q" * 530_000
    offsets, networks, offset = {}, [], 0
    for index, (name, value) in enumerate(
        [("near", near), ("small_pad", small_pad), ("middle", middle), ("big_pad", big_pad), ("far", far)]
    ):
        offsets[name] = offset
        networks.append((f"10.{index}.0.0/16", value))
        offset += len(mmdb_writer.encode(value))
    assert offsets["middle"] >= 2048 and offsets["far"] >= 526336
    pointers = {
        "one_byte": Pointer(offsets["near"]),
        "two_bytes": Pointer(offsets["middle"]),
        "three_bytes": Pointer(offsets["far"]),
        "four_bytes": Pointer(offsets["middle"], form=3),
    }
    assert [mmdb_writer.encode(pointer)[0] >> 3 for pointer in pointers.values()] == [0b100, 0b101, 0b110, 0b111]
    networks.append(("10.9.0.0/16", pointers))
    path = mmdb_writer.write(tmp_path / "pointers.mmdb", networks, ip_version=4)
    with mmdb.Reader(path) as reader:
        got = reader.get("10.9.0.1")
    assert got == {"one_byte": near, "two_bytes": middle, "three_bytes": far, "four_bytes": middle}


def test_a_pointer_to_a_pointer_is_refused(tmp_path):
    networks = [("10.1.0.0/16", Pointer(0)), ("10.2.0.0/16", {"bad": Pointer(0)})]
    path = mmdb_writer.write(tmp_path / "loop.mmdb", networks, ip_version=4)
    with mmdb.Reader(path) as reader, pytest.raises(mmdb.InvalidDatabaseError, match="pointer"):
        reader.get("10.2.0.1")


def test_rejects_files_that_are_not_databases(tmp_path):
    empty = tmp_path / "empty.mmdb"
    empty.write_bytes(b"")
    with pytest.raises(mmdb.InvalidDatabaseError, match="empty"):
        mmdb.Reader(empty)
    html = tmp_path / "error.html"
    html.write_text("<html>404 Not Found</html>", encoding="utf-8")
    with pytest.raises(mmdb.InvalidDatabaseError, match="no MaxMind DB metadata"):
        mmdb.Reader(html)
    odd = mmdb_writer.write(tmp_path / "odd.mmdb", [("10.1.0.0/16", "x")], metadata={"record_size": Typed(5, 26)})
    with pytest.raises(mmdb.InvalidDatabaseError, match="record size"):
        mmdb.Reader(odd)
    future = mmdb_writer.write(
        tmp_path / "v3.mmdb", [("10.1.0.0/16", "x")], metadata={"binary_format_major_version": Typed(5, 3)}
    )
    with pytest.raises(mmdb.InvalidDatabaseError, match="format version"):
        mmdb.Reader(future)


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (b"\x45ab", "past the end"),  # a 5-byte string with 2 bytes left
        (b"\x5f\x01", "past the end"),  # a size escape cut short
        (b"\x00\x00", "invalid extended type"),
        (b"\x00\x05", "unexpected type 12"),  # the data cache container never appears in data
        (b"\x64" + bytes(4), "a double has 4 bytes"),
    ],
)
def test_damaged_values_are_invalid(raw, message):
    with pytest.raises(mmdb.InvalidDatabaseError, match=message):
        mmdb.Decoder(raw, 0).decode(0)


def test_not_an_address_is_a_value_error(tmp_path):
    path = mmdb_writer.write(tmp_path / "city.mmdb", [("81.2.69.0/24", LONDON)])
    with mmdb.Reader(path) as reader, pytest.raises(ValueError):
        reader.get("not-an-ip")
