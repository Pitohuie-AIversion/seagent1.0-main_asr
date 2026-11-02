"""Family/model aliases need the same current-turn evidence as fleet units."""
import copy

import pytest

from src.dialogue_manager import DialogueManager
from src.handlers.equipment_assertion_grounding import EQUIPMENT_KEYS
from src.knowledge_retriever import KnowledgeBase
from tests.interaction_plan_support import ScriptedLLM, extraction_result, make_plan, slot_candidate


OLD_FAMILY = "观察级深海机器人"
OLD_TYPE = "观察级深海机器人 75HP"
NEW_FAMILY = "轻型工作级深海机器人"
NEW_TYPE = "轻型工作级深海机器人 150HP"


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
    unit = manager.kb.resolve_robot_unit("OBSROV-75-001", "pipeline_inspection")
    seed = {
        "task_type_key": "pipeline_inspection", "task_type": "管缆巡检",
        "intent_id": "TI2026102001", "water_depth": 300,
        "start_point": {"lat": 20.0, "lon": 115.0},
        "end_point": {"lat": 20.3, "lon": 115.5},
        "equipment_class": unit["robot"]["robot_class"],
        "equipment_family": OLD_FAMILY, "equipment_type": OLD_TYPE,
        "equipment_unit_id": unit["unit_id"], "equipment_name": unit["display_name"],
    }
    slots = manager.slot_store.clone_slots()
    for key, value in seed.items():
        slots[key].value = copy.deepcopy(value)
        slots[key].status = "valid"
        slots[key].raw_value = f"已确认的{key}"
        slots[key].confidence = 0.98
        slots[key].source = "user_input"
    manager.slot_store.commit_transaction(slots, [], request_id="seed-family-evidence")
    manager._rebuild_cache()
    manager.phase = "collecting"
    return manager, llm


def equipment_slots(manager):
    return {key: manager.slot_store.slots[key].to_dict()
            for key in EQUIPMENT_KEYS if key in manager.slot_store.slots}


@pytest.mark.parametrize("message", [
    "不要改用天鹰座。",
    "天鹰座150HP可以吗？",
    "如果天气允许，使用天鹰座。",
    "昨天，使用天鹰座150HP。",
    "使用天鹰座，取消这个修改。",
    "保持天鹰座。",
    "天鹰座或者观察级。",
    "使用天鹰座150HP还是观察级75HP？",
])
@pytest.mark.parametrize("with_model_candidates", [False, True])
def test_nonasserted_aliases_preserve_full_device_metadata_and_snapshot(
    dialogue, message, with_model_candidates,
):
    manager, llm = dialogue
    before = equipment_slots(manager)
    candidates = [
        slot_candidate("equipment_family", NEW_FAMILY),
        slot_candidate("equipment_type", NEW_TYPE),
        slot_candidate("equipment_unit_id", "LROV-150-001"),
        slot_candidate("rov_description", NEW_TYPE),
    ] if with_model_candidates else []
    llm.queue_extraction(extraction_result(*candidates))

    manager.process("水深改为320米，" + message)

    assert manager.task_state["water_depth"] == 320
    assert equipment_slots(manager) == before
    restored = DialogueManager(ScriptedLLM(), manager.kb)
    restored.load_snapshot(manager.export_snapshot())
    assert equipment_slots(restored) == before


@pytest.mark.parametrize("selector,key,expected", [
    ("天鹰座", "equipment_family", NEW_FAMILY),
    ("天鹰座150HP", "equipment_type", NEW_TYPE),
])
@pytest.mark.parametrize("proposal", ["empty", "stale", "correct"])
def test_current_catalog_choice_overrides_previous_recommendation(dialogue, selector, key, expected, proposal):
    manager, llm = dialogue
    offered_key = "device_class" if key == "equipment_family" else "device"
    old_value = OLD_FAMILY if key == "equipment_family" else OLD_TYPE
    manager.conversation_history = [
        {"role": "assistant", "content": f"我明确推荐【{old_value}】。"},
    ]
    llm.queue_plan(make_plan("WRITE", relation="recommend", subject_type=offered_key, subject_text=old_value))
    candidates = [] if proposal == "empty" else [
        slot_candidate(key, old_value if proposal == "stale" else expected),
    ]
    llm.queue_extraction(extraction_result(*candidates))

    manager.process(f"改用{selector}，水深改为320米。")

    assert manager.task_state["equipment_family"] == NEW_FAMILY
    assert manager.task_state["equipment_type"] == NEW_TYPE
    assert manager.slot_store.slots["equipment_family"].status == "valid"
    assert manager.slot_store.slots["equipment_type"].status == "valid"
    assert not manager.task_state.get("equipment_unit_id")
    assert manager.task_state["water_depth"] == 320
    restored = DialogueManager(ScriptedLLM(), manager.kb)
    restored.load_snapshot(manager.export_snapshot())
    assert restored.task_state["equipment_family"] == NEW_FAMILY
    assert restored.task_state["equipment_type"] == NEW_TYPE
    assert not restored.task_state.get("equipment_unit_id")


