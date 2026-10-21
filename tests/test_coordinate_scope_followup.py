"""Coordinate scope must preserve independent writes and retained slot evidence."""
import copy

import pytest

from src.dialogue_manager import DialogueManager
from src.extraction.coord_parser import (
    coordinate_update_issues,
    parse_coordinate_updates,
    protected_coordinate_fields,
)
from src.knowledge_retriever import KnowledgeBase
from tests.interaction_plan_support import ScriptedLLM, extraction_result, make_plan, slot_candidate


FIELDS = {"start_point", "end_point"}
START = {"lat": 20.1, "lon": 115.2}
END = {"lat": 20.4, "lon": 115.6}


@pytest.mark.parametrize("message,expected,protected", [
    ("起点改为北纬20.1度东经115.2度，如果天气允许，支持船用海洋石油286。",
     {"start_point": START}, set()),
    ("起点改为北纬20.1度东经115.2度，如果天气允许，终点改为北纬20.4度东经115.6度。",
     {"start_point": START}, {"end_point"}),
    ("如果天气允许，起点改为北纬20.1度东经115.2度。",
     {}, {"start_point"}),
    ("起点改为北纬20.1度东经115.2度，如果天气允许。",
     {}, {"start_point"}),
    ("起点改为北纬20.1度东经115.2度，如果天气允许，起点改为北纬20.4度东经115.6度。",
     {}, {"start_point"}),
    ("起点坐标不变，终点改为北纬20.4度东经115.6度。",
     {"end_point": END}, {"start_point"}),
    ("起始坐标保持不变，终点改为北纬20.4度东经115.6度。",
     {"end_point": END}, {"start_point"}),
    ("取消起点坐标修改，终点改为北纬20.4度东经115.6度。",
     {"end_point": END}, {"start_point"}),
])
def test_coordinate_condition_and_pause_scope(message, expected, protected):
    assert parse_coordinate_updates(message, FIELDS) == expected
    assert protected_coordinate_fields(message, FIELDS) == protected
    assert coordinate_update_issues(message, FIELDS) == []


@pytest.mark.parametrize("message", [
    "保持坐标，水深改为320米。",
    "保留当前坐标，水深改为320米。",
    "取消坐标修改，水深改为320米。",
    "请撤销当前经纬度调整，水深改为320米。",
    "坐标不变，起点改为北纬20.1度东经115.2度。",
])
def test_generic_coordinate_pause_protects_all_coordinate_candidates(message):
    assert parse_coordinate_updates(message, FIELDS) == {}
    assert protected_coordinate_fields(message, FIELDS) == FIELDS
    assert parse_coordinate_updates(
        message, FIELDS, proposed_updates={"start_point": START, "end_point": END},
    ) == {}
    assert coordinate_update_issues(message, FIELDS) == []


@pytest.fixture(params=[(False, False), (True, False), (True, True)],
                ids=["legacy", "patch-v2", "norm-v2"])
def coordinate_dialogue(request, monkeypatch, tmp_path):
    patch, norm = request.param
    monkeypatch.setenv("SEAGENT_RESULT_DIR", str(tmp_path))
    monkeypatch.setattr("src.dialogue_manager.is_task_patch_v2_enabled", lambda: patch)
    monkeypatch.setattr("src.dialogue_manager.is_normalization_contract_v2_enabled", lambda: norm)
    llm = ScriptedLLM(default_plan=make_plan("WRITE"), default_reply="收到。")
    manager = DialogueManager(llm, KnowledgeBase())
    manager.slot_store.init_task_slots(manager.builder.get_schema("pipeline_inspection", "normal"))
    slots = manager.slot_store.clone_slots()
    values = {
        "task_type_key": "pipeline_inspection",
        "task_type": "管缆巡检",
        "intent_id": "TI2026102001",
        "start_point": {"lat": 20.0, "lon": 115.0},
        "end_point": {"lat": 20.3, "lon": 115.5},
        "water_depth": 300,
        "support_vessel": "海洋石油681",
    }
    for key, value in values.items():
        slots[key].value = copy.deepcopy(value)
        slots[key].status = "valid"
        slots[key].raw_value = f"先前确认的{key}"
        slots[key].confidence = 0.98
        slots[key].source = "user_input"
    manager.slot_store.commit_transaction(slots, [], request_id="seed-coordinate-scope")
    manager._rebuild_cache()
    manager.phase = "collecting"
    return manager, llm


@pytest.mark.parametrize("with_model_candidate", [False, True])
def test_independent_coordinate_before_ship_condition_commits(coordinate_dialogue, with_model_candidate):
    manager, llm = coordinate_dialogue
    before = {key: manager.slot_store.slots[key].to_dict()
              for key in ("end_point", "support_vessel")}
    candidates = [slot_candidate("start_point", {"lat": 22.0, "lon": 117.0})] if with_model_candidate else []
    llm.queue_extraction(extraction_result(*candidates))

    manager.process("起点改为北纬20.1度东经115.2度，如果天气允许，支持船用海洋石油286。")

    assert manager.task_state["start_point"] == START
    assert manager.slot_store.slots["start_point"].status == "valid"
    assert {key: manager.slot_store.slots[key].to_dict() for key in before} == before


@pytest.mark.parametrize("with_model_candidate", [False, True])
def test_start_point_pause_preserves_metadata_while_end_changes(coordinate_dialogue, with_model_candidate):
    manager, llm = coordinate_dialogue
    before_start = manager.slot_store.slots["start_point"].to_dict()
    candidates = [
        slot_candidate("start_point", {"lat": 22.0, "lon": 117.0}),
        slot_candidate("end_point", {"lat": 23.0, "lon": 118.0}),
    ] if with_model_candidate else []
    llm.queue_extraction(extraction_result(*candidates))

    manager.process("起点坐标不变，终点改为北纬20.4度东经115.6度。")

    assert manager.task_state["end_point"] == END
    assert manager.slot_store.slots["start_point"].to_dict() == before_start


@pytest.mark.parametrize("message", [
    "保持坐标，水深改为320米。",
    "取消坐标修改，水深改为320米。",
])
@pytest.mark.parametrize("with_model_candidate", [False, True])
def test_generic_pause_preserves_both_coordinates_and_metadata(
    coordinate_dialogue, message, with_model_candidate,
):
    manager, llm = coordinate_dialogue
    before = {key: manager.slot_store.slots[key].to_dict() for key in FIELDS}
    candidates = [
        slot_candidate("start_point", START), slot_candidate("end_point", END),
    ] if with_model_candidate else []
    llm.queue_extraction(extraction_result(*candidates))

    manager.process(message)

    assert manager.task_state["water_depth"] == 320
    assert {key: manager.slot_store.slots[key].to_dict() for key in FIELDS} == before
