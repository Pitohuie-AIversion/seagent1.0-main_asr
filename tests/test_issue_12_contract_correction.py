"""
Test suite for Issue #12 Contract Correction:
Fix Robot Cascade Data Model by removing equipment_specification slot
and restoring class -> family -> model_variant -> unit 4-level hierarchy.
"""

import unittest
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
from src.prompts import build_responder_messages


class TestIssue12ContractCorrection(unittest.TestCase):
    def setUp(self):
        self.kb = KnowledgeBase()
        self.slot_store = SlotStore(kb=self.kb)
        self.dm = DialogueManager(kb=self.kb)

    # Scenario 1: task_schemas.yaml contains equipment_class, family, type, unit in order and no equipment_specification
    def test_task_schemas_no_equipment_specification(self):
        templates = self.kb.task_schemas.get("task_templates", {})
        for task_key in ["pipeline_inspection", "pipeline_burial", "tree_valve_operation"]:
            normal_slots = templates.get(task_key, {}).get("output_schema", {}).get("normal", [])
            slot_keys = [s.get("key") for s in normal_slots if isinstance(s, dict)]

            self.assertIn("equipment_class", slot_keys, f"equipment_class missing in {task_key}")
            self.assertIn("equipment_family", slot_keys, f"equipment_family missing in {task_key}")
            self.assertIn("equipment_type", slot_keys, f"equipment_type missing in {task_key}")
            self.assertIn("equipment_unit_id", slot_keys, f"equipment_unit_id missing in {task_key}")
            self.assertNotIn("equipment_specification", slot_keys, f"equipment_specification unexpectedly found in {task_key}")

            cls_idx = slot_keys.index("equipment_class")
            fam_idx = slot_keys.index("equipment_family")
            typ_idx = slot_keys.index("equipment_type")
            unit_idx = slot_keys.index("equipment_unit_id")
            self.assertTrue(cls_idx < fam_idx < typ_idx < unit_idx, f"Slot ordering incorrect in {task_key}")

    # Scenario 2: BASE_SLOT_TYPES does not contain equipment_specification
    def test_base_slot_types_no_equipment_specification(self):
        self.assertNotIn("equipment_specification", BASE_SLOT_TYPES)
        self.assertIn("equipment_type", BASE_SLOT_TYPES)
        self.assertEqual(BASE_SLOT_TYPES["equipment_type"], "string")

    # Scenario 3: ROBOT_CASCADE_DEPENDENCIES has exact 4-level structure
    def test_robot_cascade_dependencies_structure(self):
        self.assertNotIn("equipment_specification", ROBOT_CASCADE_DEPENDENCIES)
        self.assertEqual(
            list(ROBOT_CASCADE_DEPENDENCIES.get("equipment_class")),
            ["equipment_family", "equipment_type", "equipment_unit_id", "equipment_name"],
        )
        self.assertEqual(
            list(ROBOT_CASCADE_DEPENDENCIES.get("equipment_family")),
            ["equipment_type", "equipment_unit_id", "equipment_name"],
        )
        self.assertEqual(
            list(ROBOT_CASCADE_DEPENDENCIES.get("equipment_type")),
            ["equipment_unit_id", "equipment_name"],
        )

    # Scenario 4: KnowledgeBase.list_robot_variants
    def test_kb_list_robot_variants(self):
        variants = self.kb.list_robot_variants("work_class_rov", "general_work_class_rov")
        self.assertTrue(len(variants) > 0)
        variant_ids = [v["variant_id"] for v in variants]
        self.assertIn("general_work_class_rov_250hp", variant_ids)

    # Scenario 5: KnowledgeBase.list_robot_units accepts equipment_type
    def test_kb_list_robot_units(self):
        units = self.kb.list_robot_units("work_class_rov", "general_work_class_rov", "通用工作级深海机器人 250HP")
        self.assertTrue(len(units) > 0)
        unit_ids = [u["unit_id"] for u in units]
        self.assertIn("WROV-250-001", unit_ids)

    # Scenario 6: KnowledgeBase.validate_static_robot_selection 4-level validation
    def test_kb_validate_static_robot_selection(self):
        res = self.kb.validate_static_robot_selection(
            "work_class_rov",
            "general_work_class_rov",
            "通用工作级深海机器人 250HP",
            "WROV-250-001",
        )
        self.assertEqual(res["robot_class"], "work_class_rov")
        self.assertEqual(res["family_id"], "general_work_class_rov")
        self.assertEqual(res["variant_id"], "general_work_class_rov_250hp")
        self.assertEqual(res["unit_id"], "WROV-250-001")
        self.assertIn("equipment_type", res)

    # Scenario 7: User HP / CC resolution to equipment_type
    def test_user_hp_cc_resolution(self):
        # 250HP -> General Work Class 250HP
        self.dm._handle_equipment_updates_in_transaction(
            {"equipment_type": "250HP"},
            self.dm.slot_store.slots,
            allow_overwrite=True,
        )
        slots = self.dm.slot_store.slots
        self.assertEqual(slots["equipment_class"].value, "work_class_rov")
        self.assertEqual(slots["equipment_family"].value, "通用工作级深海机器人")
        self.assertEqual(slots["equipment_type"].value, "通用工作级深海机器人 250HP")
        self.assertNotIn("equipment_specification", slots)

        # 324CC -> AUV 324CC
        self.dm.slot_store = SlotStore(kb=self.kb)
        self.dm._handle_equipment_updates_in_transaction(
            {"equipment_type": "324CC"},
            self.dm.slot_store.slots,
            allow_overwrite=True,
        )
        slots = self.dm.slot_store.slots
        self.assertEqual(slots["equipment_class"].value, "auv")
        self.assertEqual(slots["equipment_family"].value, "水下无人自主航行器")
        self.assertEqual(slots["equipment_type"].value, "水下无人自主航行器 324CC")

    # Scenario 8: Null HP model variants (light_work_class_rov, observation_rov) remain selectable
    def test_null_hp_variant_selectable(self):
        self.dm._handle_equipment_updates_in_transaction(
            {"equipment_type": "轻型工作级深海机器人"},
            self.dm.slot_store.slots,
            allow_overwrite=True,
        )
        slots = self.dm.slot_store.slots
        self.assertEqual(slots["equipment_class"].value, "observation_rov")
        self.assertEqual(slots["equipment_family"].value, "轻型工作级深海机器人")
        self.assertEqual(slots["equipment_type"].value, "轻型工作级深海机器人")

    # Scenario 9: 4-level cascade forward completion
    def test_forward_cascade_completion(self):
        self.dm._handle_equipment_updates_in_transaction({"equipment_class": "work_class_rov"}, self.dm.slot_store.slots, allow_overwrite=True)
        self.assertEqual(self.dm.slot_store.slots["equipment_class"].value, "work_class_rov")

        self.dm._handle_equipment_updates_in_transaction({"equipment_family": "通用工作级深海机器人"}, self.dm.slot_store.slots, allow_overwrite=True)
        self.assertEqual(self.dm.slot_store.slots["equipment_family"].value, "通用工作级深海机器人")

        self.dm._handle_equipment_updates_in_transaction({"equipment_type": "通用工作级深海机器人 250HP"}, self.dm.slot_store.slots, allow_overwrite=True)
        self.assertEqual(self.dm.slot_store.slots["equipment_type"].value, "通用工作级深海机器人 250HP")

        self.dm._handle_equipment_updates_in_transaction({"equipment_unit_id": "WROV-250-001"}, self.dm.slot_store.slots, allow_overwrite=True)
        self.assertEqual(self.dm.slot_store.slots["equipment_unit_id"].value, "WROV-250-001")

    # Scenario 10: Reverse auto-fill from equipment_unit_id
    def test_reverse_autofill_from_unit_id(self):
        self.dm._handle_equipment_updates_in_transaction({"equipment_unit_id": "WROV-250-001"}, self.dm.slot_store.slots, allow_overwrite=True)
        slots = self.dm.slot_store.slots
        self.assertEqual(slots["equipment_class"].value, "work_class_rov")
        self.assertEqual(slots["equipment_family"].value, "通用工作级深海机器人")
        self.assertEqual(slots["equipment_type"].value, "通用工作级深海机器人 250HP")
        self.assertEqual(slots["equipment_unit_id"].value, "WROV-250-001")
        self.assertNotIn("equipment_specification", slots)

    # Scenario 11: Reverse auto-fill from equipment_type
    def test_reverse_autofill_from_equipment_type(self):
        self.dm._handle_equipment_updates_in_transaction({"equipment_type": "通用工作级深海机器人 250HP"}, self.dm.slot_store.slots, allow_overwrite=True)
        slots = self.dm.slot_store.slots
        self.assertEqual(slots["equipment_class"].value, "work_class_rov")
        self.assertEqual(slots["equipment_family"].value, "通用工作级深海机器人")
        self.assertEqual(slots["equipment_type"].value, "通用工作级深海机器人 250HP")

    # Scenario 12: Cascade invalidation on mutating equipment_class
    def test_cascade_invalidation_mutate_class(self):
        self.test_forward_cascade_completion()
        # Mutate class
        self.dm._handle_equipment_updates_in_transaction({"equipment_class": "auv"}, self.dm.slot_store.slots, allow_overwrite=True)
        slots = self.dm.slot_store.slots
        self.assertEqual(slots["equipment_class"].value, "auv")
        self.assertEqual(slots["equipment_family"].status, "missing")
        self.assertEqual(slots["equipment_type"].status, "missing")
        self.assertEqual(slots["equipment_unit_id"].status, "missing")

    # Scenario 13: Cascade invalidation on mutating equipment_family
    def test_cascade_invalidation_mutate_family(self):
        self.test_forward_cascade_completion()
        self.dm._handle_equipment_updates_in_transaction({"equipment_family": "轻型工作级深海机器人"}, self.dm.slot_store.slots, allow_overwrite=True)
        slots = self.dm.slot_store.slots
        self.assertEqual(slots["equipment_family"].value, "轻型工作级深海机器人")
        self.assertEqual(slots["equipment_type"].status, "missing")
        self.assertEqual(slots["equipment_unit_id"].status, "missing")

    # Scenario 14: Cascade invalidation on mutating equipment_type
    def test_cascade_invalidation_mutate_type(self):
        self.test_forward_cascade_completion()
        self.dm._handle_equipment_updates_in_transaction({"equipment_type": "特种工作级深海机器人 600HP"}, self.dm.slot_store.slots, allow_overwrite=True)
        slots = self.dm.slot_store.slots
        self.assertEqual(slots["equipment_type"].value, "特种工作级深海机器人 600HP")
        self.assertEqual(slots["equipment_unit_id"].status, "missing")

    # Scenario 15: Mutating equipment_unit_id does not clear parent fields
    def test_mutate_unit_id_preserves_parents(self):
        self.dm._handle_equipment_updates_in_transaction({"equipment_unit_id": "LROV--001"}, self.dm.slot_store.slots, allow_overwrite=True)
        self.dm._handle_equipment_updates_in_transaction({"equipment_unit_id": "LROV--002"}, self.dm.slot_store.slots, allow_overwrite=True)
        slots = self.dm.slot_store.slots
        self.assertEqual(slots["equipment_class"].value, "observation_rov")
        self.assertEqual(slots["equipment_family"].value, "轻型工作级深海机器人")
        self.assertEqual(slots["equipment_type"].value, "轻型工作级深海机器人")
        self.assertEqual(slots["equipment_unit_id"].value, "LROV--002")

    # Scenario 16: Conflict Fence
    def test_conflict_fence(self):
        self.test_forward_cascade_completion()
        # Attempting to change family with allow_overwrite=False should trigger Conflict Fence
        self.dm._handle_equipment_updates_in_transaction({"equipment_family": "水下无人自主航行器"}, self.dm.slot_store.slots, allow_overwrite=False)
        slots = self.dm.slot_store.slots
        self.assertEqual(slots["equipment_family"].status, "conflict")
        self.assertEqual(slots["equipment_family"].value, "通用工作级深海机器人")
        self.assertEqual(slots["equipment_family"].candidate_value, "水下无人自主航行器")

    # Scenario 17: Legacy Snapshot Migration Case A (valid equipment_type present)
    def test_legacy_snapshot_migration_case_a(self):
        store = SlotStore(kb=self.kb)
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
        self.assertNotIn("equipment_specification", store.slots)
        self.assertEqual(store.slots["equipment_type"].value, "通用工作级深海机器人 250HP")
        self.assertEqual(store.slots["equipment_type"].status, "valid")

    # Scenario 18: Legacy Snapshot Migration Case B (legacy spec present, equipment_type missing -> backfill)
    def test_legacy_snapshot_migration_case_b(self):
        store = SlotStore(kb=self.kb)
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
        self.assertNotIn("equipment_specification", store.slots)
        self.assertEqual(store.slots["equipment_type"].value, "通用工作级深海机器人 250HP")
        self.assertEqual(store.slots["equipment_type"].status, "valid")

    # Scenario 19: Legacy Snapshot Migration Case C (invalid spec / mismatch -> fail closed)
    def test_legacy_snapshot_migration_case_c(self):
        store = SlotStore(kb=self.kb)
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
        self.assertNotIn("equipment_specification", store.slots)
        self.assertEqual(store.slots["equipment_type"].status, "missing")

    # Scenario 20: Flat JSON output Builder has no equipment_specification
    def test_flat_task_intent_builder_output(self):
        builder = OutputBuilder(kb=self.kb)
        state = {
            "equipment_class": "工作级ROV",
            "equipment_family": "通用工作级深海机器人",
            "equipment_type": "通用工作级深海机器人 250HP",
            "equipment_unit_id": "WROV-250-001",
        }
        result, _ = builder.build(state, "tree_valve_operation")
        self.assertIn("equipment_class", result)
        self.assertIn("equipment_family", result)
        self.assertIn("equipment_type", result)
        self.assertIn("equipment_unit_id", result)
        self.assertNotIn("equipment_specification", result)

    # Scenario 21: Prompts field dependency instructions
    def test_prompts_field_dependency_instructions(self):
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
        self.assertIn("equipment_type", prompt_str)
        self.assertNotIn("equipment_specification", prompt_str)

    # Scenario 22: Normalization passthrough keys
    def test_normalization_passthrough_keys(self):
        self.assertNotIn("equipment_specification", NORMALIZATION_RUNTIME_PASSTHROUGH_KEYS)
        self.assertIn("equipment_type", NORMALIZATION_RUNTIME_PASSTHROUGH_KEYS)


if __name__ == "__main__":
    unittest.main()