@pytest.mark.parametrize("assignment", [
    "机器人型号为天鹰座150HP",
    "设备型号：轻型工作级深海机器人 150HP",
])
@pytest.mark.parametrize("proposal", ["empty", "stale", "correct"])
@pytest.mark.parametrize("recommendation", [False, True])
def test_explicit_model_assignment_overrides_previous_recommendation(
    dialogue, assignment, proposal, recommendation,
):
    manager, llm = dialogue
    manager.conversation_history = [
        {"role": "assistant", "content": f"我明确推荐【{OLD_TYPE}】。"},
    ]
    llm.queue_plan(make_plan(
        "WRITE", relation="recommend" if recommendation else "unknown",
        subject_type="device", subject_text=OLD_TYPE,
    ))
    candidates = [] if proposal == "empty" else [
        slot_candidate("equipment_type", OLD_TYPE if proposal == "stale" else NEW_TYPE),
    ]
    llm.queue_extraction(extraction_result(*candidates))

    manager.process(assignment + "，水深改为320米。")

    assert manager.task_state["equipment_family"] == NEW_FAMILY
    assert manager.task_state["equipment_type"] == NEW_TYPE
    assert not manager.task_state.get("equipment_unit_id")
    assert manager.task_state["water_depth"] == 320
    restored = DialogueManager(ScriptedLLM(), manager.kb)
    restored.load_snapshot(manager.export_snapshot())
    assert restored.task_state["equipment_type"] == NEW_TYPE
    assert not restored.task_state.get("equipment_unit_id")


@pytest.mark.parametrize("message", [
    "不要设定机器人型号为天鹰座150HP。",
    "不要设置设备型号：轻型工作级深海机器人 150HP。",
    "机器人型号为天鹰座150HP？",
    "设备型号：轻型工作级深海机器人 150HP？",
])
def test_nonasserted_model_assignments_keep_metadata_and_snapshot(dialogue, message):
    manager, llm = dialogue
    before = equipment_slots(manager)
    llm.queue_extraction(extraction_result(
        slot_candidate("equipment_family", NEW_FAMILY),
        slot_candidate("equipment_type", NEW_TYPE),
        slot_candidate("equipment_unit_id", "LROV-150-001"),
    ))

    manager.process("水深改为320米，" + message)

    assert equipment_slots(manager) == before
    assert manager.task_state["water_depth"] == 320
    restored = DialogueManager(ScriptedLLM(), manager.kb)
    restored.load_snapshot(manager.export_snapshot())
    assert equipment_slots(restored) == before


@pytest.mark.parametrize("message", ["确认", "那就按你刚才推荐的选"])
def test_accepting_visible_family_recommendation_still_works(dialogue, message):
    manager, llm = dialogue
    manager.conversation_history = [
        {"role": "assistant", "content": f"我明确推荐机器人系列【{NEW_FAMILY}】。"},
    ]
    llm.queue_plan(make_plan(
        "WRITE", relation="recommend", subject_type="device_class", subject_text=NEW_FAMILY,
    ))
    llm.queue_extraction(extraction_result())

    manager.process(message)

    assert manager.task_state["equipment_family"] == NEW_FAMILY
    assert manager.task_state["equipment_type"] == NEW_TYPE
    assert not manager.task_state.get("equipment_unit_id")


def test_positive_model_after_negative_unit_derives_only_unique_ancestors(dialogue):
    manager, llm = dialogue
    llm.queue_extraction(extraction_result(
        slot_candidate("equipment_family", OLD_FAMILY),
        slot_candidate("equipment_type", OLD_TYPE),
        slot_candidate("equipment_unit_id", "OBSROV-75-001"),
    ))

    manager.process("不要使用OBSROV-75-001，改用天鹰座150HP，水深改为320米。")

    assert manager.task_state["equipment_family"] == NEW_FAMILY
    assert manager.task_state["equipment_type"] == NEW_TYPE
    assert not manager.task_state.get("equipment_unit_id")
    assert manager.task_state["water_depth"] == 320


def test_incompatible_model_alias_cannot_change_to_an_allowed_model(dialogue):
    manager, llm = dialogue
    old_unit = manager.task_state["equipment_unit_id"]
    llm.queue_extraction(extraction_result(
        slot_candidate("equipment_family", NEW_FAMILY),
        slot_candidate("equipment_type", NEW_TYPE),
    ))

    manager.process("改用奇点250HP，水深改为320米。")

    family = manager.slot_store.slots["equipment_family"]
    assert family.value == OLD_FAMILY
    assert family.status in {"invalid", "conflict"}
    assert family.candidate_value == "通用工作级深海机器人"
    assert manager.slot_store.slots["equipment_type"].value == OLD_TYPE
    assert manager.task_state["equipment_unit_id"] == old_unit
    assert manager.task_state["water_depth"] == 320
