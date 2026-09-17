"""Regression coverage for the time defects in the September code review."""

from datetime import datetime, timedelta

import pytest

from src.relative_time_parser import (
    AmbiguityCode, parse_relative_datetime_detail, parse_time_range,
)


@pytest.mark.parametrize("base", [
    datetime(2026, 9, 17, 23, 30), datetime(2026, 9, 30, 23, 30),
    datetime(2026, 12, 31, 23, 30),
])
@pytest.mark.parametrize("text,minutes", [("60分钟后", 60), ("两小时后", 120)])
def test_clock_offsets_carry_the_date_without_meridiem_ambiguity(base, text, minutes):
    result = parse_time_range(text, "1小时", None, base_dt=base)
    assert result.success, result.error_detail
    assert result.start_time.iso_string == (base + timedelta(minutes=minutes)).isoformat()


@pytest.mark.parametrize("number,days", [
    ("13", 13), ("十三", 13), ("23", 23), ("二十三", 23), ("103", 103),
])
def test_complete_day_count_is_not_truncated_to_three(number, days):
    base = datetime(2026, 9, 17, 9)
    result = parse_relative_datetime_detail(f"{number}天后上午9点", base_dt=base)
    assert result.success
    assert result.target_local_datetime == base + timedelta(days=days)


def test_relative_date_and_matching_weekday_are_one_anchor():
    result = parse_relative_datetime_detail("明天周五上午9点", datetime(2026, 9, 17))
    assert result.iso_string == "2026-09-18T09:00:00"
    assert not result.ambiguities


def test_relative_date_and_wrong_weekday_require_correction():
    base = datetime(2026, 9, 17)
    result = parse_relative_datetime_detail("明天周六上午9点", base)
    assert result.iso_string == "2026-09-18T09:00:00"
    assert AmbiguityCode.DATE_WEEKDAY_CONFLICT in {a.code for a in result.ambiguities}
    assert not parse_time_range("明天周六上午9点", "1小时", None, base_dt=base).success


@pytest.mark.parametrize("clock", [
    "25点", "125点", "二十五点", "12:99", "12:009", "12:30:99",
    "99:30", "12点99分", "12点九十九分", "12点30分99秒",
])
def test_invalid_clock_is_rejected_without_clamping_or_partial_match(clock):
    base = datetime(2026, 9, 17)
    assert not parse_relative_datetime_detail(f"明天{clock}", base).success
    assert not parse_time_range(f"明天{clock}", "1小时", None, base_dt=base).success


@pytest.mark.parametrize("clock,expected", [
    ("23:59:59", "23:59:59"), ("凌晨0点", "00:00:00"),
    ("下午3点一刻", "15:15:00"), ("早上六点十五分", "06:15:00"),
])
def test_valid_clocks_remain_supported(clock, expected):
    result = parse_relative_datetime_detail(f"明天{clock}", datetime(2026, 9, 17))
    assert result.iso_string == f"2026-09-18T{expected}"
