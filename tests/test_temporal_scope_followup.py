"""Time corrections must respect local rejection, withdrawal and field context."""

import copy
from datetime import datetime
from types import SimpleNamespace

import pytest

from src.dialogue_manager import DialogueManager
from src.extraction.extractor import ParameterExtractor
from src.knowledge_retriever import KnowledgeBase
from src.temporal.relative_time_parser import parse_relative_datetime_detail
from src.temporal.temporal_parser import TemporalParser
from tests.interaction_plan_support import ScriptedLLM, extraction_result, make_plan, slot_candidate


NOW = datetime(2026, 10, 20, 23, 0, 37)
STATE = {"start_time": "2026-10-21T09:00:00", "end_time": "2026-10-21T12:00:00"}


@pytest.fixture(autouse=True)
def fixed_time(monkeypatch):
    monkeypatch.setattr("src.temporal.simulated_time.get_current_datetime", lambda: NOW)


def materialize(message, candidates=()):
    state = copy.deepcopy(STATE)
    before = copy.deepcopy(list(candidates))
    result, unresolved = TemporalParser.materialize_time_relation(
        list(candidates), None, state, {"start_time", "end_time"}, message,
    )
    assert state == STATE
    assert list(candidates) == before
    return {item["canonical_key"]: item["normalized_value"] for item in result}, unresolved


@pytest.mark.parametrize("rejection", [
    "不要把结束时间提前半小时", "取消结束时间提前半小时", "撤销结束时间提前半小时",
])
@pytest.mark.parametrize("rejection_first", [False, True])
def test_rejecting_another_delta_does_not_erase_affirmative_adjustment(rejection, rejection_first):
    clauses = [rejection, "结束时间延长1小时"]
    if not rejection_first:
        clauses.reverse()
    values, unresolved = materialize("，".join(clauses))
    assert unresolved == []
    assert values == {"end_time": "2026-10-21T13:00:00"}


@pytest.mark.parametrize("verb", ["取消", "撤销"])
def test_cancelled_delta_never_moves_start_and_keeps_independent_end(verb):
    values, unresolved = materialize(f"{verb}开始时间推迟半小时，结束时间延长1小时")
    assert unresolved == []
    assert values == {"end_time": "2026-10-21T13:00:00"}


@pytest.mark.parametrize("cancellation_first", [False, True])
def test_withdrawing_start_edit_protects_saved_start_and_derived_end(cancellation_first):
    clauses = ["开始时间改为2026年10月21日10:00", "取消开始时间修改"]
    if cancellation_first:
        clauses.reverse()
    values, unresolved = materialize("，".join(clauses), [
        slot_candidate("start_time", "2026-10-21T10:00:00", raw_value="2026年10月21日10:00"),
        slot_candidate("end_time", "2026-10-21T13:00:00", raw_value="2026年10月21日10:00"),
    ])
    assert unresolved == []
    assert values == {}


def test_withdrawing_end_edit_keeps_start_edit_without_moving_saved_end():
    values, unresolved = materialize("取消结束时间修改，开始时间改为2026年10月21日10:00")
    assert unresolved == []
    assert values == {"start_time": "2026-10-21T10:00:00"}


def test_withdrawn_end_still_bounds_an_independent_start_edit():
    values, unresolved = materialize("取消结束时间修改，开始时间改为2026年10月21日13:00")
    assert values == {}
    assert unresolved


@pytest.mark.parametrize("equipment_clause", [
    "取消摄像机开始时间修改",
    "摄像机明天凌晨3点开始自检，取消这个修改",
])
@pytest.mark.parametrize("equipment_first", [False, True])
def test_equipment_time_withdrawal_does_not_cancel_task_start(equipment_clause, equipment_first):
    clauses = [equipment_clause, "开始时间改为2026年10月21日10:00"]
    if not equipment_first:
        clauses.reverse()
    values, unresolved = materialize("，".join(clauses))
    assert unresolved == []
    assert values["start_time"] == "2026-10-21T10:00:00"


@pytest.mark.parametrize("raw,expected", [
    ("半小时后", "2026-10-20T23:30:37"),
    ("现在", "2026-10-20T23:00:37"),
    ("今天晚上11点半", "2026-10-20T23:30:00"),
])
def test_another_fields_dawn_cannot_roll_local_time_to_tomorrow(raw, expected):
    result = parse_relative_datetime_detail(raw, NOW, full_user_message=f"{raw}，摄像机明天凌晨3点自检。")
    assert result.iso_string == expected


