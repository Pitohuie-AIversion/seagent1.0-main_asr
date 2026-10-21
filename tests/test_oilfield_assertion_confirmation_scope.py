"""Pending oilfield answers and named mentions must stay within their scope."""

import copy

import pytest

from src.dialogue_manager import DialogueManager
from src.knowledge_retriever import KnowledgeBase
from src.slots.slot_store import Slot
from tests.interaction_plan_support import ScriptedLLM, extraction_result, make_plan, slot_candidate


@pytest.fixture(scope="module")
def kb():
    return KnowledgeBase()


@pytest.fixture(params=[(False, False), (True, False), (True, True)], ids=["legacy", "patch", "norm"])
def dialogue(request, monkeypatch, kb):
    patch, norm = request.param
    monkeypatch.setattr("src.dialogue_manager.is_task_patch_v2_enabled", lambda: patch)
    monkeypatch.setattr("src.dialogue_manager.is_normalization_contract_v2_enabled", lambda: norm)
    llm = ScriptedLLM(default_plan=make_plan("WRITE"), default_reply="请继续提供参数。")
    manager = DialogueManager(llm, kb)
    manager.slot_store.init_task_slots(manager.builder.get_schema("tree_valve_operation", "normal"))
    for key, value in {
        "task_type_key": "tree_valve_operation", "task_type": "采油树控制面板插入",
        "water_depth": 300, "equipment_family": "通用工作级深海机器人",
        "equipment_type": "通用工作级深海机器人 250HP", "equipment_unit_id": "WROV-250-001",
    }.items():
        manager.slot_store.slots[key] = Slot(key, value=value, status="valid", source="user_input")
    manager._rebuild_cache()
    manager.phase = "collecting"
    return manager, llm


def pending(manager):
    for key, value in {
        "pending_oilfield_name": "硫化11-1油田",
        "pending_oilfield_candidates": [
            {"name": "流花11-1油田", "id": "liuhua_11_1", "confidence": 0.8},
        ],
    }.items():
        manager.slot_store.slots[key] = Slot(key, value=value, status="valid")
    manager._rebuild_cache()


@pytest.mark.parametrize("message", ["水深是320米。", "水深改为320米。"])
@pytest.mark.parametrize("action", [None, "confirm"])
def test_unrelated_scalar_assertion_cannot_confirm_pending_oilfield(dialogue, message, action):
    manager, llm = dialogue
    pending(manager)
    before = copy.deepcopy(manager.slot_store.slots["pending_oilfield_candidates"].to_dict())
    llm.queue_plan(make_plan("WRITE", pending_action=action))
    llm.queue_extraction(extraction_result(slot_candidate("water_depth", 320)))
    manager.process(message)
    assert manager.task_state["water_depth"] == 320
    assert not manager.task_state.get("oilfield_name")
    assert manager.slot_store.slots["pending_oilfield_candidates"].to_dict() == before
    assert len(llm.extract_calls) == 1


@pytest.mark.parametrize("message,action,confirmed", [
    ("确认流花11-1油田，水深改为320米。", "confirm", True),
    ("就是流花11-1，水深改为320米。", None, True),
    ("好的，水深改为320米。", "confirm", True),
    ("不是这个油田，水深改为320米。", "reject", False),
    ("不是流花11-1，水深改为320米。", None, False),
    ("流花11-1油田不对，水深改为320米。", "reject", False),
    ("不是流花11-1油田，水深改为320米。", "confirm", False),
])
def test_pending_answer_does_not_swallow_other_parameters(dialogue, message, action, confirmed):
    manager, llm = dialogue
    pending(manager)
    llm.queue_plan(make_plan("WRITE", pending_action=action))
    llm.queue_extraction(extraction_result(slot_candidate("water_depth", 320)))
    reply = manager.process(message)
    assert manager.task_state["water_depth"] == 320
    assert manager.task_state.get("oilfield_name") == ("流花11-1油田" if confirmed else None)
    assert not manager.task_state.get("pending_oilfield_name")
    assert ("已确认油田名称" if confirmed else "已取消当前待确认油田名称") in reply
    assert len(llm.extract_calls) == 1
    assert manager.phase != "done"


