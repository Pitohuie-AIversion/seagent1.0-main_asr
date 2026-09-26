"""Time extraction regressions reproduced from the September user journeys."""

import copy
from datetime import datetime
from types import SimpleNamespace

import pytest

from src.extraction.extractor import ParameterExtractor
from src.temporal.relative_time_parser import parse_relative_datetime, parse_time_range


@pytest.fixture
def now(monkeypatch):
    value = datetime(2026, 9, 16, 17, 37, 30, 456789)
    monkeypatch.setattr("src.temporal.simulated_time.get_current_datetime", lambda: value)
    return value


def candidate(key, raw, value):
    return {"canonical_key": key, "raw_key": key, "raw_value": raw,
            "normalized_value": value, "confidence": 0.95}


def extract(message, candidates, relation=None, state=None):
    llm = SimpleNamespace(
        extract_json=lambda *args, **kwargs: {
            "slot_candidates": copy.deepcopy(candidates), "list_mutations": [],
            "time_relation": copy.deepcopy(relation), "unresolved": [],
        },
        extract_temporal_relation=lambda *args, **kwargs: {"has_duration": False},
    )
    return ParameterExtractor(llm).extract_updates(
        message, current_state=state or {}, task_type_key="pipeline_inspection",
        required=[{"key": "start_time", "type": "datetime"},
                  {"key": "end_time", "type": "datetime"}],
    )


def times(result):
    return {item["canonical_key"]: item["normalized_value"] for item in result["slot_candidates"]}


@pytest.mark.parametrize("day,expected", [("明天", "2026-09-17"), ("后天", "2026-09-18")])
def test_relative_end_date_uses_current_day_once(now, day, expected):
    result = parse_time_range(f"{day}上午9点", "4小时", f"{day}下午1点", base_dt=now)
    assert result.success, result.error_detail
    assert result.start_time.iso_string == f"{expected}T09:00:00"
    assert result.end_time.iso_string == f"{expected}T13:00:00"


def test_extractor_keeps_morning_and_afternoon_candidates_separate(now):
    result = extract("明天上午9点开始，明天下午1点结束", [
        candidate("start_time", "明天上午9点", "2026-09-17T09:00:00"),
        candidate("end_time", "明天下午1点", "2026-09-17T13:00:00"),
    ])
    assert result["unresolved"] == []
    assert times(result) == {"start_time": "2026-09-17T09:00:00", "end_time": "2026-09-17T13:00:00"}


@pytest.mark.parametrize("raw_duration,seconds", [
    ("开始时间是2026年9月18日上午8点，结束时间是2026年9月18日中午12点。", 14400),
    ("持续8小时", 28800),
])
def test_explicit_burial_times_ignore_ungrounded_model_duration(now, raw_duration, seconds):
    message = ("开始时间是2026年9月18日上午8点，结束时间是2026年9月18日中午12点。"
               "起点为东经115.2度北纬20.1度，终点为东经115.3度北纬20.2度。")
    result = extract(message, [
        candidate("start_time", "2026年9月18日上午8点", "2026-09-18T08:00:00"),
        candidate("end_time", "2026年9月18日中午12点", "2026-09-18T12:00:00"),
    ], {"has_duration": True, "raw_text": raw_duration, "duration_seconds": seconds})
    assert result["unresolved"] == []
    assert times(result) == {"start_time": "2026-09-18T08:00:00", "end_time": "2026-09-18T12:00:00"}


def test_valve_explicit_end_change_does_not_inherit_or_invent_duration(now):
    result = extract("先不发布。井口改为A04，结束时间延长到明天下午1点，其他参数保持不变。", [
        candidate("end_time", "明天下午1点", "2026-09-17T13:00:00"),
    ], {"has_duration": True, "raw_text": "延长1小时", "duration_seconds": 3600,
        "action": "ADD", "target": "end_time"},
        {"start_time": "2026-09-17T09:00:00", "end_time": "2026-09-17T12:00:00"})
    assert result["unresolved"] == []
    assert times(result) == {"end_time": "2026-09-17T13:00:00"}


