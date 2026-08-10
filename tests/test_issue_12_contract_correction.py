"""
Test suite for Issue #12 Contract Correction:
Fix Robot Cascade Data Model by removing equipment_specification slot
and restoring class -> family -> model_variant -> unit 4-level hierarchy.
"""

import pytest
import yaml
from src.knowledge_retriever import KnowledgeBase, RobotSelectionDataError
from src.slot_store import (
    SlotStore,
    Slot,
    BASE_SLOT_TYPES,
    ROBOT_CASCADE_DEPENDENCIES,
    SnapshotValidationError,
)
from src.dialogue_manager import DialogueManager
from src.output_builder import OutputBuilder
from src.normalization_contract import NORMALIZATION_RUNTIME_PASSTHROUGH_KEYS


@pytest.fixture
def kb():
    return KnowledgeBase()


@pytest.fixture
def slot_store(kb):
    return SlotStore(kb=kb)


@pytest.fixture
def dm(kb):
    return DialogueManager(kb=kb)


# Scenario 1: task_schemas.yaml does not contain equipment_specification
def test_task_schemas_no_equipment_specification(kb):
    schemas = kb.task_schemas.get("task_schemas", {})
    for task_key in ["pipeline_inspection", "pipeline_burial", "tree_valve_operation"]:
        slots = schemas.get(task_key, {}).get("slots", [])
        slot_keys = [s.get("key") for s in slots if isinstance(s, dict)]
        assert "equipment_specification" not in slot_keys, f"equipment_specification found in {task_key}"


# Scenario 2: BASE_SLOT_TYPES does not contain equipment_specification
def test_base_slot_types_no_equipment_specification():
    assert "equipment_specification" not in BASE_SLOT_TYPES
    assert "equipment_type" in BASE_SLOT_TYPES
    assert BASE_SLOT_TYPES["equipment_type"] == "string"


# Scenario 3: ROBOT_CASCADE_DEPENDENCIES has exact 4-level structure
def test_robot_cascade_dependencies_structure():
    assert "equipment_specification" not in ROBOT_CASCADE_DEPENDENCIES
    assert list(ROBOT_CASCADE_DEPENDENCIES.get("equipment_class")) == [
        "equipment_family",
        "equipment_type",
        "equipment_unit_id",
        "equipment_name",
    ]
    assert list(ROBOT_CASCADE_DEPENDENCIES.get("equipment_family")) == [
        "equipment_type",
        "equipment_unit_id",
        "equipment_name",
    ]
    assert list(ROBOT_CASCADE_DEPENDENCIES.get("equipment_type")) == [
        "equipment_unit_id",
        "equipment_name",
    ]


# Scenario 4: KnowledgeBase.list_robot_variants
def test_kb_list_robot_variants(kb):
    variants = kb.list_robot_variants("work_class_rov", "general_work_class_rov")
    assert len(variants) > 0
    variant_ids = [v["variant_id"] for v in variants]
    assert "general_work_class_rov_250hp" in variant_ids


# Scenario 5: KnowledgeBase.list_robot_units accepts equipment_type
def test_kb_list_robot_units(kb):
    units = kb.list_robot_units("work_class_rov", "general_work_class_rov", "通用工作级深海机器人 250HP")
    assert len(units) > 0
    unit_ids = [u["unit_id"] for u in units]
    assert "WROV-250-001" in unit_ids


# Scenario 6: KnowledgeBase.validate_static_robot_selection 4-level validation
def test_kb_validate_static_robot_selection(kb):
    res = kb.validate_static_robot_selection(
        "work_class_rov",
        "general_work_class_rov",
        "通用工作级深海机器人 250HP",
        "WROV-250-001",
    )
    assert res["robot_class"] == "work_class_rov"
    assert res["family_id"] == "general_work_class_rov"
    assert res["variant_id"] == "general_work_class_rov_250hp"
    assert res["unit_id"] == "WROV-250-001"
    assert "equipment_type" in res


