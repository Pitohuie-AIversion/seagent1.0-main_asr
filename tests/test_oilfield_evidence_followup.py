"""Oilfield writes require a current assertion or a supported pending answer."""

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


def save_oilfield(manager):
    for key, value in {
        "oilfield_name": "陵水17-2气田",
        "raw_oilfield_name": "陵水17-2油田",
        "oilfield_entity_id": "lingshui_17_2",
        "oilfield_match_status": "accepted",
        "oilfield_coordinates": {"lat": 17.48, "lon": 110.05},
    }.items():
        manager.slot_store.slots[key] = Slot(key, value=value, status="valid", source="user_input")
    manager._rebuild_cache()


def set_pending(manager):
    for key, value in {
        "pending_oilfield_name": "硫化11-1油田",
        "pending_oilfield_candidates": [
            {"name": "流花11-1油田", "id": "liuhua_11_1", "confidence": 0.8},
        ],
    }.items():
        manager.slot_store.slots[key] = Slot(key, value=value, status="valid")
    manager._rebuild_cache()


def oilfield_snapshot(manager):
    return {
        key: copy.deepcopy(slot.to_dict()) for key, slot in manager.slot_store.slots.items()
        if "oilfield" in key
    }


@pytest.mark.parametrize("model_key", [None, "oilfield_name", "raw_oilfield_name"])
@pytest.mark.parametrize("existing", ["saved", "pending"])
def test_unrelated_parameter_cannot_rewrite_saved_oilfield(dialogue, model_key, existing):
    manager, llm = dialogue
    (save_oilfield if existing == "saved" else set_pending)(manager)
    before = oilfield_snapshot(manager)
    candidates = [slot_candidate("water_depth", 320)]
    if model_key:
        candidates.append(slot_candidate(model_key, "流花11-1油田"))
    llm.queue_extraction(extraction_result(*candidates))
    manager.process("水深改为320米。")
    assert manager.task_state["water_depth"] == 320
    assert oilfield_snapshot(manager) == before
    assert len(llm.extract_calls) == 1


@pytest.mark.parametrize("message", [
    "确认流花11-1油田，可以吗？水深改为320米。",
    "取消这个油田，可以吗？水深改为320米。",
])
@pytest.mark.parametrize("model_name", [None, "流花11-1油田"])
def test_trailing_question_cannot_confirm_or_reject_pending(dialogue, message, model_name):
    manager, llm = dialogue
    set_pending(manager)
    before = oilfield_snapshot(manager)
    llm.queue_plan(make_plan("WRITE", pending_action="confirm"))
    candidates = [slot_candidate("water_depth", 320)]
    if model_name:
        candidates.append(slot_candidate("oilfield_name", model_name))
    llm.queue_extraction(extraction_result(*candidates))
    reply = manager.process(message)
    assert manager.task_state["water_depth"] == 320
    assert oilfield_snapshot(manager) == before
    assert "已确认油田名称" not in reply
    assert "已取消当前待确认油田名称" not in reply


def test_trailing_question_cannot_replace_saved_oilfield(dialogue):
    manager, llm = dialogue
    save_oilfield(manager)
    before = oilfield_snapshot(manager)
    llm.queue_extraction(extraction_result(
        slot_candidate("oilfield_name", "流花11-1油田"), slot_candidate("water_depth", 320),
    ))
    manager.process("油田改为流花11-1油田，可以吗？水深改为320米。")
    assert manager.task_state["water_depth"] == 320
    assert oilfield_snapshot(manager) == before


@pytest.mark.parametrize("message", [
    "不是流花11-1油田，现在确认流花11-1油田。水深改为320米。",
    "先不要这个油田，现在确认采用流花11-1油田。水深改为320米。",
])
def test_final_explicit_confirmation_after_rejection_is_applied(dialogue, message):
    manager, llm = dialogue
    set_pending(manager)
    llm.queue_plan(make_plan("WRITE", pending_action="confirm"))
    llm.queue_extraction(extraction_result(slot_candidate("water_depth", 320)))
    reply = manager.process(message)
    assert manager.task_state["oilfield_name"] == "流花11-1油田"
    assert not manager.task_state.get("pending_oilfield_name")
    assert manager.task_state["water_depth"] == 320
    assert "已确认油田名称" in reply
    assert "已取消当前待确认油田名称" not in reply


