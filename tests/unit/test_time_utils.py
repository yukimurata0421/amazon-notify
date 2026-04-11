from __future__ import annotations

from datetime import UTC, datetime

from amazon_notify.time_utils import parse_utc_iso, utc_now_iso


def test_parse_utc_iso_accepts_z_suffix() -> None:
    dt = parse_utc_iso("2026-01-15T12:30:45Z")
    assert dt is not None
    assert dt.tzinfo == UTC
    assert dt.year == 2026


def test_parse_utc_iso_accepts_offset() -> None:
    dt = parse_utc_iso("2026-01-15T12:30:45+00:00")
    assert dt is not None
    assert dt.hour == 12


def test_utc_now_iso_roundtrip_parse() -> None:
    raw = utc_now_iso()
    parsed = parse_utc_iso(raw)
    assert parsed is not None
    assert isinstance(parsed, datetime)


def test_parse_utc_iso_rejects_non_string_and_empty() -> None:
    assert parse_utc_iso(None) is None
    assert parse_utc_iso("") is None
    assert parse_utc_iso("   ") is None
    assert parse_utc_iso("not iso") is None
    assert parse_utc_iso(object()) is None  # type: ignore[arg-type]


def test_parse_utc_iso_fills_naive_datetime() -> None:
    dt = parse_utc_iso("2026-01-15T12:30:45")
    assert dt is not None
    assert dt.tzinfo == UTC
