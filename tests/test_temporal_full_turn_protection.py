"""Complete-turn restrictions must precede deterministic time recovery."""

import copy
from datetime import datetime

import pytest

from src.dialogue_manager import DialogueManager
from src.knowledge_retriever import KnowledgeBase
from src.temporal.temporal_parser import TemporalParser
from tests.interaction_plan_support import ScriptedLLM, make_plan
from tests.test_explicit_time_field_recovery import extract
from tests.test_time_relation_protocol import candidate, extraction, relation


STATE = {"start_time": "2026-10-21T09:00:00", "end_time": "2026-10-21T12:00:00"}


@pytest.fixture(autouse=True)
def fixed_time(monkeypatch):
    monkeypatch.setattr("src.temporal.simulated_time.get_current_datetime", lambda: datetime(2026, 10, 19, 17, 0))


@pytest.mark.parametrize("message", [
    "不要改任何时间，持续半天两小时。",
    "持续半天两小时。不要修改任何时间。",
    "时间别动，持续半天两小时。",
    "请先不要调整时间参数，开始时间改为2026年10月21日10:00。",
    "先别修改，开始时间改为2026年10月21日10:00。",
    "只讨论，持续半天两小时。",
    "只讨论时间，持续半天两小时。",
    "只讨论时间，开始时间改为2026年10月21日10:00。",
    "不要把任何时间改动，持续半天两小时。",
    "只是讨论持续半天两小时。",
    "我只想讨论持续半天两小时。",
    "先别修改开始时间为2026年10月21日10:00。",
    "只讨论开始时间改为2026年10月21日10:00。",
    "持续半天两小时吗？",
])
@pytest.mark.parametrize("model_mode", ["empty", "relation", "wrong_candidates"])
def test_extractor_never_recovers_rejected_time_edits(message, model_mode):
    candidates = []
    rel = None
    if model_mode != "empty":
        rel = relation(raw="半天两小时", seconds=50400)
    if model_mode == "wrong_candidates":
        raw = "2026年10月21日10:00" if "10:00" in message else "半天两小时"
        candidates = [candidate("start_time", raw, "2026-10-21T10:00:00"),
                      candidate("end_time", raw, "2026-10-21T23:00:00")]
    before = copy.deepcopy(STATE)
    result = extract(message, candidates, rel, STATE)
    assert result["slot_candidates"] == []
    assert STATE == before


@pytest.fixture(params=[(False, False), (True, False), (True, True)], ids=["legacy", "patch-v2", "norm-v2"])
def manager_and_llm(request, monkeypatch, tmp_path):
    patch_v2, norm_v2 = request.param
    monkeypatch.setenv("SEAGENT_RESULT_DIR", str(tmp_path))
    monkeypatch.setattr("src.dialogue_manager.is_task_patch_v2_enabled", lambda: patch_v2)
    monkeypatch.setattr("src.dialogue_manager.is_normalization_contract_v2_enabled", lambda: norm_v2)
    llm = ScriptedLLM(default_plan=make_plan("WRITE"), default_reply="收到。")
    manager = DialogueManager(llm=llm, kb=KnowledgeBase())
    manager.slot_store.init_task_slots(manager.builder.get_schema("pipeline_inspection", "normal"))
    slots = manager.slot_store.clone_slots()
    seed = {"intent_id": "TI2026101901", "task_type_key": "pipeline_inspection", "task_type": "管缆巡检",
            "water_depth": 300, **STATE}
    for key, value in seed.items():
        slots[key].value, slots[key].status = value, "valid"
    manager.slot_store.commit_transaction(slots, [], request_id="seed-time-protection")
    manager._rebuild_cache()
    manager.phase = "collecting"
    return manager, llm


@pytest.mark.parametrize("message,model_mode", [
    ("不要改任何时间，持续半天两小时。", "empty"),
    ("持续半天两小时。不要修改任何时间。", "wrong"),
    ("时间别动，水深改为320米，持续半天两小时。", "depth"),
    ("只讨论持续半天两小时，水深改为320米。", "depth"),
    ("先别修改，开始时间改为2026年10月21日10:00。", "empty"),
    ("持续半天两小时吗？", "wrong"),
])
def test_full_turn_preserves_entire_time_slots(manager_and_llm, message, model_mode):
    manager, llm = manager_and_llm
    before = {key: manager.slot_store.slots[key].to_dict() for key in STATE}
    candidates = []
    rel = None
    if model_mode != "empty":
        candidates = [candidate("start_time", "半天两小时", "2026-10-21T10:00:00"),
                      candidate("end_time", "半天两小时", "2026-10-21T23:00:00")]
        rel = relation(raw="半天两小时", seconds=50400)
    if model_mode == "depth":
        candidates.append(candidate("water_depth", "320米", 320))
    llm.queue_extraction(extraction(candidates, rel))

    manager.process(message)

    assert {key: manager.slot_store.slots[key].to_dict() for key in STATE} == before
    if model_mode == "depth":
        assert manager.task_state["water_depth"] == 320
    restored = DialogueManager(llm=llm, kb=manager.kb)
    restored.load_snapshot(manager.export_snapshot())
    assert {key: restored.slot_store.slots[key].to_dict() for key in STATE} == before


@pytest.mark.parametrize("message", [
    "不要改水深，结束时间延长一小时。",
    "不要修改任务编号，结束时间延长一小时。",
    "先不发布，结束时间延长一小时。",
    "只讨论水深，结束时间延长一小时。",
])
def test_other_field_restrictions_keep_time_edit_authorized(manager_and_llm, message):
    manager, llm = manager_and_llm
    llm.queue_extraction(extraction(rel=relation("ADD", "end_time", "一小时", 3600)))
    manager.process(message)
    assert manager.task_state["end_time"] == "2026-10-21T13:00:00"
    assert manager.task_state["start_time"] == STATE["start_time"]
    assert manager.task_state["water_depth"] == 300


def test_discussed_duration_keeps_separate_explicit_endpoint():
    result = extract("只讨论持续半天两小时，结束时间改为2026年10月21日13:00。", [
        candidate("end_time", "半天两小时", "2026-10-21T23:00:00"),
    ], relation(raw="半天两小时", seconds=50400), STATE)
    assert result["unresolved"] == []
    assert {item["canonical_key"]: item["normalized_value"] for item in result["slot_candidates"]} == {
        "end_time": "2026-10-21T13:00:00",
    }


def test_pause_applies_even_to_start_only_schemas_without_mutating_candidates():
    candidates = [candidate("start_time", "2026年10月21日10:00", "2026-10-21T10:00:00"),
                  candidate("water_depth", "320米", 320)]
    before = copy.deepcopy(candidates)
    result, unresolved = TemporalParser.materialize_time_relation(
        candidates, None, STATE, {"start_time", "water_depth"},
        "不要改任何时间，开始时间改为2026年10月21日10:00，水深改320米。",
    )
    assert result == [before[1]]
    assert unresolved == []
    assert candidates == before


@pytest.mark.parametrize("model_candidates", [[], [
    candidate("start_time", "2026-10-21 09:00", "2026-10-21T09:00:00"),
    candidate("end_time", "12:00", "2026-10-21T12:00:00"),
]])
def test_range_recovery_cannot_reintroduce_discussed_clock_candidates(model_candidates):
    result = extract("只讨论2026-10-21 09:00到12:00。", model_candidates, state=STATE)
    assert result["slot_candidates"] == []
    assert result["unresolved"] == []