def test_question_about_other_field_does_not_cancel_explicit_confirmation(dialogue):
    manager, llm = dialogue
    set_pending(manager)
    llm.queue_plan(make_plan("WRITE", pending_action="confirm"))
    reply = manager.process("确认流花11-1油田，水深多少米？")
    assert manager.task_state["oilfield_name"] == "流花11-1油田"
    assert not manager.task_state.get("pending_oilfield_name")
    assert "已确认油田名称" in reply


@pytest.mark.parametrize("raw_name,standard_name", [
    ("17-2", "陵水17-2气田"),
    ("陵水17-2油田", "陵水17-2气田"),
    ("自设A区", "自设A区"),
])
def test_current_alias_or_custom_name_preserves_raw_evidence(dialogue, raw_name, standard_name):
    manager, llm = dialogue
    llm.queue_extraction(extraction_result(slot_candidate("oilfield_name", raw_name)))
    manager.process(raw_name)
    assert manager.task_state["oilfield_name"] == standard_name
    assert manager.task_state["raw_oilfield_name"] == raw_name


def test_new_coordinates_can_link_location_after_unfounded_name_is_removed(dialogue):
    manager, llm = dialogue
    save_oilfield(manager)
    llm.queue_extraction(extraction_result(
        slot_candidate("oilfield_coordinates", {"lat": 20.812, "lon": 115.732}),
        slot_candidate("oilfield_name", "陵水17-2气田"),
    ))
    manager.process("坐标改为北纬20.812，东经115.732。")
    assert manager.task_state["oilfield_name"] == "流花11-1油田"
    assert manager.task_state["oilfield_entity_id"] == "liuhua_11_1"
    assert manager.task_state["oilfield_coordinates"] == {"lat": 20.812, "lon": 115.732}


@pytest.mark.parametrize("offer,message,model_name,accepted", [
    ("visible", "2", "陵水17-2气田", True),
    ("visible", "选2", "陵水17-2油田", True),
    ("visible", "2", "流花11-1油田", False),
    ("missing", "2", "陵水17-2气田", False),
    ("stale", "2", "陵水17-2气田", False),
    ("visible", "选2，可以吗？", "陵水17-2气田", False),
    ("visible", "不要选2", "陵水17-2气田", False),
])
def test_numbered_oilfield_selection_requires_current_visible_matching_offer(
    dialogue, offer, message, model_name, accepted,
):
    manager, llm = dialogue
    set_pending(manager)
    before = oilfield_snapshot(manager)
    visible = "请选择油田：\n1. 流花11-1油田\n2. 陵水17-2气田"
    manager.conversation_history = [
        {"role": "assistant", "content": visible if offer != "missing" else "请提供油田名称。"},
    ]
    if offer == "stale":
        manager.conversation_history.append({"role": "assistant", "content": "请提供水深。"})
    llm.queue_extraction(extraction_result(slot_candidate("oilfield_name", model_name)))
    manager.process(message)
    if accepted:
        assert manager.task_state["oilfield_name"] == "陵水17-2气田"
        assert manager.task_state["oilfield_entity_id"] == "lingshui_17_2"
        assert manager.task_state["raw_oilfield_name"] == model_name
        assert not manager.task_state.get("pending_oilfield_name")
    else:
        assert oilfield_snapshot(manager) == before
    assert len(llm.extract_calls) == 1


@pytest.mark.parametrize("message", ["若需要，确认", "若需要，去流花11-1油田"])
def test_bare_conditional_prefix_does_not_confirm_or_nominate_oilfield(dialogue, message):
    manager, llm = dialogue
    set_pending(manager)
    before = oilfield_snapshot(manager)
    llm.queue_plan(make_plan("WRITE", pending_action="confirm"))
    llm.queue_extraction(extraction_result(slot_candidate("oilfield_name", "流花11-1油田")))
    manager.process(message)
    assert oilfield_snapshot(manager) == before


@pytest.mark.parametrize("message,accepted", [
    ("若需要，就在这里", False),
    ("就在这里", True),
])
def test_conditional_reference_cannot_be_staged_for_a_later_task(dialogue, message, accepted):
    seeded_manager, llm = dialogue
    manager = DialogueManager(llm, seeded_manager.kb)
    manager._last_discussed_oilfield = "流花11-1油田"
    manager._last_discussed_task_type = "tree_valve_operation"
    manager.process(message)
    assert bool(manager._pending_referential_candidates) is accepted
    assert not manager.task_state.get("oilfield_name")
    manager.process("那开始这个任务")
    assert manager.task_state["task_type_key"] == "tree_valve_operation"
    assert manager.task_state.get("oilfield_name") == ("流花11-1油田" if accepted else None)