@pytest.mark.parametrize("message", ["确认", "就是流花11-1", "确认使用流花11-1油田"])
def test_standalone_confirmation_and_controlled_abbreviation_keep_existing_protocol(dialogue, message):
    manager, llm = dialogue
    pending(manager)
    reply = manager.process(message)
    assert manager.task_state["oilfield_name"] == "流花11-1油田"
    assert "已确认油田名称" in reply
    assert len(llm.extract_calls) == 0


def test_explicit_new_target_cannot_fall_back_to_first_pending_candidate(dialogue):
    manager, llm = dialogue
    pending(manager)
    llm.queue_plan(make_plan("WRITE", pending_action="confirm", subject_text="陵水17-2气田"))
    llm.queue_extraction(extraction_result(slot_candidate("oilfield_name", "陵水17-2气田")))
    reply = manager.process("确认陵水17-2气田。")
    assert manager.task_state["oilfield_name"] == "陵水17-2气田"
    assert not manager.task_state.get("pending_oilfield_name")
    assert "已确认油田名称为“流花11-1油田”" not in reply


def test_generic_confirmation_with_subject_outside_visible_offer_does_not_select_first(dialogue):
    manager, llm = dialogue
    pending(manager)
    llm.queue_plan(make_plan("WRITE", pending_action="confirm", subject_text="陵水17-2气田"))
    manager.process("确认")
    assert not manager.task_state.get("oilfield_name")
    assert manager.task_state.get("pending_oilfield_name")


@pytest.mark.parametrize("message", [
    "之前说陵水17-2气田，现在确认流花11-1油田。",
    "不是陵水17-2气田，现在确认流花11-1油田。",
])
def test_historical_or_rejected_candidate_is_not_the_current_confirmation_target(dialogue, message):
    manager, llm = dialogue
    pending(manager)
    manager.slot_store.slots["pending_oilfield_candidates"].value.insert(
        0, {"name": "陵水17-2气田", "id": "lingshui_17_2", "confidence": 0.8},
    )
    manager._rebuild_cache()
    llm.queue_plan(make_plan("WRITE", pending_action="confirm"))
    reply = manager.process(message)
    assert manager.task_state["oilfield_name"] == "流花11-1油田"
    assert "已确认油田名称为“流花11-1油田”" in reply
    assert len(llm.extract_calls) == 0


@pytest.mark.parametrize("message", [
    "如果需要，取消这个油田。",
    "昨天，取消这个油田。",
])
def test_conditional_or_historical_rejection_preserves_pending_candidate(dialogue, message):
    manager, llm = dialogue
    pending(manager)
    before = copy.deepcopy(manager.slot_store.slots["pending_oilfield_candidates"].to_dict())
    llm.queue_plan(make_plan("WRITE", pending_action="reject"))
    manager.process(message)
    assert manager.slot_store.slots["pending_oilfield_candidates"].to_dict() == before
    assert manager.task_state.get("pending_oilfield_name")
    assert not manager.task_state.get("oilfield_name")


@pytest.mark.parametrize("message", [
    "如果需要，确认。水深改为320米。",
    "昨天，确认。水深改为320米。",
])
def test_short_confirmation_in_conditional_or_historical_scope_is_not_current_consent(dialogue, message):
    manager, llm = dialogue
    pending(manager)
    before = copy.deepcopy(manager.slot_store.slots["pending_oilfield_candidates"].to_dict())
    llm.queue_plan(make_plan("WRITE", pending_action="confirm"))
    llm.queue_extraction(extraction_result(slot_candidate("water_depth", 320)))
    manager.process(message)
    assert manager.slot_store.slots["pending_oilfield_candidates"].to_dict() == before
    assert manager.task_state.get("pending_oilfield_name")
    assert not manager.task_state.get("oilfield_name")
    assert manager.task_state["water_depth"] == 320


@pytest.mark.parametrize("message", [
    "不要去流花11-1油田，水深改为320米。",
    "昨天在流花11-1油田，水深改为320米。",
    "如果天气允许，去流花11-1油田。水深改为320米。",
    "是否采用流花11-1油田？水深改为320米。",
])
@pytest.mark.parametrize("model_names_oilfield", [False, True])
def test_nonasserted_oilfield_mention_cannot_override_saved_location(dialogue, message, model_names_oilfield):
    manager, llm = dialogue
    for key, value in {"oilfield_name": "陵水17-2气田", "oilfield_entity_id": "lingshui_17_2"}.items():
        manager.slot_store.slots[key] = Slot(key, value=value, status="valid", source="user_input")
    manager._rebuild_cache()
    before = {key: copy.deepcopy(manager.slot_store.slots[key].to_dict())
              for key in ("oilfield_name", "oilfield_entity_id")}
    candidates = [slot_candidate("water_depth", 320)]
    if model_names_oilfield:
        candidates.append(slot_candidate("oilfield_name", "流花11-1油田"))
    llm.queue_extraction(extraction_result(*candidates))
    manager.process(message)
    assert manager.task_state["water_depth"] == 320
    assert {key: manager.slot_store.slots[key].to_dict() for key in before} == before