def test_now_and_one_hour_later_use_one_current_clock_after_model_latency(now):
    result = extract("将这份草稿的开始时间改为现在，结束时间改为一小时后，其他信息保持不变。", [
        candidate("start_time", "现在", "2026-09-16T17:36:38"),
        candidate("end_time", "一小时后", "2026-09-16T18:36:38"),
    ], {"has_duration": True, "raw_text": "一小时", "duration_seconds": 3600,
        "action": "SET", "target": "duration"},
        {"start_time": "2026-09-17T09:00:00", "end_time": "2026-09-17T13:00:00"})
    assert result["unresolved"] == []
    assert times(result) == {"start_time": "2026-09-16T17:37:30", "end_time": "2026-09-16T18:37:30"}


def test_explicit_duration_conflict_does_not_return_end_as_a_valid_update(now):
    result = extract("明天上午9点开始，持续2小时，明天下午1点结束", [
        candidate("start_time", "明天上午9点", "2026-09-17T09:00:00"),
        candidate("end_time", "明天下午1点", "2026-09-17T13:00:00"),
    ], {"has_duration": True, "raw_text": "持续2小时", "duration_seconds": 7200})
    assert "开始时间、持续时间和结束时间不一致" in result["unresolved"]
    assert "end_time" not in times(result)


def test_explicit_duration_increment_is_applied_before_end_consistency_check(now):
    result = extract("结束时间延长1小时，到明天下午1点结束", [
        candidate("end_time", "明天下午1点", "2026-09-17T13:00:00"),
    ], {"has_duration": True, "raw_text": "1小时", "duration_seconds": 3600,
        "action": "ADD", "target": "end_time"},
        {"start_time": "2026-09-17T09:00:00", "end_time": "2026-09-17T12:00:00"})
    assert result["unresolved"] == []
    assert times(result) == {"end_time": "2026-09-17T13:00:00"}


@pytest.mark.parametrize("message,action,expected", [
    ("持续时长再增加半小时", "ADD", "2026-09-17T12:30:00"),
    ("持续时长再减少半小时", "SUB", "2026-09-17T11:30:00"),
])
def test_half_hour_duration_delta_retains_filler_word_support(now, message, action, expected):
    result = extract(message, [], {
        "has_duration": True, "raw_text": message, "duration_seconds": 1800,
        "action": action, "target": "duration",
    }, {"start_time": "2026-09-17T09:00:00", "end_time": "2026-09-17T12:00:00"})
    assert result["unresolved"] == []
    assert times(result) == {"end_time": expected}


@pytest.mark.parametrize("raw,seconds", [
    ("2 days", 172800), ("两天半", 216000), ("一个半天", 129600),
    ("两个半小时", 9000), ("贰小时", 7200), ("半时", 1800),
    ("１．５小时", 5400), ("1天2小时30分钟", 95400),
    ("二点五小时", 9000), ("二点五分钟", 150),
    ("一周", 604800), ("一个星期", 604800), ("一刻钟", 900),
])
def test_duration_evidence_keeps_existing_formats_and_model_converted_units(now, raw, seconds):
    from datetime import timedelta

    result = extract(f"任务从2026年9月18日上午8点开始，持续{raw}", [
        candidate("start_time", "2026年9月18日上午8点", "2026-09-18T08:00:00"),
    ], {"has_duration": True, "raw_text": raw, "duration_seconds": seconds})
    assert result["unresolved"] == []
    expected = datetime(2026, 9, 18, 8) + timedelta(seconds=seconds)
    assert times(result)["end_time"] == expected.isoformat(timespec="seconds")


@pytest.mark.parametrize("raw,full,expected", [
    ("现在", "现在开始，2026年9月18日结束", "2026-09-16T17:37:30"),
    ("一小时后", "一小时后开始，2026年9月18日结束", "2026-09-16T18:37:30"),
    ("明天上午9点", "明天上午9点开始，2026年9月18日下午1点结束", "2026-09-17T09:00:00"),
    ("早上6点", "任务从9月18号早上6点开始，下午1点结束", "2026-09-18T06:00:00"),
])
def test_full_sentence_supplies_only_a_missing_date(now, raw, full, expected):
    assert parse_relative_datetime(raw, now, full_user_message=full) == expected


