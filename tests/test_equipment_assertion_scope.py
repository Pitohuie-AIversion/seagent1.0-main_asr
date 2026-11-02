"""Rejected or ambiguous robot mentions cannot replace confirmed fleet evidence."""
import copy

import pytest

from src.dialogue_manager import DialogueManager
from src.handlers.equipment_assertion_grounding import EQUIPMENT_KEYS
from src.handlers.explicit_value_grounding import ground_explicit_values
from src.knowledge_retriever import KnowledgeBase
from tests.interaction_plan_support import ScriptedLLM, extraction_result, make_plan, slot_candidate


OLD = "LROV-150-001"
NEW = "LROV-150-002"
NON_WRITES = [
    f"机器人用{NEW}？",
    f"是否用{NEW}？",
    f"{NEW}？",
    f"机器人用{NEW}，可以吗？",
    f"如果天气允许，机器人用{NEW}。",
    f"机器人用{NEW}，如果天气允许。",
    f"昨天使用{NEW}。",
    f"昨天，机器人改用{NEW}。",
    f"机器人不要改用{NEW}。",
    f"机器人用{NEW}，取消机器人修改。",
    f"机器人用{NEW}，取消这个修改。",
    f"机器人用{NEW}，保留当前机器人。",
    "保持机器人。",
    "撤销设备修改。",
    f"机器人用{NEW}或者{OLD}。",
    f"机器人用{NEW}，机器人用{OLD}。",
    f"使用{NEW}和{OLD}。",
    f"机器人用{NEW} {OLD}。",
]


@pytest.fixture(scope="module")
def kb():
    return KnowledgeBase()


def grounded(kb, message, candidates=()):
    return ground_explicit_values(
        extraction_result(*candidates), message, kb=kb,
        fields=[{"key": "water_depth", "type": "number"}],
        current_state={"equipment_unit_id": OLD}, task_type="pipeline_inspection",
    )


@pytest.mark.parametrize("message", NON_WRITES)
@pytest.mark.parametrize("model_candidate", [False, True])
def test_device_mentions_filter_model_candidates_and_do_not_ground(kb, message, model_candidate):
    candidates = [slot_candidate(key, NEW) for key in EQUIPMENT_KEYS] if model_candidate else []
    result = grounded(kb, "水深改为320米，" + message, candidates)
    assert {c["canonical_key"]: c["normalized_value"] for c in result["slot_candidates"]} == {
        "water_depth": 320,
    }


@pytest.mark.parametrize("message", [
    f"不要用{OLD}，改用{NEW}。",
    f"昨天，机器人用{OLD}，现在改用{NEW}。",
    f"机器人用{OLD}？机器人改用{NEW}。",
    f"机器人用{NEW}，机器人用{NEW}。",
    f"机器人用{NEW}，水深320米可以吗？",
    f"机器人用{NEW}，是否水深320米可以？",
    f"机器人用{NEW}，水深320米，取消这个修改。",
    f"机器人用{NEW}，水深320米，如果天气允许。",
    f"机器人用{NEW}，取消机器人载荷修改。",
    f"机器人用{NEW}，如果天气允许，支持船用海洋石油286。",
    f"支持船不变，机器人用{NEW}。",
    NEW,
])
def test_independent_positive_device_choice_remains_grounded(kb, message):
    result = grounded(kb, message, [slot_candidate("equipment_unit_id", OLD)])
    values = {c["canonical_key"]: c["normalized_value"] for c in result["slot_candidates"]}
    assert values["equipment_unit_id"] == NEW


def test_positive_variant_cannot_reauthorize_negated_unit(kb):
    result = grounded(
        kb, f"不要使用{NEW}，机器人使用观察级深海机器人75HP。",
        [slot_candidate("equipment_unit_id", NEW),
         slot_candidate("equipment_type", "观察级深海机器人 75HP")],
    )
    assert {c["canonical_key"]: c["normalized_value"] for c in result["slot_candidates"]} == {
        "equipment_family": "观察级深海机器人",
        "equipment_type": "观察级深海机器人 75HP",
    }