def test_local_dawn_still_gets_existing_overnight_correction():
    assert parse_relative_datetime_detail("凌晨2点", NOW).iso_string == "2026-10-21T02:00:00"


def test_extractor_does_not_borrow_dawn_from_another_equipment_event():
    llm = SimpleNamespace(
        extract_json=lambda *a, **kw: {
            **extraction_result(slot_candidate("start_time", "半小时后")), "time_relation": None,
        },
        extract_temporal_relation=lambda *a, **kw: {"has_duration": False},
    )
    result = ParameterExtractor(llm).extract_updates(
        "半小时后，摄像机明天凌晨3点自检。", current_state={}, task_type_key="pipeline_inspection",
        required=[{"key": "start_time", "type": "datetime"}, {"key": "end_time", "type": "datetime"}],
    )
    assert result["unresolved"] == []
    assert {item["canonical_key"]: item["normalized_value"] for item in result["slot_candidates"]} == {
        "start_time": "2026-10-20T23:30:37",
    }


@pytest.fixture(params=[(False, False), (True, False), (True, True)], ids=["legacy", "patch-v2", "norm-v2"])
def manager_factory(request, monkeypatch, tmp_path):
    patch, norm = request.param
    monkeypatch.setenv("SEAGENT_RESULT_DIR", str(tmp_path))
    monkeypatch.setattr("src.dialogue_manager.is_task_patch_v2_enabled", lambda: patch)
    monkeypatch.setattr("src.dialogue_manager.is_normalization_contract_v2_enabled", lambda: norm)

    def make_manager(saved_times=True):
        llm = ScriptedLLM(default_plan=make_plan("WRITE"), default_reply="收到。")
        manager = DialogueManager(llm=llm, kb=KnowledgeBase())
        manager.slot_store.init_task_slots(manager.builder.get_schema("pipeline_inspection", "normal"))
        slots = manager.slot_store.clone_slots()
        seed = {"intent_id": "TI2026102001", "task_type_key": "pipeline_inspection", "task_type": "管缆巡检"}
        if saved_times:
            seed.update(STATE)
        for key, value in seed.items():
            slots[key].value, slots[key].status = value, "valid"
        manager.slot_store.commit_transaction(slots, [], request_id="seed-time-scope")
        manager._rebuild_cache()
        manager.phase = "collecting"
        return manager, llm

    return make_manager


@pytest.mark.parametrize("message", [
    "现在开始？", "现在开始吗？", "现在开始持续4小时？",
    "现在开始持续4小时，取消这个修改。",
])
def test_immediate_grounding_cannot_resurrect_rejected_times(manager_factory, message):
    manager, llm = manager_factory()
    before = {key: manager.slot_store.slots[key].to_dict() for key in STATE}
    llm.queue_extraction(extraction_result())
    manager.process(message)
    assert {key: manager.slot_store.slots[key].to_dict() for key in STATE} == before


@pytest.mark.parametrize("message", [
    "现在执行", "请现在开始", "开始时间定在现在", "现在开始吧", "现在开始执行任务",
])
def test_immediate_fallback_preserves_seconds_without_model_candidates(manager_factory, message):
    manager, llm = manager_factory(saved_times=False)
    llm.queue_extraction(extraction_result())
    manager.process(message)
    assert manager.task_state["start_time"] == "2026-10-20T23:00:37"
    assert manager.slot_store.slots["start_time"].status == "valid"


def test_withdrawal_preserves_saved_start_through_commit_and_snapshot(manager_factory):
    manager, llm = manager_factory()
    before = manager.slot_store.slots["start_time"].to_dict()
    llm.queue_extraction(extraction_result(
        slot_candidate("start_time", "2026-10-21T10:00:00", raw_value="2026年10月21日10:00"),
    ))
    manager.process("开始时间改为2026年10月21日10:00，取消开始时间修改，结束时间延长1小时")
    assert manager.slot_store.slots["start_time"].to_dict() == before
    assert manager.task_state["end_time"] == "2026-10-21T13:00:00"
    restored = DialogueManager(llm=llm, kb=manager.kb)
    restored.load_snapshot(manager.export_snapshot())
    assert restored.slot_store.slots["start_time"].to_dict() == before
    assert restored.task_state["end_time"] == "2026-10-21T13:00:00"