# Scenario 7: User HP / CC resolution to equipment_type
def test_user_hp_cc_resolution(dm):
    # 250HP -> General Work Class 250HP
    dm._handle_equipment_updates_in_transaction(
        {"equipment_type": "250HP"},
        dm.slot_store.slots,
        allow_overwrite=True,
    )
    slots = dm.slot_store.slots
    assert slots["equipment_class"].value == "work_class_rov"
    assert slots["equipment_family"].value == "通用工作级深海机器人"
    assert slots["equipment_type"].value == "通用工作级深海机器人 250HP"
    assert "equipment_specification" not in slots

    # 324CC -> AUV 324CC
    dm.slot_store = SlotStore(kb=dm.kb)
    dm._handle_equipment_updates_in_transaction(
        {"equipment_type": "324CC"},
        dm.slot_store.slots,
        allow_overwrite=True,
    )
    slots = dm.slot_store.slots
    assert slots["equipment_class"].value == "auv"
    assert slots["equipment_family"].value == "水下无人自主航行器"
    assert slots["equipment_type"].value == "水下无人自主航行器 324CC"


# Scenario 8: Null HP model variants (light_work_class_rov, observation_rov) remain selectable
def test_null_hp_variant_selectable(dm):
    dm._handle_equipment_updates_in_transaction(
        {"equipment_type": "轻型工作级深海机器人"},
        dm.slot_store.slots,
        allow_overwrite=True,
    )
    slots = dm.slot_store.slots
    assert slots["equipment_class"].value == "observation_rov"
    assert slots["equipment_family"].value == "轻型工作级深海机器人"
    assert slots["equipment_type"].value == "轻型工作级深海机器人"


# Scenario 9: 4-level cascade forward completion
def test_forward_cascade_completion(dm):
    dm._handle_equipment_updates_in_transaction({"equipment_class": "work_class_rov"}, dm.slot_store.slots, allow_overwrite=True)
    assert dm.slot_store.slots["equipment_class"].value == "work_class_rov"

    dm._handle_equipment_updates_in_transaction({"equipment_family": "通用工作级深海机器人"}, dm.slot_store.slots, allow_overwrite=True)
    assert dm.slot_store.slots["equipment_family"].value == "通用工作级深海机器人"

    dm._handle_equipment_updates_in_transaction({"equipment_type": "通用工作级深海机器人 250HP"}, dm.slot_store.slots, allow_overwrite=True)
    assert dm.slot_store.slots["equipment_type"].value == "通用工作级深海机器人 250HP"

    dm._handle_equipment_updates_in_transaction({"equipment_unit_id": "WROV-250-001"}, dm.slot_store.slots, allow_overwrite=True)
    assert dm.slot_store.slots["equipment_unit_id"].value == "WROV-250-001"


# Scenario 10: Reverse auto-fill from equipment_unit_id
def test_reverse_autofill_from_unit_id(dm):
    dm._handle_equipment_updates_in_transaction({"equipment_unit_id": "WROV-250-001"}, dm.slot_store.slots, allow_overwrite=True)
    slots = dm.slot_store.slots
    assert slots["equipment_class"].value == "work_class_rov"
    assert slots["equipment_family"].value == "通用工作级深海机器人"
    assert slots["equipment_type"].value == "通用工作级深海机器人 250HP"
    assert slots["equipment_unit_id"].value == "WROV-250-001"
    assert "equipment_specification" not in slots


# Scenario 11: Reverse auto-fill from equipment_type
def test_reverse_autofill_from_equipment_type(dm):
    dm._handle_equipment_updates_in_transaction({"equipment_type": "通用工作级深海机器人 250HP"}, dm.slot_store.slots, allow_overwrite=True)
    slots = dm.slot_store.slots
    assert slots["equipment_class"].value == "work_class_rov"
    assert slots["equipment_family"].value == "通用工作级深海机器人"
    assert slots["equipment_type"].value == "通用工作级深海机器人 250HP"