@pytest.fixture(params=[(False, False), (True, False), (True, True)],
                ids=["legacy", "patch-v2", "norm-v2"])
def dialogue(request, monkeypatch, tmp_path):
    patch, norm = request.param
    monkeypatch.setenv("SEAGENT_RESULT_DIR", str(tmp_path))
    monkeypatch.setattr("src.dialogue_manager.is_task_patch_v2_enabled", lambda: patch)
    monkeypatch.setattr("src.dialogue_manager.is_normalization_contract_v2_enabled", lambda: norm)
    llm = ScriptedLLM(default_plan=make_plan("WRITE"), default_reply="收到。")
    manager = DialogueManager(llm, KnowledgeBase())
    manager.slot_store.init_task_slots(manager.builder.get_schema("pipeline_inspection", "normal"))
    unit = manager.kb.resolve_robot_unit(OLD, "pipeline_inspection")
    seed = {
        "task_type_key": "pipeline_inspection", "task_type": "管缆巡检",
        "intent_id": "TI2026102001", "water_depth": 300,
        "start_point": {"lat": 20.0, "lon": 115.0},
        "end_point": {"lat": 20.3, "lon": 115.5},
        "equipment_class": unit["robot"]["robot_class"],
        "equipment_family": unit["robot"]["family_full_name"],
        "equipment_type": unit["robot"]["full_name"],
        "equipment_unit_id": unit["unit_id"], "equipment_name": unit["display_name"],
    }
    slots = manager.slot_store.clone_slots()
    for key, value in seed.items():
        slots[key].value = copy.deepcopy(value)
        slots[key].status = "valid"
        slots[key].raw_value = f"先前确认的{key}"
        slots[key].confidence = 0.98
        slots[key].source = "user_input"
    manager.slot_store.commit_transaction(slots, [], request_id="seed-equipment-scope")
    manager._rebuild_cache()
    manager.phase = "collecting"
    return manager, llm


@pytest.mark.parametrize("message", [
    f"机器人用{NEW}？",
    f"如果天气允许，机器人用{NEW}。",
    f"昨天，机器人改用{NEW}。",
    f"机器人不要改用{NEW}。",
    f"机器人用{NEW}，取消机器人修改。",
    f"机器人用{NEW}或者{OLD}。",
])
@pytest.mark.parametrize("model_candidate", [False, True])
def test_rejected_device_preserves_slots_and_snapshot_with_independent_write(
    dialogue, message, model_candidate,
):
    manager, llm = dialogue
    before = {
        key: manager.slot_store.slots[key].to_dict()
        for key in EQUIPMENT_KEYS if key in manager.slot_store.slots
    }
    candidates = [slot_candidate("equipment_unit_id", NEW)] if model_candidate else []
    llm.queue_extraction(extraction_result(*candidates))

    manager.process("水深改为320米，" + message)

    assert manager.task_state["water_depth"] == 320
    assert manager.task_state["equipment_unit_id"] == OLD
    assert {key: manager.slot_store.slots[key].to_dict() for key in before} == before
    restored = DialogueManager(ScriptedLLM(), manager.kb)
    restored.load_snapshot(manager.export_snapshot())
    assert {key: restored.slot_store.slots[key].to_dict() for key in before} == before
    assert restored.task_state["water_depth"] == 320


@pytest.mark.parametrize("model_candidate", [False, True])
def test_negative_old_choice_allows_explicit_new_device(dialogue, model_candidate):
    manager, llm = dialogue
    candidates = [slot_candidate("equipment_unit_id", OLD)] if model_candidate else []
    llm.queue_extraction(extraction_result(*candidates))

    manager.process(f"不要用{OLD}，改用{NEW}，水深改为320米。")

    assert manager.task_state["equipment_unit_id"] == NEW
    assert manager.slot_store.slots["equipment_unit_id"].status == "valid"
    assert manager.task_state["water_depth"] == 320
