"""Tests for `compute_initial_fires_at` and the relative-duration parser."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from lifeman.scheduler import (
    _compute_next_fire,
    _parse_relative_duration,
    compute_initial_fires_at,
)


# ---------------------------------------------------------------------------
# _parse_relative_duration
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,seconds", [
    ("30s", 30),
    ("30", 30),
    ("5m", 300),
    ("5min", 300),
    ("5 mins", 300),
    ("2h", 7200),
    ("2hr", 7200),
    ("1d", 86400),
    ("1 day", 86400),
    ("  60  ", 60),
])
def test_parse_relative_duration_accepts_common_forms(text: str, seconds: int):
    assert _parse_relative_duration(text) == timedelta(seconds=seconds)


@pytest.mark.parametrize("bad", ["", "abc", "5x", "-5s", "5.5s", "tomorrow"])
def test_parse_relative_duration_rejects_garbage(bad: str):
    assert _parse_relative_duration(bad) is None


# ---------------------------------------------------------------------------
# compute_initial_fires_at — relative forms
# ---------------------------------------------------------------------------

def _approx_seconds_from_now(iso: str) -> float:
    dt = datetime.fromisoformat(iso)
    return (dt - datetime.now(timezone.utc)).total_seconds()


def test_int_seconds():
    out = compute_initial_fires_at(60)
    assert 55 <= _approx_seconds_from_now(out) <= 65


def test_float_seconds():
    out = compute_initial_fires_at(0.5)
    assert -1 <= _approx_seconds_from_now(out) <= 2


def test_relative_string():
    out = compute_initial_fires_at("5m")
    assert 295 <= _approx_seconds_from_now(out) <= 305


def test_in_seconds_object():
    out = compute_initial_fires_at({"in_seconds": 120})
    assert 115 <= _approx_seconds_from_now(out) <= 125


def test_in_object_with_unit():
    out = compute_initial_fires_at({"in": "2h"})
    assert 7195 <= _approx_seconds_from_now(out) <= 7205


def test_bool_rejected():
    # bool is an int subclass — must not be silently accepted as 1 second.
    with pytest.raises(ValueError):
        compute_initial_fires_at(True)


# ---------------------------------------------------------------------------
# compute_initial_fires_at — ISO timestamps
# ---------------------------------------------------------------------------

def test_iso_future_timestamp_passes_through():
    target = datetime.now(timezone.utc) + timedelta(hours=1)
    out = compute_initial_fires_at(target.isoformat())
    assert datetime.fromisoformat(out) == target


def test_iso_z_suffix_normalized_to_utc():
    target = datetime.now(timezone.utc) + timedelta(minutes=30)
    z_form = target.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    out = compute_initial_fires_at(z_form)
    assert datetime.fromisoformat(out).tzinfo is not None


def test_iso_naive_rejected():
    naive = (datetime.now() + timedelta(hours=1)).isoformat()
    with pytest.raises(ValueError, match="missing a timezone"):
        compute_initial_fires_at(naive)


def test_iso_past_rejected():
    past = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    with pytest.raises(ValueError, match="in the past"):
        compute_initial_fires_at(past)


def test_iso_garbage_rejected():
    with pytest.raises(ValueError, match="invalid"):
        compute_initial_fires_at("not a date")


# ---------------------------------------------------------------------------
# compute_initial_fires_at — recurrence
# ---------------------------------------------------------------------------

def test_daily_recurrence_picks_future_target():
    out = compute_initial_fires_at({"recur": "daily", "at": "08:00"})
    dt = datetime.fromisoformat(out)
    assert dt > datetime.now(timezone.utc)
    assert dt.hour == 8 and dt.minute == 0


def test_hourly_recurrence_returns_valid_timestamp():
    # Note: compute_initial_fires_at picks today's "at" and rolls one unit
    # forward — for hourly the minute matters, the hour is whatever falls
    # out of that. Just verify we got a parseable UTC timestamp.
    out = compute_initial_fires_at({"recur": "hourly", "at": "00:30"})
    dt = datetime.fromisoformat(out)
    assert dt.tzinfo is not None
    assert dt.minute == 30


def test_unknown_dict_rejected():
    with pytest.raises(ValueError, match="unrecognized"):
        compute_initial_fires_at({"foo": "bar"})


# ---------------------------------------------------------------------------
# _compute_next_fire (recurring schedules)
# ---------------------------------------------------------------------------

def test_compute_next_fire_one_shot_returns_none():
    # Plain ISO timestamp (string, doesn't start with "{") => one-shot.
    assert _compute_next_fire("2030-01-01T00:00:00+00:00") is None


def test_compute_next_fire_invalid_json_returns_none():
    assert _compute_next_fire("{not-json") is None


def test_compute_next_fire_daily():
    nxt = _compute_next_fire('{"recur": "daily", "at": "08:00"}')
    assert nxt is not None
    dt = datetime.fromisoformat(nxt)
    assert dt.hour == 8 and dt.minute == 0
    assert dt > datetime.now(timezone.utc)