# Scenario 12: Cascade invalidation on mutating equipment_class
def test_cascade_invalidation_mutate_class(dm):
    test_forward_cascade_completion(dm)
    # Mutate class
    dm._handle_equipment_updates_in_transaction({"equipment_class": "auv"}, dm.slot_store.slots, allow_overwrite=True)
    slots = dm.slot_store.slots
    assert slots["equipment_class"].value == "auv"
    assert slots["equipment_family"].status == "missing"
    assert slots["equipment_type"].status == "missing"
    assert slots["equipment_unit_id"].status == "missing"


# Scenario 13: Cascade invalidation on mutating equipment_family
def test_cascade_invalidation_mutate_family(dm):
    test_forward_cascade_completion(dm)
    dm._handle_equipment_updates_in_transaction({"equipment_family": "轻型工作级深海机器人"}, dm.slot_store.slots, allow_overwrite=True)
    slots = dm.slot_store.slots
    assert slots["equipment_family"].value == "轻型工作级深海机器人"
    assert slots["equipment_type"].status == "missing"
    assert slots["equipment_unit_id"].status == "missing"


# Scenario 14: Cascade invalidation on mutating equipment_type
def test_cascade_invalidation_mutate_type(dm):
    test_forward_cascade_completion(dm)
    dm._handle_equipment_updates_in_transaction({"equipment_type": "特种工作级深海机器人 600HP"}, dm.slot_store.slots, allow_overwrite=True)
    slots = dm.slot_store.slots
    assert slots["equipment_type"].value == "特种工作级深海机器人 600HP"
    assert slots["equipment_unit_id"].status == "missing"


# Scenario 15: Mutating equipment_unit_id does not clear parent fields
def test_mutate_unit_id_preserves_parents(dm):
    dm._handle_equipment_updates_in_transaction({"equipment_unit_id": "LROV--001"}, dm.slot_store.slots, allow_overwrite=True)
    dm._handle_equipment_updates_in_transaction({"equipment_unit_id": "LROV--002"}, dm.slot_store.slots, allow_overwrite=True)
    slots = dm.slot_store.slots
    assert slots["equipment_class"].value == "observation_rov"
    assert slots["equipment_family"].value == "轻型工作级深海机器人"
    assert slots["equipment_type"].value == "轻型工作级深海机器人"
    assert slots["equipment_unit_id"].value == "LROV--002"


# Scenario 16: Conflict Fence
def test_conflict_fence(dm):
    test_forward_cascade_completion(dm)
    # Attempting to change family with allow_overwrite=False should trigger Conflict Fence
    dm._handle_equipment_updates_in_transaction({"equipment_family": "水下无人自主航行器"}, dm.slot_store.slots, allow_overwrite=False)
    slots = dm.slot_store.slots
    assert slots["equipment_family"].status == "conflict"
    assert slots["equipment_family"].value == "通用工作级深海机器人"
    assert slots["equipment_family"].candidate_value == "水下无人自主航行器"


# Scenario 17: Legacy Snapshot Migration Case A (valid equipment_type present)
def test_legacy_snapshot_migration_case_a(kb):
    store = SlotStore(kb=kb)
    legacy_snapshot = {
        "version": 1,
        "slots": {
            "equipment_class": {"slot_name": "equipment_class", "value": "work_class_rov", "status": "valid", "value_type": "string"},
            "equipment_family": {"slot_name": "equipment_family", "value": "通用工作级深海机器人", "status": "valid", "value_type": "string"},
            "equipment_type": {"slot_name": "equipment_type", "value": "通用工作级深海机器人 250HP", "status": "valid", "value_type": "string"},
            "equipment_specification": {
                "slot_name": "equipment_specification",
                "value": {"type": "power_hp", "value": 250.0, "unit": "HP", "variant_id": "general_work_class_rov_250hp"},
                "status": "valid",
                "value_type": "object",
            },
        },
    }
    store.restore_snapshot(legacy_snapshot)
    assert "equipment_specification" not in store.slots
    assert store.slots["equipment_type"].value == "通用工作级深海机器人 250HP"
    assert store.slots["equipment_type"].status == "valid"


