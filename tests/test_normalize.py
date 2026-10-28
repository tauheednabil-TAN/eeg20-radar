from datetime import UTC, date, datetime

import pytest

from eeg20_radar.normalize import (
    parse_coordinates,
    parse_date,
    parse_kw,
    parse_timestamp,
    split_ids,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2006-03-15", date(2006, 3, 15)),
        ("2006-03-15T00:00:00", date(2006, 3, 15)),
        ("15.03.2006", date(2006, 3, 15)),
        (" 2006-03-15 ", date(2006, 3, 15)),
    ],
)
def test_parse_date_accepts_iso_and_german(raw, expected):
    assert parse_date(raw) == (expected, None)


@pytest.mark.parametrize("raw", ["2007-13-45", "2006-02-30", "next year", "2006/03/15"])
def test_parse_date_rejects_malformed(raw):
    value, error = parse_date(raw)
    assert value is None and error


def test_parse_date_missing():
    assert parse_date(None) == (None, "missing")
    assert parse_date("  ") == (None, "missing")


def test_timestamp_without_offset_is_berlin_local_time():
    ts, error = parse_timestamp("2024-03-01T09:00:00.1234567")
    assert error is None
    assert ts == datetime(2024, 3, 1, 8, 0, 0, 123456, tzinfo=UTC)


def test_timestamp_with_offset_and_summer_time():
    assert parse_timestamp("2024-07-01T12:00:00Z")[0] == datetime(2024, 7, 1, 12, tzinfo=UTC)
    assert parse_timestamp("2024-07-01T12:00:00")[0] == datetime(2024, 7, 1, 10, tzinfo=UTC)


@pytest.mark.parametrize(("raw", "kw"), [("9.8", 9.8), ("9,8", 9.8), ("0", 0.0), (None, None)])
def test_parse_kw(raw, kw):
    assert parse_kw(raw) == (kw, None)


@pytest.mark.parametrize("raw", ["-5", "abc", "nan"])
def test_parse_kw_invalid(raw):
    value, error = parse_kw(raw)
    assert value is None and error


def test_coordinates():
    assert parse_coordinates("50.9375", "6.9603") == ((50.9375, 6.9603), None)
    assert parse_coordinates(None, None) == (None, None)
    assert parse_coordinates("0", "0")[1].startswith("coordinates outside Germany")
    assert parse_coordinates("50.1", None)[1] == "only one coordinate present"


def test_split_ids():
    assert split_ids("SEE1, SEE2;SEE3") == ["SEE1", "SEE2", "SEE3"]
    assert split_ids(None) == []
