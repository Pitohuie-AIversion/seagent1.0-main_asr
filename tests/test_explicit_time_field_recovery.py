"""Real GPU dialogue regressions: an extractor can omit explicit time fields."""

import copy
from datetime import datetime
from types import SimpleNamespace

import pytest

from src.extraction.extractor import ParameterExtractor


@pytest.fixture(autouse=True)
def current_time(monkeypatch):
    monkeypatch.setattr("src.temporal.simulated_time.get_current_datetime", lambda: datetime(2026, 10, 15, 17, 0))


def candidate(key, raw, value):
    return {"canonical_key": key, "raw_key": key, "raw_value": raw,
            "normalized_value": value, "confidence": 0.95}


def extract(message, candidates=(), relation=None, state=None):
    llm = SimpleNamespace(
        extract_json=lambda *args, **kwargs: {
            "slot_candidates": copy.deepcopy(list(candidates)), "list_mutations": [],
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


@pytest.mark.parametrize("relation", [
    {"has_duration": True, "raw_text": "持续一个半天两小时", "duration_seconds": 136800},
    {"has_duration": True, "raw_text": "两小时", "duration_seconds": 7200},
    {"has_duration": False},
])
def test_full_request_recovers_omitted_start_and_complete_duration(relation):
    message = (
        "我想做管缆巡检，开始时间为2026年10月17日09:00，持续一个半天两小时，"
        "管缆类型为海底油气管道，水深300米，使用观察级深海机器人75HP。"
    )
    result = extract(message, relation=relation)
    assert result["unresolved"] == []
    assert times(result) == {"start_time": "2026-10-17T09:00:00", "end_time": "2026-10-18T23:00:00"}


@pytest.mark.parametrize("relation", [
    {"has_duration": True, "raw_text": "半天两小时", "duration_seconds": 7200},
    {"has_duration": True, "raw_text": "两小时", "duration_seconds": 7200},
    {"has_duration": False},
])
def test_full_request_recovers_bare_half_duration_even_when_model_drops_it(relation):
    result = extract(
        "我想做管缆巡检，开始时间为2026年10月17日09:00，持续半天两小时，"
        "水深300米，使用观察级深海机器人75HP。", relation=relation,
    )
    assert result["unresolved"] == []
    assert times(result) == {"start_time": "2026-10-17T09:00:00", "end_time": "2026-10-17T23:00:00"}


@pytest.mark.parametrize("model_candidates", [[], [candidate("end_time", "增加1小时", "2026-10-16T13:00:00")]])
@pytest.mark.parametrize("hour,conflict", [(15, True), (13, False)])
def test_explicit_end_is_checked_even_if_model_omits_or_replaces_it(model_candidates, hour, conflict):
    state = {"start_time": "2026-10-16T09:00:00", "end_time": "2026-10-16T12:00:00"}
    original = copy.deepcopy(state)
    result = extract(
        f"持续时长增加1小时，结束时间改成2026年10月16日{hour}:00。", model_candidates,
        {"has_duration": True, "raw_text": "增加1小时", "duration_seconds": 3600, "action": "ADD", "target": "duration"}, state,
    )
    assert state == original
    if conflict:
        assert "开始时间、持续时间和结束时间不一致" in result["unresolved"]
        assert "end_time" not in times(result)
    else:
        assert result["unresolved"] == []
        assert times(result)["end_time"] == "2026-10-16T13:00:00"


def test_each_labeled_date_belongs_to_its_endpoint():
    result = extract("开始时间为2026年10月17日23:00，结束时间为2026年10月18日02:00。")
    assert result["unresolved"] == []
    assert times(result) == {"start_time": "2026-10-17T23:00:00", "end_time": "2026-10-18T02:00:00"}


@pytest.mark.parametrize("message", [
    "不要把开始时间改为2026年10月17日09:00。",
    "如果开始时间为2026年10月17日09:00。",
    "开始时间为2026年10月17日09:00会怎样？",
    "开始时间是2026年10月17日09:00吗？",
    "开始时间为2026年10月17日09:00，开始时间为2026年10月18日09:00。",
    "设备检查记录时间为2026年10月17日09:00。",
    "开始时间为明天9点。",  # Meridiem is ambiguous; never guess.
])
def test_recovery_does_not_guess_nonassertive_ambiguous_or_unrelated_times(message):
    assert times(extract(message)) == {}


def test_negated_start_does_not_suppress_independent_explicit_end():
    result = extract(
        "不要把开始时间改为2026年10月17日10:00，结束时间改为2026年10月18日15:00。",
        state={"start_time": "2026-10-17T09:00:00", "end_time": "2026-10-17T12:00:00"},
    )
    assert result["unresolved"] == []
    assert times(result) == {"end_time": "2026-10-18T15:00:00"}


@pytest.mark.parametrize("message", ["不要持续两个半小时。", "如果持续两个半小时会怎样？", "持续两个半小时吗？"])
def test_no_duration_recovery_from_nonassertive_clause(message):
    result = extract(message, state={"start_time": "2026-10-17T09:00:00", "end_time": "2026-10-17T12:00:00"})
    assert times(result) == {}


@pytest.mark.parametrize("message", [
    "不要持续半天两小时。",
    "不要把持续时长改为半天两小时。",
    "如果持续半天两小时会怎样？",
    "持续半天两小时吗？",
    "持续半天两小时吗",
])
@pytest.mark.parametrize("model_infers_end", [False, True])
def test_nonassertive_half_duration_rejects_model_relation_and_inferred_end(message, model_infers_end):
    state = {"start_time": "2026-10-17T09:00:00", "end_time": "2026-10-17T12:00:00"}
    candidates = [candidate("end_time", "半天两小时", "2026-10-17T23:00:00")] if model_infers_end else []
    result = extract(message, candidates,
                     {"has_duration": True, "raw_text": "半天两小时", "duration_seconds": 50400}, state)
    assert result["unresolved"] == []
    assert times(result) == {}


def test_negated_half_duration_preserves_separate_explicit_end_change():
    result = extract("不要把持续时长改为半天两小时，结束时间改为2026年10月17日13:00。", [
        candidate("end_time", "半天两小时", "2026-10-17T23:00:00"),
    ], {"has_duration": True, "raw_text": "半天两小时", "duration_seconds": 50400},
        {"start_time": "2026-10-17T09:00:00", "end_time": "2026-10-17T12:00:00"})
    assert result["unresolved"] == []
    assert times(result) == {"end_time": "2026-10-17T13:00:00"}


@pytest.mark.parametrize("affirmative", ["持续3小时", "干3小时"])
def test_rejected_half_duration_keeps_independent_affirmative_duration(affirmative):
    result = extract(f"不要持续半天两小时，{affirmative}。", [
        candidate("end_time", "半天两小时", "2026-10-17T23:00:00"),
    ], {"has_duration": True, "raw_text": affirmative, "duration_seconds": 10800},
        {"start_time": "2026-10-17T09:00:00", "end_time": "2026-10-17T11:00:00"})
    assert result["unresolved"] == []
    assert times(result) == {"end_time": "2026-10-17T12:00:00"}


@pytest.mark.parametrize("patch_v2,norm_v2", [(False, False), (True, False), (True, True)])
def test_half_duration_user_journey_commits_and_preserves_saved_slots(monkeypatch, tmp_path, patch_v2, norm_v2):
    from src.dialogue_manager import DialogueManager
    from src.knowledge_retriever import KnowledgeBase
    from tests.interaction_plan_support import ScriptedLLM, make_plan
    from tests.test_time_relation_protocol import extraction, relation

    monkeypatch.setenv("SEAGENT_RESULT_DIR", str(tmp_path))
    monkeypatch.setattr("src.dialogue_manager.is_task_patch_v2_enabled", lambda: patch_v2)
    monkeypatch.setattr("src.dialogue_manager.is_normalization_contract_v2_enabled", lambda: norm_v2)
    llm = ScriptedLLM(default_plan=make_plan("WRITE"), default_reply="收到。")
    manager = DialogueManager(llm=llm, kb=KnowledgeBase())
    manager.slot_store.init_task_slots(manager.builder.get_schema("pipeline_inspection", "normal"))
    slots = manager.slot_store.clone_slots()
    seed = {"intent_id": "TI2026101501", "task_type_key": "pipeline_inspection", "task_type": "管缆巡检", "water_depth": 300,
            "start_time": "2026-10-17T09:00:00", "end_time": "2026-10-17T12:00:00"}
    for key, value in seed.items():
        slots[key].value, slots[key].status = value, "valid"
    manager.slot_store.commit_transaction(slots, [], request_id="seed-half-duration")
    manager._rebuild_cache()
    manager.phase = "collecting"

    llm.queue_extraction(extraction(rel=relation(raw="两小时", seconds=7200)))
    reply = manager.process("持续半天两小时。")
    assert manager.task_state["end_time"] == "2026-10-17T23:00:00"
    assert "23:00" in reply
    versions = {key: manager.slot_store.slots[key].version for key in ("start_time", "end_time")}

    llm.queue_extraction(extraction([
        candidate("water_depth", "320米", 320),
        candidate("start_time", "推迟半小时15分钟", "2026-10-17T09:45:00"),
        candidate("end_time", "推迟半小时15分钟", "2026-10-17T23:45:00"),
    ], relation("ADD", "start_time", "半小时15分钟", 2700)))
    manager.process("水深改320米，不要把开始时间推迟半小时15分钟。")
    assert manager.task_state["water_depth"] == 320
    assert manager.task_state["start_time"] == seed["start_time"]
    assert manager.task_state["end_time"] == "2026-10-17T23:00:00"
    assert {key: manager.slot_store.slots[key].version for key in versions} == versions

    llm.queue_extraction(extraction(rel=relation(raw="半小时15分钟", seconds=900)))
    manager.process("持续半小时15分钟吗？")
    assert manager.task_state["end_time"] == "2026-10-17T23:00:00"
    assert {key: manager.slot_store.slots[key].version for key in versions} == versions

    llm.queue_extraction(extraction(rel=relation(raw="半天两小时", seconds=7200)))
    manager.process("不要把持续时长改为半天两小时，结束时间改为2026年10月17日13:00。")
    assert manager.task_state["end_time"] == "2026-10-17T13:00:00"
    assert manager.task_state["start_time"] == seed["start_time"]
    assert manager.task_state["water_depth"] == 320
    assert all(manager.slot_store.slots[key].status == "valid" for key in versions)
    restored = DialogueManager(llm=llm, kb=manager.kb)
    restored.load_snapshot(manager.export_snapshot())
    assert restored.slot_store.get_task_state() == manager.slot_store.get_task_state()