def test_rejected_old_choice_does_not_block_a_new_explicit_oilfield(dialogue):
    manager, llm = dialogue
    pending(manager)
    llm.queue_plan(make_plan("WRITE", pending_action="reject"))
    llm.queue_extraction(extraction_result(slot_candidate("oilfield_name", "陵水17-2气田")))
    manager.process("不是流花11-1油田，现在采用陵水17-2气田。")
    assert manager.task_state["oilfield_name"] == "陵水17-2气田"
    assert not manager.task_state.get("pending_oilfield_name")


@pytest.mark.parametrize("raw_name", ["17-2", "陵水17-2油田"])
def test_asserted_oilfield_alias_preserves_original_evidence(dialogue, raw_name):
    manager, llm = dialogue
    llm.queue_extraction(extraction_result(slot_candidate("raw_oilfield_name", raw_name)))
    manager.process(f"确认选择{raw_name}。")
    assert manager.task_state["oilfield_name"] == "陵水17-2气田"
    assert manager.task_state["oilfield_entity_id"] == "lingshui_17_2"
    assert manager.task_state["raw_oilfield_name"] == raw_name
    assert manager.slot_store.slots["raw_oilfield_name"].value == raw_name


@pytest.mark.parametrize("model_name", [None, "陵水17-2气田"])
def test_current_asserted_oilfield_repairs_missing_or_conflicting_candidate(dialogue, model_name):
    manager, llm = dialogue
    candidates = [slot_candidate("water_depth", 320)]
    if model_name:
        candidates.append(slot_candidate("raw_oilfield_name", model_name))
    llm.queue_extraction(extraction_result(*candidates))
    manager.process("之前说陵水17-2气田，现在采用流花11-1油田。水深改为320米。")
    assert manager.task_state["oilfield_name"] == "流花11-1油田"
    assert manager.task_state["oilfield_entity_id"] == "liuhua_11_1"
    assert manager.task_state["raw_oilfield_name"] == "流花11-1油田"
    assert manager.task_state["water_depth"] == 320


def test_unrelated_edit_preserves_custom_coordinates_and_metadata_for_same_oilfield(dialogue):
    manager, llm = dialogue
    for key, value in {
        "oilfield_name": "流花11-1油田", "oilfield_entity_id": "liuhua_11_1",
        "oilfield_coordinates": {"lat": 20.812, "lon": 115.732},
    }.items():
        manager.slot_store.slots[key] = Slot(key, value=value, status="valid", source="user_input")
    manager._rebuild_cache()
    before = copy.deepcopy(manager.slot_store.slots["oilfield_coordinates"].to_dict())
    llm.queue_extraction(extraction_result(slot_candidate("water_depth", 320)))
    manager.process("水深改为320米。")
    assert manager.task_state["water_depth"] == 320
    assert manager.slot_store.slots["oilfield_coordinates"].to_dict() == before


def test_explicit_entity_switch_keeps_existing_default_coordinate_policy(dialogue):
    manager, llm = dialogue
    for key, value in {
        "oilfield_name": "陵水17-2气田", "oilfield_entity_id": "lingshui_17_2",
        "oilfield_coordinates": {"lat": 17.48, "lon": 110.05},
    }.items():
        manager.slot_store.slots[key] = Slot(key, value=value, status="valid", source="user_input")
    manager._rebuild_cache()
    llm.queue_extraction(extraction_result(slot_candidate("oilfield_name", "流花11-1油田")))
    manager.process("油田改为流花11-1油田。")
    assert manager.task_state["oilfield_entity_id"] == "liuhua_11_1"
    assert manager.task_state["oilfield_coordinates"] == {"lat": 20.815, "lon": 115.735}
