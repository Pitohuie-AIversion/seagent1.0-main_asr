"""The live demo's split time turns keep grounded endpoints across later edits."""

from datetime import datetime, timedelta

import pytest

from src.dialogue_manager import DialogueManager
from src.extraction.extractor import ParameterExtractor
from src.knowledge_retriever import KnowledgeBase
from src.temporal.temporal_parser import TemporalParser
from tests.interaction_plan_support import (
    ScriptedLLM,
    extraction_result,
    make_plan,
    slot_candidate,
)


@pytest.mark.parametrize(
    "patch_v2,norm_v2",
    [(False, False), (True, False), (True, True)],
    ids=["legacy", "patch-v2", "normalization-v2"],
)
@pytest.mark.parametrize("model_start", [None, "2026-10-21T16:15:54"])
def test_separate_offset_and_duration_turns_preserve_saved_time_evidence(
    monkeypatch, tmp_path, patch_v2, norm_v2, model_start,
):
    monkeypatch.setenv("SEAGENT_RESULT_DIR", str(tmp_path))
    clock = [datetime(2026, 10, 21, 14, 15, 54)]
    monkeypatch.setattr(
        "src.temporal.simulated_time.get_current_datetime", lambda: clock[0],
    )
    monkeypatch.setattr("src.dialogue_manager.is_task_patch_v2_enabled", lambda: patch_v2)
    monkeypatch.setattr("src.dialogue_manager.is_normalization_contract_v2_enabled", lambda: norm_v2)
    llm = ScriptedLLM(default_plan=make_plan("WRITE"), default_reply="收到。")
    manager = DialogueManager(llm=llm, kb=KnowledgeBase())
    manager.slot_store.init_task_slots(manager.builder.get_schema("pipeline_inspection", "normal"))
    slots = manager.slot_store.clone_slots()
    for key, value in {
        "task_type_key": "pipeline_inspection",
        "task_type": "管缆巡检",
        "cable_type": "电力电缆",
    }.items():
        slots[key].value = value
        slots[key].status = "valid"
    manager.slot_store.commit_transaction(slots, [], request_id="seed-live-time")
    manager._rebuild_cache()
    manager.phase = "collecting"

    # An omitted value or a plausible-looking but incorrect ISO must not
    # replace the spoken relative offset with the model's clock arithmetic.
    llm.queue_extraction(extraction_result(*(
        [slot_candidate("start_time", model_start, raw_value="1小时后")]
        if model_start is not None else []
    )))
    manager.process("1小时后开始。")
    assert manager.task_state["start_time"] == "2026-10-21T15:15:54"
    assert manager.slot_store.slots["start_time"].raw_value == "1小时后"
    start_version = manager.slot_store.slots["start_time"].version
    assert not manager.task_state.get("end_time")

    # The next answer is anchored to the committed start, even after time has
    # advanced and even when the model invents a different absolute endpoint.
    clock[0] += timedelta(minutes=2)
    extracted = extraction_result(
        slot_candidate("end_time", "2026-10-21T19:15:54", raw_value="持续3小时"),
    )
    extracted["time_relation"] = {
        "has_duration": True, "action": "SET", "target": "duration",
        "duration_seconds": 10800, "raw_text": "持续3小时", "confidence": 0.99,
    }
    llm.queue_extraction(extracted)
    manager.process("持续3小时。")
    assert manager.task_state["start_time"] == "2026-10-21T15:15:54"
    assert manager.task_state["end_time"] == "2026-10-21T18:15:54"
    assert manager.slot_store.slots["start_time"].version == start_version
    assert manager.slot_store.slots["end_time"].raw_value == "持续3小时"
    end_version = manager.slot_store.slots["end_time"].version

    clock[0] += timedelta(hours=1)
    llm.queue_extraction(extraction_result(slot_candidate("water_depth", 300, raw_value="300米")))
    manager.process("水深300米。")
    assert manager.task_state["water_depth"] == 300
    assert manager.task_state["start_time"] == "2026-10-21T15:15:54"
    assert manager.task_state["end_time"] == "2026-10-21T18:15:54"
    assert manager.slot_store.slots["start_time"].version == start_version
    assert manager.slot_store.slots["end_time"].version == end_version
    assert manager.slot_store.unresolved == []


@pytest.mark.parametrize("key", ["start_time", "end_time"])
@pytest.mark.parametrize("message,raw,expected", [
    ("1小时后。", "1小时后", "2026-10-21T15:15:54"),
    ("半小时后。", "半小时后", "2026-10-21T14:45:54"),
    ("1小时30分钟后。", "30分钟后", "2026-10-21T15:45:54"),
])
def test_bare_offset_answer_overrides_incorrect_model_iso(monkeypatch, key, message, raw, expected):
    monkeypatch.setattr(
        "src.temporal.simulated_time.get_current_datetime",
        lambda: datetime(2026, 10, 21, 14, 15, 54),
    )
    llm = ScriptedLLM(extractions=[extraction_result(
        slot_candidate(key, "2026-10-21T20:15:54", raw_value=raw),
    )])
    result = ParameterExtractor(llm).extract_updates(
        message, {}, "pipeline_inspection",
        required=[{"key": field, "label": field, "type": "datetime"}
                  for field in ("start_time", "end_time")],
    )
    assert result["unresolved"] == []
    candidates = {item["canonical_key"]: item for item in result["slot_candidates"]}
    assert candidates[key]["normalized_value"] == expected
    assert candidates[key]["resolution_method"] == "relative_date_parsed"


