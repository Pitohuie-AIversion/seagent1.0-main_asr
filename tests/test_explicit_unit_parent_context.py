"""Explicit unit changes must resolve before considering stale saved parents."""
import pytest

from src.dialogue_manager import DialogueManager
from src.knowledge_retriever import KnowledgeBase
from src.slots.slot_store import Slot
from tests.interaction_plan_support import ScriptedLLM


@pytest.fixture
def selected_observation_robot():
    manager = DialogueManager(ScriptedLLM(), KnowledgeBase())
    manager.slot_store.init_task_slots(manager.builder.get_schema("pipeline_inspection", "normal"))
    unit = manager.kb.resolve_robot_unit("OBSROV-75-001", "pipeline_inspection")
    values = {
        "task_type_key": "pipeline_inspection",
        "equipment_class": unit["robot"]["robot_class"],
        "equipment_family": unit["robot"]["family_full_name"],
        "equipment_type": unit["robot"]["full_name"],
        "equipment_unit_id": unit["unit_id"],
        "equipment_name": unit["display_name"],
    }
    for key, value in values.items():
        manager.slot_store.slots[key] = Slot(key, value=value, status="valid")
    return manager


@pytest.mark.parametrize("selector,expected_unit", [
    ("LROV-150-001", "LROV-150-001"),
    ("天鹰座一号机", "LROV-150-001"),
    ("AUV-324cc-001", "AUV-324cc-001"),
])
def test_exact_unit_replaces_prior_family_and_variant(selected_observation_robot, selector, expected_unit):
    manager = selected_observation_robot
    manager._handle_equipment_updates_in_transaction(
        {"equipment_unit_id": selector}, manager.slot_store.slots, allow_overwrite=True,
    )
    expected = manager.kb.resolve_robot_unit(expected_unit, "pipeline_inspection")
    assert manager.slot_store.slots["equipment_unit_id"].value == expected_unit
    assert manager.slot_store.slots["equipment_unit_id"].status == "valid"
    assert manager.slot_store.slots["equipment_family"].value == expected["robot"]["family_full_name"]
    assert manager.slot_store.slots["equipment_type"].value == expected["robot"]["full_name"]
    assert manager.slot_store.slots["equipment_class"].value == expected["robot"]["robot_class"]


@pytest.mark.parametrize("explicit_parent", [
    {"equipment_class": "auv"},
    {"equipment_family": "观察级深海机器人"},
    {"equipment_type": "观察级深海机器人 75HP"},
])
def test_same_turn_parent_conflict_cannot_be_discarded(selected_observation_robot, explicit_parent):
    manager = selected_observation_robot
    manager._handle_equipment_updates_in_transaction(
        {**explicit_parent, "equipment_unit_id": "LROV-150-001"},
        manager.slot_store.slots, allow_overwrite=True,
    )
    slot = manager.slot_store.slots["equipment_unit_id"]
    assert slot.status in {"invalid", "conflict"}
    assert slot.value == "OBSROV-75-001"
    assert slot.candidate_value == "LROV-150-001"
    assert "mismatched" in slot.validation_error


def test_no_overwrite_fence_still_applies_to_cross_family_unit(selected_observation_robot):
    manager = selected_observation_robot
    manager._handle_equipment_updates_in_transaction(
        {"equipment_unit_id": "LROV-150-001"}, manager.slot_store.slots, allow_overwrite=False,
    )
    slot = manager.slot_store.slots["equipment_unit_id"]
    assert slot.value == "OBSROV-75-001"
    assert slot.status == "conflict"
    assert slot.candidate_value == "LROV-150-001"
    assert manager.slot_store.slots["equipment_family"].value == "观察级深海机器人"


def test_known_incompatible_unit_is_reported_as_incompatible(selected_observation_robot):
    manager = selected_observation_robot
    manager._handle_equipment_updates_in_transaction(
        {"equipment_unit_id": "WROV-250-001"}, manager.slot_store.slots, allow_overwrite=True,
    )
    slot = manager.slot_store.slots["equipment_unit_id"]
    assert slot.value == "OBSROV-75-001"
    assert slot.status == "conflict"
    assert slot.candidate_value == "WROV-250-001"
    assert "不支持当前任务" in slot.validation_error
