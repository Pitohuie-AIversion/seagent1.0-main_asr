import copy
import math
import unittest
from typing import Any, Dict

from src.slot_store import (
    SLOT_SNAPSHOT_SCHEMA_VERSION,
    Slot,
    SlotStore,
    SnapshotValidationError,
)


class TestSlotStoreSnapshotContract(unittest.TestCase):

    def setUp(self):
        self.store = SlotStore()
        self.store.slots["equipment_class"] = Slot(slot_name="equipment_class", value="work_class_rov", status="valid", value_type="string")
        self.store.slots["equipment_family"] = Slot(slot_name="equipment_family", value="通用工作级深海机器人", status="valid", value_type="string")
        self.store.slots["equipment_type"] = Slot(slot_name="equipment_type", value="通用工作级深海机器人 250HP", status="valid", value_type="string")
        self.store.slots["equipment_unit_id"] = Slot(slot_name="equipment_unit_id", value="WROV-250-001", status="valid", value_type="string")

    def assert_restore_failure_is_atomic(self, store: SlotStore, bad_snapshot: Dict[str, Any]):
        before_state = copy.deepcopy(store.export_snapshot())
        before_input = copy.deepcopy(bad_snapshot)

        with self.assertRaises(SnapshotValidationError):
            store.restore_snapshot(bad_snapshot)

        self.assertEqual(store.export_snapshot(), before_state)
        # Input purity check: caller's snapshot input object must not be mutated
        self.assertEqual(bad_snapshot, before_input)

    # 1. Snapshot Schema Version Dispatch Tests
    def test_schema_version_dispatch(self):
        base_snap = self.store.export_snapshot()

        # Missing schema_version -> Legacy V1 success
        v1_snap = copy.deepcopy(base_snap)
        del v1_snap["snapshot_schema_version"]
        restored_v1 = SlotStore.from_snapshot(v1_snap)
        self.assertEqual(restored_v1.slots["equipment_type"].value, "通用工作级深海机器人 250HP")

        # schema_version == 2 -> Canonical V2 success
        v2_snap = copy.deepcopy(base_snap)
        v2_snap["snapshot_schema_version"] = 2
        restored_v2 = SlotStore.from_snapshot(v2_snap)
        self.assertEqual(restored_v2.slots["equipment_type"].value, "通用工作级深海机器人 250HP")

        # Unsupported schema versions -> Reject fail closed
        unsupported_versions = [0, -1, 3, 999, True, "2", 2.0, [], {}]
        for bad_ver in unsupported_versions:
            with self.subTest(bad_ver=bad_ver):
                bad_snap = copy.deepcopy(base_snap)
                bad_snap["snapshot_schema_version"] = bad_ver
                self.assert_restore_failure_is_atomic(self.store, bad_snap)

    # 2. Value / Value Type Compatibility Matrix
    def test_value_type_compatibility_matrix(self):
        valid_cases = [
            ("string", "test_val"),
            ("number", 100),
            ("number", 99.5),
            ("boolean", True),
            ("boolean", False),
            ("list", ["a", "b"]),
            ("object", {"k": "v"}),
            ("coord", {"lat": 25.0, "lon": 120.0}),
            ("datetime", "2026-08-10T12:00:00Z"),
        ]
        for val_type, val in valid_cases:
            with self.subTest(type=val_type, value=val):
                snap = {
                    "snapshot_schema_version": 2,
                    "store_version": 1,
                    "slots": {
                        "test_slot": {
                            "slot_name": "test_slot",
                            "value": val,
                            "status": "valid",
                            "value_type": val_type,
                        }
                    },
                }
                st = SlotStore.from_snapshot(snap)
                self.assertEqual(st.slots["test_slot"].value, val)

        invalid_cases = [
            ("number", True),
            ("number", "100"),
            ("number", {"a": 1}),
            ("number", [1, 2]),
            ("number", float("nan")),
            ("number", float("inf")),
            ("boolean", 1),
            ("boolean", "true"),
            ("boolean", {}),
            ("list", "a"),
            ("list", {}),
            ("object", [1, 2]),
            ("object", "str"),
            ("string", 123),
            ("string", {}),
            ("coord", "25.0, 120.0"),
            ("coord", {"lat": "25.0", "lon": 120.0}),
            ("coord", {"lat": 25.0}),  # missing lon
            ("coord", {"lat": 100.0, "lon": 120.0}),  # lat out of range
            ("datetime", 123456789),
            ("datetime", "not_a_date"),
        ]
        for val_type, bad_val in invalid_cases:
            with self.subTest(type=val_type, bad_value=bad_val):
                bad_snap = {
                    "snapshot_schema_version": 2,
                    "store_version": 1,
                    "slots": {
                        "test_slot": {
                            "slot_name": "test_slot",
                            "value": bad_val,
                            "status": "valid",
                            "value_type": val_type,
                        }
                    },
                }
                self.assert_restore_failure_is_atomic(self.store, bad_snap)

    # 3. Representation Parity (Dict vs Slot Object)
    def test_representation_parity(self):
        invalid_slot_payloads = [
            ("valid_null_value", {"status": "valid", "value": None, "value_type": "string"}),
            ("invalid_status", {"status": "unknown_status", "value": "x", "value_type": "string"}),
            ("negative_version", {"status": "valid", "value": "x", "value_type": "string", "version": -1}),
            ("bool_version", {"status": "valid", "value": "x", "value_type": "string", "version": True}),
            ("bad_source", {"status": "valid", "value": "x", "value_type": "string", "source": 123}),
            ("bool_confidence", {"status": "valid", "value": "x", "value_type": "string", "confidence": True}),
            ("nan_confidence", {"status": "valid", "value": "x", "value_type": "string", "confidence": float("nan")}),
            ("inf_confidence", {"status": "valid", "value": "x", "value_type": "string", "confidence": float("inf")}),
            ("confidence_out_low", {"status": "valid", "value": "x", "value_type": "string", "confidence": -0.1}),
            ("confidence_out_high", {"status": "valid", "value": "x", "value_type": "string", "confidence": 1.1}),
            ("invalid_updated_at", {"status": "valid", "value": "x", "value_type": "string", "updated_at": "bad_dt"}),
            ("key_name_mismatch", {"slot_name": "wrong_key", "status": "valid", "value": "x", "value_type": "string"}),
            ("number_dict_mismatch", {"status": "valid", "value": {"a": 1}, "value_type": "number"}),
            ("boolean_string_mismatch", {"status": "valid", "value": "true", "value_type": "boolean"}),
        ]

        for name, payload in invalid_slot_payloads:
            # Test Dict representation
            with self.subTest(name=name, repr="dict"):
                dict_snap = {
                    "snapshot_schema_version": 2,
                    "store_version": 1,
                    "slots": {"target_slot": copy.deepcopy(payload)},
                }
                self.assert_restore_failure_is_atomic(self.store, dict_snap)

            # Test Slot object representation
            with self.subTest(name=name, repr="Slot"):
                slot_kwargs = {"slot_name": payload.get("slot_name", "target_slot")}
                for field in ("value", "value_type", "status", "source", "confidence", "updated_at", "version"):
                    if field in payload:
                        slot_kwargs[field] = payload[field]

                slot_obj = Slot(**slot_kwargs)
                slot_snap = {
                    "snapshot_schema_version": 2,
                    "store_version": 1,
                    "slots": {"target_slot": slot_obj},
                }

                before_slot_dict = {
                    "slot_name": slot_obj.slot_name,
                    "value": slot_obj.value,
                    "value_type": slot_obj.value_type,
                    "status": slot_obj.status,
                    "source": slot_obj.source,
                    "confidence": slot_obj.confidence,
                    "updated_at": slot_obj.updated_at,
                    "version": slot_obj.version,
                }
                before_store = copy.deepcopy(self.store.export_snapshot())

                with self.assertRaises(SnapshotValidationError):
                    self.store.restore_snapshot(slot_snap)

                # Store zero mutation
                self.assertEqual(self.store.export_snapshot(), before_store)
                # Slot object input purity check
                after_slot_dict = {
                    "slot_name": slot_obj.slot_name,
                    "value": slot_obj.value,
                    "value_type": slot_obj.value_type,
                    "status": slot_obj.status,
                    "source": slot_obj.source,
                    "confidence": slot_obj.confidence,
                    "updated_at": slot_obj.updated_at,
                    "version": slot_obj.version,
                }
                self.assertEqual(after_slot_dict, before_slot_dict)

    # 4. Canonical V2 Round-trip Test
    def test_canonical_v2_round_trip(self):
        # Set up a complex store with candidate, conflict, raw_value, validation_error, etc.
        self.store.slots["task_type"] = Slot(slot_name="task_type", value="tree_valve_operation", status="valid", value_type="string")
        self.store.slots["water_depth"] = Slot(
            slot_name="water_depth",
            value=500.0,
            value_type="number",
            status="conflict",
            candidate_value=600.0,
            raw_value="500米",
            confidence=0.95,
            validation_error="Water depth conflict with seabed model",
            updated_at="2026-08-10T12:00:00Z",
            version=3,
        )

        exported1 = self.store.export_snapshot()
        self.assertEqual(exported1["snapshot_schema_version"], 2)

        restored_store = SlotStore.from_snapshot(exported1)
        exported2 = restored_store.export_snapshot()

        self.assertEqual(exported1, exported2)

    # 5. Legacy V1 Migration Compatibility
    def test_legacy_v1_robot_specification_migration(self):
        # Case B: Legal legacy specification migrates to canonical equipment_type
        legacy_snap = {
            "store_version": 1,
            "slots": {
                "equipment_class": {"slot_name": "equipment_class", "value": "work_class_rov", "status": "valid", "value_type": "string"},
                "equipment_family": {"slot_name": "equipment_family", "value": "通用工作级深海机器人", "status": "valid", "value_type": "string"},
                "equipment_specification": {
                    "slot_name": "equipment_specification",
                    "value": {
                        "type": "power_hp",
                        "value": 250,
                        "unit": "HP",
                        "display_value": "250HP",
                        "variant_id": "general_work_class_rov_250hp",
                    },
                    "status": "valid",
                    "value_type": "object",
                },
            },
        }

        st = SlotStore.from_snapshot(legacy_snap)
        self.assertEqual(st.slots["equipment_type"].value, "通用工作级深海机器人 250HP")
        self.assertEqual(st.slots["equipment_type"].status, "valid")
        self.assertNotIn("equipment_specification", st.slots)

    # 6. Legacy Robot Case C Fail Closed
    def test_legacy_v1_malformed_specification_fail_closed(self):
        legacy_snap_malformed = {
            "store_version": 1,
            "slots": {
                "equipment_class": {"slot_name": "equipment_class", "value": "work_class_rov", "status": "valid", "value_type": "string"},
                "equipment_specification": {
                    "slot_name": "equipment_specification",
                    "value": True,  # Bool value fails validation even with legal variant_id!
                    "candidate_value": {"variant_id": "general_work_class_rov_250hp"},
                    "status": "valid",
                    "value_type": "object",
                },
            },
        }
        self.assert_restore_failure_is_atomic(self.store, legacy_snap_malformed)


if __name__ == "__main__":
    unittest.main()
