"""Regressions from real user journeys: rejected values and visual payloads."""

import pytest

from src.dialogue_manager import DialogueManager
from src.knowledge_retriever import KnowledgeBase
from src.slot_store import Slot, SlotStore
from tests.interaction_plan_support import (
    ScriptedLLM,
    extraction_result,
    make_plan,
    slot_candidate,
)


@pytest.fixture(params=[(False, False), (True, False), (True, True)])
def depth_dialogue(request, monkeypatch):
    patch_v2, norm_v2 = request.param
    monkeypatch.setattr("src.dialogue_manager.is_task_patch_v2_enabled", lambda: patch_v2)
    monkeypatch.setattr("src.dialogue_manager.is_normalization_contract_v2_enabled", lambda: norm_v2)
    llm = ScriptedLLM(default_plan=make_plan("WRITE"), default_reply="收到。")
    dm = DialogueManager(llm=llm, kb=KnowledgeBase())
    dm.slot_store.init_task_slots(dm.builder.get_schema("pipeline_burial", "normal"))
    slots = dm.slot_store.clone_slots()
    for key, value in {"task_type_key": "pipeline_burial", "task_type": "管缆埋设", "water_depth": 300}.items():
        slots[key].value = value
        slots[key].status = "valid"
    dm.slot_store.commit_transaction(slots, [], request_id="seed")
    dm._rebuild_cache()
    dm.phase = "collecting"
    return dm, llm


def reject_negative_depth(dm, llm):
    llm.queue_extraction(extraction_result(slot_candidate("water_depth", -10)))
    dm.process("水深改成负10米。")
    depth = dm.slot_store.slots["water_depth"]
    assert depth.status == "conflict"
    assert depth.value == 300
    assert depth.candidate_value == -10
    assert depth.validation_error


def test_explicit_correction_replaces_rejected_candidate(depth_dialogue):
    dm, llm = depth_dialogue
    reject_negative_depth(dm, llm)

    llm.queue_extraction(extraction_result(slot_candidate("water_depth", 350)))
    dm.process("刚才说错了，水深改为350米。")

    depth = dm.slot_store.slots["water_depth"]
    assert depth.status == "valid"
    assert depth.value == 350
    assert depth.candidate_value is None
    assert depth.validation_error is None
    assert dm.slot_store.get_task_state()["water_depth"] == 350


@pytest.mark.parametrize("repeat_value", [False, True])
def test_confirmation_cannot_accept_rejected_negative_depth(depth_dialogue, repeat_value):
    dm, llm = depth_dialogue
    reject_negative_depth(dm, llm)
    llm.queue_extraction(extraction_result(*([slot_candidate("water_depth", -10)] if repeat_value else [])))

    dm.process("确认水深改为负10米。" if repeat_value else "确认水深修改。")

    depth = dm.slot_store.slots["water_depth"]
    assert depth.status == "conflict"
    assert depth.value == 300
    assert depth.candidate_value == -10
    assert depth.validation_error
    assert "water_depth" not in dm.slot_store.get_task_state()


@pytest.mark.parametrize("equipment,visual", [
    (equipment, visual)
    for equipment in ("观察级深海机器人 75HP", "轻型工作级深海机器人 150HP")
    for visual in ("浑水水下成像系统", "双目水下成像系统")
] + [("观察级深海机器人 75HP", "高清水下摄像机")])
@pytest.mark.parametrize("operation", ["add", "set", "replace"])
def test_distinct_supported_visual_payload_is_not_onboard_alias(equipment, visual, operation):
    kb = KnowledgeBase()
    store = SlotStore(kb)
    slots = {
        "task_type_key": Slot("task_type_key", value="pipeline_inspection", status="valid"),
        "equipment_type": Slot("equipment_type", value=equipment, status="valid"),
        "payload": Slot("payload", value=["激光标尺"], value_type="list", status="valid"),
    }
    result = store.apply_list_mutation(
        slots,
        {"field": "payload", "operation": operation, "items": [visual],
         "target_items": ["激光标尺"] if operation == "replace" else [],
         "raw_text": f"载荷选择{visual}"},
        required_schema=[{"key": "payload", "allowed_values_ref": "payload_options.pipeline_inspection"}],
    )

    assert result["success"] is True
    assert slots["payload"].value == (["激光标尺", visual] if operation == "add" else [visual])


@pytest.mark.parametrize("visual", ["浑水水下成像系统", "双目水下成像系统", "高清水下摄像机"])
def test_visual_replacement_survives_dialogue_validation(depth_dialogue, visual):
    dm, llm = depth_dialogue
    slots = dm.slot_store.clone_slots()
    for key, value in {
        "task_type_key": "pipeline_inspection",
        "task_type": "管缆巡检",
        "equipment_type": "观察级深海机器人 75HP",
        "payload": ["激光标尺"],
    }.items():
        slots[key].value = value
        slots[key].status = "valid"
    dm.slot_store.commit_transaction(slots, [], request_id="seed_visual_payload")
    dm._rebuild_cache()
    llm.queue_extraction(extraction_result(slot_candidate("payload", [visual])))

    dm.process(f"载荷改成{visual}。")

    assert dm.slot_store.slots["payload"].status == "valid"
    assert dm.slot_store.get_task_state()["payload"] == [visual]
