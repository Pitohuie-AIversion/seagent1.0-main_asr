"""LIVE-01: contextual add commands must survive a separate keep clause."""

import copy

import pytest

from src.dialogue_manager import DialogueManager
from src.knowledge_retriever import KnowledgeBase
from src.session.ui_state_builder import build_frontend_ui_state
from src.slots.slot_store import BASE_SLOT_TYPES, Slot
from tests.interaction_plan_support import ScriptedLLM, extraction_result, make_plan


SONAR = "侧扫声呐"
PROFILER = "浅地层剖面仪"
LIVE_MESSAGE = (
    "在现有携带工具中再添加浅地层剖面仪，保留侧扫声呐。"
    "起点、机器人和其他参数均不修改。"
)


@pytest.fixture(scope="module")
def kb():
    return KnowledgeBase()


@pytest.fixture(params=[(False, False), (True, False), (True, True)],
                ids=["legacy", "patch-v2", "norm-v2"])
def dialogue(request, monkeypatch, kb):
    patch_enabled, norm_enabled = request.param
    monkeypatch.setattr("src.dialogue_manager.is_task_patch_v2_enabled", lambda: patch_enabled)
    monkeypatch.setattr("src.dialogue_manager.is_normalization_contract_v2_enabled", lambda: norm_enabled)
    llm = ScriptedLLM(default_reply="参数已处理。")
    manager = DialogueManager(llm, kb)
    manager.slot_store.init_task_slots(manager.builder.get_schema("pipeline_inspection", "normal"))
    slots = manager.slot_store.clone_slots()
    values = {
        "task_type_key": "pipeline_inspection", "task_type": "管缆巡检",
        "cable_type": "电力电缆", "water_depth": 300,
        "start_point": {"lat": 19.9, "lon": 113.6},
        "equipment_class": "auv", "equipment_family": "水下无人自主航行器",
        "equipment_type": "水下无人自主航行器 324CC",
        "equipment_unit_id": "AUV-324cc-001", "payload": [SONAR],
    }
    for key, value in values.items():
        slots[key] = Slot(
            key, value=copy.deepcopy(value), value_type=BASE_SLOT_TYPES.get(key, "string"),
            status="valid", source="user_input", raw_value="previous confirmed value",
        )
    manager.slot_store.commit_transaction(slots, [], request_id="seed-live-prefix")
    manager._rebuild_cache()
    manager.phase = "collecting"
    return manager, llm


def process(manager, llm, message, proposal="correct-add", item=PROFILER):
    mutations = [] if proposal == "empty" else [{
        "field": "payload", "operation": "clear" if proposal == "wrong-clear" else "add",
        "items": [] if proposal == "wrong-clear" else [item], "target_items": [],
        "raw_text": "model proposal", "confidence": 0.95, "source": "user_input",
    }]
    llm.queue_plan(make_plan("WRITE"))
    llm.queue_extraction(extraction_result(list_mutations=mutations))
    return manager.process(message)


@pytest.mark.parametrize("message", [
    LIVE_MESSAGE,
    "在现有携带工具中再添加浅地层剖面仪并保留侧扫声呐。",
    "保留侧扫声呐，在现有携带工具中添加浅地层剖面仪。不要修改起点。",
])
@pytest.mark.parametrize("proposal", ["empty", "correct-add", "wrong-clear"])
def test_explicit_contextual_add_preserves_saved_tool_and_other_fields(dialogue, message, proposal):
    manager, llm = dialogue
    before = copy.deepcopy(manager.slot_store.get_slot_snapshot())

    reply = process(manager, llm, message, proposal)

    payload = manager.slot_store.slots["payload"]
    assert payload.value == [SONAR, PROFILER]
    assert payload.status == "valid"
    assert payload.raw_value == message
    assert payload.source == "user_input"
    assert payload.validation_error is None
    assert manager.task_state["payload"] == manager._last_built_json["payload"] == [SONAR, PROFILER]
    for key in ("start_point", "water_depth", "equipment_type", "equipment_unit_id"):
        assert manager.slot_store.slots[key].to_dict() == before[key]
    assert len(llm.extract_calls) == 1
    assert PROFILER in reply
    assert manager.phase == "collecting"
    assert not build_frontend_ui_state(manager)["actions"]["can_publish"]


@pytest.mark.parametrize("message", [
    "不要在现有携带工具中再添加浅地层剖面仪，保留侧扫声呐。",
    "在现有携带工具中不要再添加浅地层剖面仪，保留侧扫声呐。",
    "如果需要，在现有携带工具中再添加浅地层剖面仪，保留侧扫声呐。",
    "在现有携带工具中再添加浅地层剖面仪并保留侧扫声呐，可以吗？",
    "之前在现有携带工具中添加浅地层剖面仪，现在保留侧扫声呐。",
    "不要修改载荷，在现有携带工具中再添加浅地层剖面仪，保留侧扫声呐。",
])
def test_context_prefix_does_not_bypass_assertion_guards(dialogue, message):
    manager, llm = dialogue
    before = copy.deepcopy(manager.slot_store.slots["payload"].to_dict())

    reply = process(manager, llm, message)

    assert manager.slot_store.slots["payload"].to_dict() == before
    assert manager.task_state["payload"] == [SONAR]
    assert not any("携带工具" in line for line in reply.splitlines() if line.startswith("✅ 已记录："))


@pytest.mark.parametrize("item", ["不存在的工具", "高清水下摄像机"])
def test_contextual_add_still_checks_catalog_and_selected_robot(dialogue, item):
    manager, llm = dialogue

    reply = process(manager, llm, f"在现有携带工具中再添加{item}，保留侧扫声呐。", item=item)

    payload = manager.slot_store.slots["payload"]
    assert payload.value == [SONAR]
    assert payload.validation_error
    assert manager.task_state["payload"] == [SONAR]
    assert not any(item in line for line in reply.splitlines() if line.startswith("✅ 已记录："))