@pytest.mark.parametrize("message,raw", [
    ("水深300米。", "1小时后"),
    ("1小时或2小时后。", "2小时后"),
    ("不要1小时后。", "1小时后"),
    ("如果1小时后开始呢？", "1小时后"),
    ("摄像机1小时后启动。", "1小时后"),
])
def test_relative_candidate_needs_unambiguous_current_task_evidence(monkeypatch, message, raw):
    monkeypatch.setattr(
        "src.temporal.simulated_time.get_current_datetime",
        lambda: datetime(2026, 10, 21, 14, 15, 54),
    )
    llm = ScriptedLLM(extractions=[extraction_result(
        slot_candidate("start_time", "2026-10-21T20:15:54", raw_value=raw),
    )])
    result = ParameterExtractor(llm).extract_updates(
        message, {}, "pipeline_inspection",
        required=[{"key": field, "label": field, "type": "datetime"}
                  for field in ("start_time", "end_time")],
    )
    assert not any(item["canonical_key"] in ("start_time", "end_time")
                   for item in result["slot_candidates"])


def test_duration_without_start_does_not_commit_model_invented_end(monkeypatch):
    monkeypatch.setattr(
        "src.temporal.simulated_time.get_current_datetime",
        lambda: datetime(2026, 10, 21, 14, 15, 54),
    )
    llm = ScriptedLLM(extractions=[extraction_result(
        slot_candidate("end_time", "2026-10-21T19:15:54", raw_value="持续3小时"),
    )])
    result = ParameterExtractor(llm).extract_updates(
        "持续3小时。", {}, "pipeline_inspection",
        required=[{"key": field, "label": field, "type": "datetime"}
                  for field in ("start_time", "end_time")],
    )
    assert result["slot_candidates"] == []
    assert any("缺少开始时间" in message for message in result["unresolved"])


@pytest.mark.parametrize("shift,conflict", [(1, False), (2, True)])
def test_explicit_end_adjustment_remains_a_duration_constraint(monkeypatch, shift, conflict):
    monkeypatch.setattr(
        "src.temporal.simulated_time.get_current_datetime",
        lambda: datetime(2026, 10, 21, 14),
    )
    candidates, unresolved = TemporalParser.materialize_time_relation(
        [], None,
        {"start_time": "2026-10-21T15:00:00", "end_time": "2026-10-21T17:00:00"},
        {"start_time", "end_time"},
        f"结束时间推迟{shift}小时，持续3小时。",
    )
    if conflict:
        assert candidates == []
        assert unresolved
    else:
        assert unresolved == []
        assert next(item["normalized_value"] for item in candidates
                    if item["canonical_key"] == "end_time") == "2026-10-21T18:00:00"


@pytest.mark.parametrize("text,expected", [
    ("1小时后。", False),
    ("半小时后开始。", False),
    ("1小时30分钟后。", False),
    ("一天两小时后开始。", False),
    ("1小时。", True),
    ("持续3小时。", True),
    ("1小时后开始持续4小时。", True),
    ("任务1小时后开始持续4小时30分钟后结束。", True),
])
def test_offset_evidence_is_not_execution_duration(text, expected):
    assert TemporalParser.has_explicit_duration(text) is expected


@pytest.mark.parametrize("end_raw", ["1小时后", "1小时后。", "持续1小时", "2026-10-21T17:15:54"])
@pytest.mark.parametrize("model_has_duration", [False, True])
@pytest.mark.parametrize("existing_window", [False, True])
def test_bare_offset_cannot_invent_duration_or_a_second_endpoint(
    monkeypatch, end_raw, model_has_duration, existing_window,
):
    monkeypatch.setattr(
        "src.temporal.simulated_time.get_current_datetime",
        lambda: datetime(2026, 10, 21, 14, 15, 54),
    )
    output = extraction_result(
        slot_candidate("start_time", "2026-10-21T20:15:54", raw_value="1小时后"),
        slot_candidate("end_time", "2026-10-21T21:15:54", raw_value=end_raw),
    )
    output["time_relation"] = {
        "has_duration": True, "action": "SET", "target": "duration",
        "duration_seconds": 3600, "raw_text": "1小时后。", "confidence": 0.95,
    } if model_has_duration else {"has_duration": False}
    state = {"start_time": "2026-10-21T15:00:00", "end_time": "2026-10-21T18:00:00"} if existing_window else {}
    result = ParameterExtractor(ScriptedLLM(extractions=[output])).extract_updates(
        "1小时后。", state, "pipeline_inspection",
        required=[{"key": field, "label": field, "type": "datetime"}
                  for field in ("start_time", "end_time")],
    )
    assert result["unresolved"] == []
    expected = {"start_time": "2026-10-21T15:15:54"}
    if existing_window:
        expected["end_time"] = "2026-10-21T18:15:54"
    assert {item["canonical_key"]: item["normalized_value"]
            for item in result["slot_candidates"]} == expected