# Scenario 18: Legacy Snapshot Migration Case B (legacy spec present, equipment_type missing -> backfill)
def test_legacy_snapshot_migration_case_b(kb):
    store = SlotStore(kb=kb)
    legacy_snapshot = {
        "version": 1,
        "slots": {
            "equipment_class": {"slot_name": "equipment_class", "value": "work_class_rov", "status": "valid", "value_type": "string"},
            "equipment_family": {"slot_name": "equipment_family", "value": "通用工作级深海机器人", "status": "valid", "value_type": "string"},
            "equipment_specification": {
                "slot_name": "equipment_specification",
                "value": {"type": "power_hp", "value": 250.0, "unit": "HP", "variant_id": "general_work_class_rov_250hp"},
                "status": "valid",
                "value_type": "object",
            },
        },
    }
    store.restore_snapshot(legacy_snapshot)
    assert "equipment_specification" not in store.slots
    assert store.slots["equipment_type"].value == "通用工作级深海机器人 250HP"
    assert store.slots["equipment_type"].status == "valid"


# Scenario 19: Legacy Snapshot Migration Case C (invalid spec / mismatch -> fail closed)
def test_legacy_snapshot_migration_case_c(kb):
    store = SlotStore(kb=kb)
    legacy_snapshot = {
        "version": 1,
        "slots": {
            "equipment_class": {"slot_name": "equipment_class", "value": "auv", "status": "valid", "value_type": "string"},
            "equipment_family": {"slot_name": "equipment_family", "value": "水下无人自主航行器", "status": "valid", "value_type": "string"},
            "equipment_specification": {
                "slot_name": "equipment_specification",
                "value": {"type": "power_hp", "value": 250.0, "unit": "HP", "variant_id": "general_work_class_rov_250hp"},  # mismatch
                "status": "valid",
                "value_type": "object",
            },
        },
    }
    store.restore_snapshot(legacy_snapshot)
    assert "equipment_specification" not in store.slots
    assert store.slots["equipment_type"].status == "missing"


# Scenario 20: Flat JSON output Builder has no equipment_specification
def test_flat_task_intent_builder_output(kb):
    builder = OutputBuilder(kb=kb)
    state = {
        "equipment_class": "工作级ROV",
        "equipment_family": "通用工作级深海机器人",
        "equipment_type": "通用工作级深海机器人 250HP",
        "equipment_unit_id": "WROV-250-001",
    }
    result, _ = builder.build(state, "tree_valve_operation")
    assert "equipment_class" in result
    assert "equipment_family" in result
    assert "equipment_type" in result
    assert "equipment_unit_id" in result
    assert "equipment_specification" not in result


from src.prompts import build_responder_messages

# Scenario 21: Prompts field dependency instructions
def test_prompts_field_dependency_instructions():
    missing_fields = [{"key": "equipment_type", "label": "作业设备型号", "allowed_values": ["通用工作级深海机器人 250HP"]}]
    built_json = {}
    task_state = {"equipment_class": "work_class_rov", "equipment_family": "通用工作级深海机器人"}
    msgs = build_responder_messages(
        task_state=task_state,
        built_json=built_json,
        missing_fields=missing_fields,
        mode="normal",
        phase="slot_filling",
        knowledge_context="",
        constraint_context={"status": "none"},
        conversation_history=[],
        latest_user_message="继续",
        ROV2type={},
        support_task=[],
    )
    prompt_str = str(msgs)
    assert "equipment_type" in prompt_str
    assert "equipment_specification" not in prompt_str


# Scenario 22: Normalization passthrough keys
def test_normalization_passthrough_keys():
    assert "equipment_specification" not in NORMALIZATION_RUNTIME_PASSTHROUGH_KEYS
    assert "equipment_type" in NORMALIZATION_RUNTIME_PASSTHROUGH_KEYS
