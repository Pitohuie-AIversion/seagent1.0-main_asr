"""Conditional selections and device follow-up questions cannot authorize writes."""

import copy

import pytest

from src.dialogue_manager import DialogueManager
from tests.interaction_plan_support import extraction_result, make_plan, slot_candidate
from tests.test_equipment_family_evidence import (
    NEW_FAMILY, NEW_TYPE, dialogue as equipment_dialogue, equipment_slots,
)
from tests.test_payload_write_grounding_regressions import INITIAL, mutation, valve


def queue_equipment(llm, wrong):
    candidates = [
        slot_candidate("equipment_family", NEW_FAMILY),
        slot_candidate("equipment_type", NEW_TYPE),
        slot_candidate("equipment_unit_id", "LROV-150-001"),
    ] if wrong else []
    llm.queue_extraction(extraction_result(*candidates))


def assert_snapshot(manager, llm, before, *, payload=False):
    restored = DialogueManager(llm, manager.kb)
    restored.load_snapshot(manager.export_snapshot())
    assert (restored.slot_store.slots["payload"].to_dict() if payload else equipment_slots(restored)) == before


@pytest.mark.parametrize("message", [
    "若天气允许，改用天鹰座150HP。",
    "若改用天鹰座150HP。",
    "改用天鹰座150HP，若天气允许。",
    "改用天鹰座150HP，这个机器人合适吗？",
    "改用天鹰座150HP，该设备可以吗？",
    "改用天鹰座150HP，这个设备型号可以吗？",
    "改用天鹰座150HP，天鹰座150HP合适吗？",
])
@pytest.mark.parametrize("wrong", [False, True], ids=["empty", "wrong"])
def test_device_condition_or_same_selection_question_preserves_all_slots(equipment_dialogue, message, wrong):
    manager, llm = equipment_dialogue
    before = equipment_slots(manager)
    queue_equipment(llm, wrong)

    manager.process(message + "水深改为320米。")

    assert equipment_slots(manager) == before
    assert manager.task_state["water_depth"] == 320
    assert_snapshot(manager, llm, before)


@pytest.mark.parametrize("message", [
    "若需要，删除阀门扭矩工具。",
    "若删除阀门扭矩工具。",
    "若需要，保留阀门扭矩工具并删除液压飞线插拔工具。",
    "添加激光标尺，这个工具合适吗？",
])
@pytest.mark.parametrize("wrong", [False, True], ids=["empty", "wrong"])
def test_payload_condition_preserves_metadata_and_independent_depth(valve, message, wrong):
    manager, llm = valve
    before = copy.deepcopy(manager.slot_store.slots["payload"].to_dict())
    llm.queue_plan(make_plan("WRITE"))
    llm.queue_extraction(extraction_result(list_mutations=[mutation("clear")] if wrong else []))

    manager.process(message + "水深改为320米。")

    assert manager.slot_store.slots["payload"].to_dict() == before
    assert manager.task_state["water_depth"] == 320
    assert_snapshot(manager, llm, before, payload=True)


@pytest.mark.parametrize("message", [
    "不要使用OBSROV-75-001，改用天鹰座150HP。",
    "昨天使用OBSROV-75-001，现在改用天鹰座150HP。",
    "若天气允许，改用观察级75HP。现在改用天鹰座150HP。",
    "改用天鹰座150HP，当前水深是多少？",
    "改用天鹰座150HP，这个机器人支持哪些工具？",
    "改用天鹰座150HP，这个工具合适吗？",
])
def test_independent_affirmative_device_choice_still_updates(equipment_dialogue, message):
    manager, llm = equipment_dialogue
    queue_equipment(llm, False)

    manager.process(message)

    assert manager.task_state["equipment_type"] == NEW_TYPE
    assert not manager.task_state.get("equipment_unit_id")
    assert manager.slot_store.slots["equipment_type"].validation_error is None
    assert_snapshot(manager, llm, equipment_slots(manager))


@pytest.mark.parametrize("message", [
    "不要删除阀门扭矩工具，添加激光标尺。",
    "若需要，删除阀门扭矩工具。现在添加激光标尺。",
    "昨天，删除阀门扭矩工具，现在添加激光标尺。",
])
def test_independent_affirmative_payload_choice_still_updates(valve, message):
    manager, llm = valve
    llm.queue_plan(make_plan("WRITE"))
    llm.queue_extraction(extraction_result(list_mutations=[mutation("clear")]))

    manager.process(message)

    slot = manager.slot_store.slots["payload"]
    assert slot.value == INITIAL + ["激光标尺"]
    assert slot.status == "valid"
    assert slot.validation_error is None
    assert_snapshot(manager, llm, slot.to_dict(), payload=True)