@pytest.mark.parametrize("raw_start,raw_end,start,end", [
    ("明天上午九点", "十一点", "09:00:00", "11:00:00"),
    ("明天上午十点", "明天上午十点到十二点", "10:00:00", "12:00:00"),
])
def test_current_turn_normalized_interval_resolves_provisional_raw_errors(now, raw_start, raw_end, start, end):
    spoken_range = raw_end if "到" in raw_end else f"{raw_start}到{raw_end}"
    result = extract(f"安排任务，{spoken_range}。", [
        candidate("start_time", raw_start, f"2026-09-17T{start}"),
        candidate("end_time", raw_end, f"2026-09-17T{end}"),
    ])
    assert result["unresolved"] == []
    assert times(result) == {"start_time": f"2026-09-17T{start}", "end_time": f"2026-09-17T{end}"}


@pytest.mark.parametrize("state", [None, {
    "start_time": "2026-09-17T08:00:00", "end_time": "2026-09-17T18:00:00",
}])
def test_invalid_new_interval_keeps_error_even_with_valid_saved_interval(now, state):
    result = extract("明天上午十点开始，明天上午九点结束", [
        candidate("start_time", "明天上午十点", "2026-09-17T10:00:00"),
        candidate("end_time", "明天上午九点", "2026-09-17T09:00:00"),
    ], state=state)
    assert "结束时间必须晚于开始时间" in result["unresolved"]


def test_invalid_end_only_edit_remains_available_for_final_validation(now):
    from src.validation.validator import TaskValidator
    from tests.test_task_time_validation import FakeKnowledgeBase

    state = {"start_time": "2026-09-17T10:00:00", "end_time": "2026-09-17T12:00:00"}
    result = extract("结束时间改成明天上午九点", [
        candidate("end_time", "明天上午九点", "2026-09-17T09:00:00"),
    ], state=state)
    updated = {**state, **times(result)}
    assert updated["end_time"] == "2026-09-17T09:00:00"
    violations = TaskValidator(FakeKnowledgeBase()).validate(updated)
    assert "C031" in {violation.constraint_id for violation in violations}


@pytest.mark.parametrize("message,raw_end,normalized_end", [
    ("明天上午十点开始，明天上午九点结束", "明天上午九点", "2026-09-17T12:00:00"),
    ("明天上午十点到九点", "九点", "2026-09-17T12:00:00"),
    ("明天上午十点到十二点", "明天上午十点到十二点", "2026-09-17T13:00:00"),
    ("明天上午十点到十一点", "十一点", "2026-09-17T12:00:00"),
])
def test_model_iso_cannot_clear_an_error_without_matching_spoken_endpoint(now, message, raw_end, normalized_end):
    result = extract(message, [
        candidate("start_time", "明天上午十点", "2026-09-17T10:00:00"),
        candidate("end_time", raw_end, normalized_end),
    ])
    assert result["unresolved"], "A valid model ISO must not override conflicting user time evidence"


def test_real_valve_request_uses_spoken_endpoint_when_model_expands_raw(monkeypatch):
    monkeypatch.setattr("src.temporal.simulated_time.get_current_datetime",
                        lambda: datetime(2026, 9, 26, 17, 0, 7))
    # Captured from GET /api/session/state for audit-final-time_remove.
    message = "我要安排采油树控制面板拔出作业，明天上午十点到十二点，井口编号A03，水深300米。"
    result = extract(message, [
        candidate("start_time", "明天上午十点", "2026-09-27T10:00:00"),
        candidate("end_time", "明天上午十二点", "2026-09-27T12:00:00"),
    ])
    assert result["unresolved"] == []
    assert times(result) == {"start_time": "2026-09-27T10:00:00", "end_time": "2026-09-27T12:00:00"}


def test_real_range_evidence_still_rejects_wrong_model_endpoint(monkeypatch):
    monkeypatch.setattr("src.temporal.simulated_time.get_current_datetime",
                        lambda: datetime(2026, 9, 26, 17, 0, 7))
    message = "我要安排采油树控制面板拔出作业，明天上午十点到九点，井口编号A03，水深300米。"
    result = extract(message, [
        candidate("start_time", "明天上午十点", "2026-09-27T10:00:00"),
        candidate("end_time", "明天上午十二点", "2026-09-27T12:00:00"),
    ])
    assert "结束时间必须晚于开始时间" in result["unresolved"]
