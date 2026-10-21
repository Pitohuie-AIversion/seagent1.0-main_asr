"""Shared elapsed offsets and adjacent task-time statements use complete evidence."""

import copy
from datetime import datetime, timedelta

import pytest

from src.temporal.relative_time_parser import parse_relative_datetime_detail
from src.temporal.temporal_parser import TemporalParser


NOW = datetime(2026, 10, 20, 23, 0)
STATE = {"start_time": "2026-10-21T09:00:00", "end_time": "2026-10-21T12:00:00"}


@pytest.fixture(autouse=True)
def fixed_time(monkeypatch):
    monkeypatch.setattr("src.temporal.simulated_time.get_current_datetime", lambda: NOW)


def materialize(message, candidates=(), state=None):
    return TemporalParser.materialize_time_relation(
        list(candidates), None, state or {}, {"start_time", "end_time", "water_depth"}, message,
    )


def values(candidates):
    return {item["canonical_key"]: item["normalized_value"] for item in candidates}


@pytest.mark.parametrize("offset,seconds", [
    ("半小时后", 1800), ("0.5小时后", 1800), ("1.5小时后", 5400),
    ("一个半小时后", 5400), ("1小时半后", 5400), ("一点五小时后", 5400),
    ("1小时30分钟后", 5400), ("一小时三十分钟后", 5400),
    ("半小时15分钟后", 2700), ("1.5小时30分钟后", 7200),
    ("半分钟后", 30), ("1分钟30秒之后", 90), ("一天两小时后", 93600),
])
def test_complete_offset_is_shared_by_point_and_task_parsing(offset, seconds):
    expected_start = NOW + timedelta(seconds=seconds)
    result = parse_relative_datetime_detail(offset, NOW)
    assert result.success
    assert not result.has_ambiguities
    assert result.iso_string == expected_start.isoformat(timespec="seconds")

    candidates, unresolved = materialize(f"任务{offset}开始持续4小时  电力")
    assert unresolved == []
    assert values(candidates) == {
        "start_time": expected_start.isoformat(timespec="seconds"),
        "end_time": (expected_start + timedelta(hours=4)).isoformat(timespec="seconds"),
    }


@pytest.mark.parametrize("offset", [
    "1至2小时后", "1小时或2小时后", "1小时后或2小时后", "1小时2小时后",
    "30分钟1小时后", "-1小时后", "负1小时后", "1.5.5小时后", "999999999小时后",
])
def test_ambiguous_or_invalid_offset_never_accepts_a_suffix_or_old_start(offset):
    assert not parse_relative_datetime_detail(offset, NOW).success
    previous = copy.deepcopy(STATE)
    wrong = {"canonical_key": "start_time", "raw_value": "2小时后", "normalized_value": "2026-10-21T01:00:00"}
    candidates, unresolved = materialize(f"任务{offset}开始持续4小时", [wrong], previous)
    assert candidates == []
    assert unresolved
    assert previous == STATE


@pytest.mark.parametrize("message", [
    "开始时间为1小时后持续4小时",
    "开始时间为2026年10月21日00:00持续4小时",
    "任务1小时后开始持续4小时5小时后结束",
    "任务1小时后开始结束时间为5小时后持续4小时",
    "任务1小时后开始5小时后结束持续4小时",
])
def test_adjacent_endpoints_and_duration_are_independent_consistent_constraints(message):
    candidates, unresolved = materialize(message)
    assert unresolved == []
    assert values(candidates) == {"start_time": "2026-10-21T00:00:00", "end_time": "2026-10-21T04:00:00"}


@pytest.mark.parametrize("message", [
    "任务1小时后开始持续4小时6小时后结束",
    "任务1小时后开始结束时间为6小时后持续4小时",
    "任务1小时后开始持续4小时30分钟后结束",
])
def test_adjacent_explicit_end_cannot_be_swallowed_by_duration(message):
    candidates, unresolved = materialize(message)
    assert "end_time" not in values(candidates)
    assert unresolved


@pytest.mark.parametrize("message", [
    "任务1小时后开始2小时后开始持续4小时",
    "开始时间为半小时后，开始时间为1.5小时后，持续4小时",
    "任务1小时后开始持续4小时持续5小时",
])
def test_duplicate_time_declarations_reject_model_guess_and_preserve_saved_window(message):
    wrong = {"canonical_key": "start_time", "raw_value": "1小时后", "normalized_value": "2026-10-21T00:00:00"}
    candidates, unresolved = materialize(message, [wrong], STATE)
    assert candidates == []
    assert unresolved


@pytest.mark.parametrize("message", [
    "不要任务半小时后开始持续4小时",
    "如果任务1.5小时后开始持续4小时会怎样？",
    "任务1小时30分钟后开始持续4小时吗？",
    "设备半小时后开始持续4小时",
    "设备开始时间为半小时后，持续4小时",
    "摄像机半小时后启动持续4小时",
])
def test_nonassertive_and_other_equipment_offsets_are_not_task_endpoints(message):
    candidates, _ = materialize(message)
    assert "start_time" not in values(candidates)


def test_model_cannot_bind_an_equipment_offset_to_task_start():
    wrong = {"canonical_key": "start_time", "raw_value": "半小时后", "normalized_value": "2026-10-20T23:30:00"}
    candidates, _ = materialize("设备半小时后开始", [wrong])
    assert candidates == []


def test_offset_cannot_borrow_another_fields_calendar_date():
    result = parse_relative_datetime_detail("1.5小时后", NOW, full_user_message="明天下午3点结束")
    assert result.iso_string == "2026-10-21T00:30:00"


@pytest.mark.parametrize("text", ["1小时后下午3点", "13:30分后"])
def test_one_point_cannot_mix_elapsed_offset_and_clock(text):
    assert not parse_relative_datetime_detail(text, NOW).success


def test_bare_relative_answer_overrides_wrong_model_iso_before_end_derivation():
    wrong = {"canonical_key": "start_time", "raw_value": "1.5小时后", "normalized_value": "2026-10-21T04:00:00"}
    candidates, unresolved = materialize("1.5小时后，持续4小时", [wrong])
    assert unresolved == []
    assert values(candidates) == {"start_time": "2026-10-21T00:30:00", "end_time": "2026-10-21T04:30:00"}
