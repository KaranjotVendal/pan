from __future__ import annotations

from datetime import datetime

from pan.adapters.clock import SystemClock, UuidGen


def test_system_clock_returns_timezone_aware_datetime() -> None:
    now = SystemClock().now()

    assert isinstance(now, datetime)
    assert now.tzinfo is not None


def test_uuid_gen_returns_non_empty_string() -> None:
    new_id = UuidGen().new_id()

    assert isinstance(new_id, str)
    assert new_id != ""


def test_uuid_gen_two_calls_differ() -> None:
    generator = UuidGen()

    assert generator.new_id() != generator.new_id()
