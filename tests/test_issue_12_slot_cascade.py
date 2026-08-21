"""
test_issue_12_slot_cascade.py — Issue #12 test suite for canonical 4-level equipment slots and dependency invalidation.
"""

import copy
import math
import unittest
from unittest.mock import MagicMock

from src.dialogue_manager import DialogueManager
from src.knowledge_retriever import KnowledgeBase, RobotSelectionDataError
from src.llm_client import LLMClient
from src.slot_store import (
    ROBOT_CASCADE_DEPENDENCIES,
    Slot,
    SlotStore,
    SlotVersionConflict,
    SnapshotValidationError,
    invalidate_robot_cascade_dependents,
)


class TestIssue12SlotCascade(unittest.TestCase):
    def setUp(self):
        self.kb = KnowledgeBase()
        self.llm = MagicMock(spec=LLMClient)
        self.llm.generate.return_value = "null"
        self.dm = DialogueManager(self.llm, self.kb)
        schema = self.kb.get_task_schema("tree_valve_operation")
        req = schema.get("required_fields", []) if isinstance(schema, dict) else []
        opt = schema.get("optional_fields", []) if isinstance(schema, dict) else []
        self.dm.slot_store.init_task_slots(req + opt)

    def _apply_updates(self, updates, allow_overwrite=True, task_type_key=None):
        if not isinstance(updates, dict):
            raise TypeError("test helper requires explicit structured updates")

        slots = self.dm.slot_store.clone_slots()

        if task_type_key:
            slots["task_type_key"].value = task_type_key
            slots["task_type_key"].status = "valid"
        self.dm._apply_updates_in_transaction(updates, slots, allow_overwrite=allow_overwrite)
        curr_tt = slots.get("task_type_key").value if slots.get("task_type_key") else task_type_key
        self.dm._normalize_and_validate_in_transaction(slots, curr_tt)
        self.dm.slot_store.commit_transaction(slots, [])
        self.dm.task_state = self.dm.slot_store.get_task_state()

    # ──────────────────────────────────────────────────────────────────────────
    # A. Slot 基础契约 (1-4)
    # ──────────────────────────────────────────────────────────────────────────

    def test_01_store_initializes_with_canonical_slots(self):
        """1. 新建 SlotStore 自动包含 equipment_class 和 equipment_type 槽位，不含 equipment_specification。"""
        store = SlotStore(kb=self.kb)
        self.assertIn("equipment_class", store.slots)
        self.assertIn("equipment_type", store.slots)
        self.assertNotIn("equipment_specification", store.slots)

    def test_02_equipment_class_value_type_is_string(self):
        """2. equipment_class value_type 为 string。"""
        store = SlotStore(kb=self.kb)
        self.assertEqual(store.slots["equipment_class"].value_type, "string")

    def test_03_equipment_type_value_type_is_string(self):
        """3. equipment_type value_type 为 string。"""
        store = SlotStore(kb=self.kb)
        self.assertEqual(store.slots["equipment_type"].value_type, "string")

    def test_04_initial_canonical_slots_are_missing(self):
        """4. 初始状态均为 missing 且 value 为 None。"""
        store = SlotStore(kb=self.kb)
        self.assertEqual(store.slots["equipment_class"].status, "missing")
        self.assertIsNone(store.slots["equipment_class"].value)
        self.assertEqual(store.slots["equipment_type"].status, "missing")
        self.assertIsNone(store.slots["equipment_type"].value)

    # ──────────────────────────────────────────────────────────────────────────
    # B. Legacy Snapshot 兼容 (5-8)
    # ──────────────────────────────────────────────────────────────────────────

    def test_05_legacy_snapshot_without_canonical_slots_can_be_restored(self):
        """5. 旧 snapshot 可恢复；已淘汰型号降级为待重新选择。"""
        legacy_snapshot = {
            "store_version": 2,
            "slots": {
                "equipment_family": {
                    "slot_name": "equipment_family",
                    "value": "观察级深海机器人",
                    "value_type": "string",
                    "status": "valid",
                    "source": "user_input",
                    "version": 1,
                },
                "equipment_type": {
                    "slot_name": "equipment_type",
                    "value": "观察级水下机器人 100HP",
                    "value_type": "string",
                    "status": "valid",
                    "source": "user_input",
                    "version": 1,
                },
            },
            "unresolved": [],
        }
        store = SlotStore(kb=self.kb)
        store.restore_snapshot(legacy_snapshot)
        self.assertEqual(store.version, 2)
        self.assertEqual(store.slots["equipment_family"].value, "观察级深海机器人")
        self.assertEqual(store.slots["equipment_type"].status, "missing")
        self.assertIsNone(store.slots["equipment_type"].value)
        self.assertEqual(store.slots["equipment_type"].source, "snapshot_migration")

    def test_05b_v1_stale_type_only_downgrades_to_missing(self):
        """V1 只有已淘汰 Variant 时可安全降级，不要求不存在的父级 lineage。"""
        snapshot = {
            "store_version": 2,
            "slots": {
                "task_type_key": Slot(
                    "task_type_key",
                    value="pipeline_inspection",
                    status="valid",
                ).to_dict(),
                "equipment_type": Slot(
                    "equipment_type",
                    value="已淘汰的旧型号",
                    status="valid",
                    source="snapshot",
                ).to_dict(),
            },
            "unresolved": [],
        }

        store = SlotStore(kb=self.kb)
        store.restore_snapshot(snapshot)

        self.assertEqual(store.slots["equipment_type"].status, "missing")
        self.assertIsNone(store.slots["equipment_type"].value)
        self.assertEqual(
            store.slots["equipment_type"].source,
            "snapshot_migration",
        )
        for key in (
            "equipment_class",
            "equipment_family",
            "equipment_unit_id",
        ):
            self.assertEqual(store.slots[key].status, "missing")
            self.assertIsNone(store.slots[key].value)

    def test_06_restored_legacy_snapshot_derives_canonical_ancestors(self):
        """6. 恢复旧 snapshot 后从型号反推并补入唯一父级。"""
        legacy_snapshot = {
            "store_version": 1,
            "slots": {
                "equipment_type": {
                    "slot_name": "equipment_type",
                    "value": "通用工作级深海机器人 250HP",
                    "value_type": "string",
                    "status": "valid",
                }
            },
        }
        store = SlotStore(kb=self.kb)
        store.restore_snapshot(legacy_snapshot)
        self.assertIn("equipment_class", store.slots)
        self.assertEqual(store.slots["equipment_class"].value, "work_class_rov")
        self.assertEqual(store.slots["equipment_class"].status, "valid")
        self.assertEqual(
            store.slots["equipment_family"].value,
            "通用工作级深海机器人",
        )
        self.assertEqual(
            store.slots["equipment_class"].source,
            "snapshot_migration",
        )
        self.assertEqual(
            store.slots["equipment_family"].source,
            "snapshot_migration",
        )
        self.assertNotIn("equipment_specification", store.slots)

    def test_07_legacy_valid_slots_unaffected(self):
        """7. 补全新槽位时不影响旧 snapshot 已有的 valid 字段值。"""
        legacy_snapshot = {
            "store_version": 3,
            "slots": {
                "equipment_name": {
                    "slot_name": "equipment_name",
                    "value": "WROV #1",
                    "value_type": "string",
                    "status": "valid",
                    "version": 2,
                }
            },
        }
        store = SlotStore(kb=self.kb)
        store.restore_snapshot(legacy_snapshot)
        self.assertEqual(store.slots["equipment_name"].value, "WROV #1")
        self.assertEqual(store.slots["equipment_name"].status, "valid")
        self.assertEqual(store.slots["equipment_name"].version, 2)

    def test_08_legacy_versions_preserved(self):
        """8. store_version 和旧 slot version 不会被无故改变或增加。"""
        legacy_snapshot = {
            "store_version": 5,
            "slots": {
                "equipment_family": {
                    "slot_name": "equipment_family",
                    "value": "通用工作级深海机器人",
                    "value_type": "string",
                    "status": "valid",
                    "version": 3,
                }
            },
        }
        store = SlotStore(kb=self.kb)
        store.restore_snapshot(legacy_snapshot)
        self.assertEqual(store.version, 5)
        self.assertEqual(store.slots["equipment_family"].version, 3)

    # ──────────────────────────────────────────────────────────────────────────
    # C. Specification Snapshot 迁移与 Fail-Closed 校验 (9-16)
    # ──────────────────────────────────────────────────────────────────────────

    def test_09_valid_power_hp_specification_restores_successfully(self):
        """9. Case B: 合法 power_hp specification 格式迁移派生 equipment_type。"""
        valid_spec = {
            "type": "power_hp",
            "value": 250,
            "unit": "hp",
            "display_value": "250HP",
            "variant_id": "general_work_class_rov_250hp",
        }
        snapshot = {
            "store_version": 1,
            "slots": {
                "equipment_class": {"slot_name": "equipment_class", "value": "work_class_rov", "status": "valid"},
                "equipment_family": {"slot_name": "equipment_family", "value": "通用工作级深海机器人", "status": "valid"},
                "equipment_specification": {
                    "slot_name": "equipment_specification",
                    "value": valid_spec,
                    "value_type": "object",
                    "status": "valid",
                },
            },
        }
        store = SlotStore(kb=self.kb)
        store.restore_snapshot(snapshot)
        self.assertNotIn("equipment_specification", store.slots)
        self.assertEqual(store.slots["equipment_type"].value, "通用工作级深海机器人 250HP")
        self.assertEqual(store.slots["equipment_type"].status, "valid")

    def test_10_valid_diameter_mm_specification_restores_successfully(self):
        """10. Case B: 合法 diameter_mm specification 格式迁移派生 equipment_type。"""
        valid_spec = {
            "type": "diameter_mm",
            "value": 324,
            "unit": "mm",
            "display_value": "324CC",
            "variant_id": "autonomous_underwater_vehicle_324cc",
        }
        snapshot = {
            "store_version": 1,
            "slots": {
                "equipment_class": {"slot_name": "equipment_class", "value": "auv", "status": "valid"},
                "equipment_family": {"slot_name": "equipment_family", "value": "水下无人自主航行器", "status": "valid"},
                "equipment_specification": {
                    "slot_name": "equipment_specification",
                    "value": valid_spec,
                    "value_type": "object",
                    "status": "valid",
                },
            },
        }
        store = SlotStore(kb=self.kb)
        store.restore_snapshot(snapshot)
        self.assertNotIn("equipment_specification", store.slots)
        self.assertEqual(store.slots["equipment_type"].value, "水下无人自主航行器 324CC")
        self.assertEqual(store.slots["equipment_type"].status, "valid")

    def test_11_non_dict_specification_fail_closed(self):
        """11. Case C: 非 dict 类型的 legacy specification 触发 SnapshotValidationError 整体 fail closed。"""
        snapshot = {
            "store_version": 1,
            "slots": {
                "equipment_class": {"slot_name": "equipment_class", "value": "work_class_rov", "status": "valid", "value_type": "string"},
                "equipment_family": {"slot_name": "equipment_family", "value": "通用工作级深海机器人", "status": "valid", "value_type": "string"},
                "equipment_specification": {
                    "slot_name": "equipment_specification",
                    "value": "250HP_string",
                    "value_type": "object",
                    "status": "valid",
                },
            },
        }
        store = SlotStore(kb=self.kb)
        with self.assertRaises(SnapshotValidationError):
            store.restore_snapshot(snapshot)

    def test_12_missing_required_field_in_specification_fail_closed(self):
        """12. Case C: 缺少 required field (即使 variant_id 合法) 的 specification 触发 SnapshotValidationError 整体 fail closed。"""
        incomplete_spec = {
            "type": "power_hp",
            "value": 250,
            "variant_id": "general_work_class_rov_250hp",
        }
        snapshot = {
            "store_version": 1,
            "slots": {
                "equipment_class": {"slot_name": "equipment_class", "value": "work_class_rov", "status": "valid", "value_type": "string"},
                "equipment_family": {"slot_name": "equipment_family", "value": "通用工作级深海机器人", "status": "valid", "value_type": "string"},
                "equipment_specification": {
                    "slot_name": "equipment_specification",
                    "value": incomplete_spec,
                    "value_type": "object",
                    "status": "valid",
                },
            },
        }
        store = SlotStore(kb=self.kb)
        with self.assertRaises(SnapshotValidationError):
            store.restore_snapshot(snapshot)

    def test_13_bool_specification_value_fail_closed(self):
        """13. Case C: value 为 bool (即使 variant_id 与 hierarchy 完全合法) 触发 SnapshotValidationError 整体 fail closed。"""
        bad_spec = {
            "type": "power_hp",
            "value": True,
            "unit": "hp",
            "display_value": "250HP",
            "variant_id": "general_work_class_rov_250hp",
        }
        snapshot = {
            "store_version": 1,
            "slots": {
                "equipment_class": {"slot_name": "equipment_class", "value": "work_class_rov", "status": "valid", "value_type": "string"},
                "equipment_family": {"slot_name": "equipment_family", "value": "通用工作级深海机器人", "status": "valid", "value_type": "string"},
                "equipment_specification": {
                    "slot_name": "equipment_specification",
                    "value": bad_spec,
                    "value_type": "object",
                    "status": "valid",
                },
            },
        }
        store = SlotStore(kb=self.kb)
        with self.assertRaises(SnapshotValidationError):
            store.restore_snapshot(snapshot)

    def test_14_non_finite_specification_value_fail_closed(self):
        """14. Case C: NaN 或 Infinity 值的 specification (即使 variant_id 合法) 触发 SnapshotValidationError 整体 fail closed。"""
        bad_spec = {
            "type": "power_hp",
            "value": float("nan"),
            "unit": "hp",
            "display_value": "250HP",
            "variant_id": "general_work_class_rov_250hp",
        }
        snapshot = {
            "store_version": 1,
            "slots": {
                "equipment_class": {"slot_name": "equipment_class", "value": "work_class_rov", "status": "valid", "value_type": "string"},
                "equipment_family": {"slot_name": "equipment_family", "value": "通用工作级深海机器人", "status": "valid", "value_type": "string"},
                "equipment_specification": {
                    "slot_name": "equipment_specification",
                    "value": bad_spec,
                    "value_type": "object",
                    "status": "valid",
                },
            },
        }
        store = SlotStore(kb=self.kb)
        with self.assertRaises(SnapshotValidationError):
            store.restore_snapshot(snapshot)

    def test_14b_infinity_specification_value_fail_closed(self):
        """14b. Case C: Infinity 值的 specification (即使 variant_id 合法) 触发 SnapshotValidationError 整体 fail closed。"""
        bad_spec = {
            "type": "power_hp",
            "value": float("inf"),
            "unit": "hp",
            "display_value": "250HP",
            "variant_id": "general_work_class_rov_250hp",
        }
        snapshot = {
            "store_version": 1,
            "slots": {
                "equipment_class": {"slot_name": "equipment_class", "value": "work_class_rov", "status": "valid", "value_type": "string"},
                "equipment_family": {"slot_name": "equipment_family", "value": "通用工作级深海机器人", "status": "valid", "value_type": "string"},
                "equipment_specification": {
                    "slot_name": "equipment_specification",
                    "value": bad_spec,
                    "value_type": "object",
                    "status": "valid",
                },
            },
        }
        store = SlotStore(kb=self.kb)
        with self.assertRaises(SnapshotValidationError):
            store.restore_snapshot(snapshot)

    def test_14c_negative_or_zero_specification_value_fail_closed(self):
        """14c. Case C: <=0 值的 specification (即使 variant_id 合法) 触发 SnapshotValidationError 整体 fail closed。"""
        bad_spec = {
            "type": "power_hp",
            "value": -5,
            "unit": "hp",
            "display_value": "250HP",
            "variant_id": "general_work_class_rov_250hp",
        }
        snapshot = {
            "store_version": 1,
            "slots": {
                "equipment_class": {"slot_name": "equipment_class", "value": "work_class_rov", "status": "valid", "value_type": "string"},
                "equipment_family": {"slot_name": "equipment_family", "value": "通用工作级深海机器人", "status": "valid", "value_type": "string"},
                "equipment_specification": {
                    "slot_name": "equipment_specification",
                    "value": bad_spec,
                    "value_type": "object",
                    "status": "valid",
                },
            },
        }
        store = SlotStore(kb=self.kb)
        with self.assertRaises(SnapshotValidationError):
            store.restore_snapshot(snapshot)

    def test_15_mismatched_type_and_unit_fail_closed(self):
        """15. Case C: Mismatched unit/type 或 invalid spec type 触发 SnapshotValidationError 整体 fail closed。"""
        mismatched_spec = {
            "type": "power_hp",
            "value": 250,
            "unit": "mm",
            "display_value": "250HP",
            "variant_id": "general_work_class_rov_250hp",
        }
        snapshot = {
            "store_version": 1,
            "slots": {
                "equipment_class": {"slot_name": "equipment_class", "value": "work_class_rov", "status": "valid", "value_type": "string"},
                "equipment_family": {"slot_name": "equipment_family", "value": "通用工作级深海机器人", "status": "valid", "value_type": "string"},
                "equipment_specification": {
                    "slot_name": "equipment_specification",
                    "value": mismatched_spec,
                    "value_type": "object",
                    "status": "valid",
                },
            },
        }
        store = SlotStore(kb=self.kb)
        with self.assertRaises(SnapshotValidationError):
            store.restore_snapshot(snapshot)

    def test_16_failed_restore_preserves_original_store_state(self):
        """16. 校验失败 (如带有合法 variant_id 的 malformed spec) 时，原 SlotStore 内存快照与版本完全不变。"""
        import copy
        from src.slot_store import SnapshotValidationError
        store = SlotStore(kb=self.kb)
        store.commit_transaction(
            {
                "task_type_key": Slot("task_type_key", value="pipeline_inspection", status="valid"),
                "equipment_class": Slot("equipment_class", value="work_class_rov", status="valid"),
            },
            [],
        )
        before_version = store.version
        before_snapshot = copy.deepcopy(store.export_snapshot())

        malformed_snapshot = copy.deepcopy(before_snapshot)
        # malformed legacy spec dict (lacks top-level 'status', but contains valid variant_id)
        malformed_snapshot["slots"]["equipment_specification"] = {
            "type": "power_hp",
            "value": True,
            "unit": "hp",
            "display_value": "250HP",
            "variant_id": "general_work_class_rov_250hp",
        }

        with self.assertRaises(SnapshotValidationError):
            store.restore_snapshot(malformed_snapshot)

        self.assertEqual(store.version, before_version)
        self.assertEqual(store.export_snapshot(), before_snapshot)

    def test_16b_mixed_robot_hierarchy_restore_is_atomic(self):
        """完整 Unit 存在时，快照中的显式父级必须与 Registry 四级关系一致。"""
        store = SlotStore(kb=self.kb)
        before_snapshot = copy.deepcopy(store.export_snapshot())
        mixed_snapshot = copy.deepcopy(before_snapshot)
        mixed_snapshot["slots"].update(
            {
                "task_type_key": Slot(
                    "task_type_key",
                    value="pipeline_inspection",
                    status="valid",
                ).to_dict(),
                "equipment_class": Slot(
                    "equipment_class",
                    value="auv",
                    status="valid",
                ).to_dict(),
                "equipment_family": Slot(
                    "equipment_family",
                    value="观察级深海机器人",
                    status="valid",
                ).to_dict(),
                "equipment_type": Slot(
                    "equipment_type",
                    value="观察级深海机器人 75HP",
                    status="valid",
                ).to_dict(),
                "equipment_unit_id": Slot(
                    "equipment_unit_id",
                    value="OBSROV-75-001",
                    status="valid",
                ).to_dict(),
            }
        )

        with self.assertRaisesRegex(
            SnapshotValidationError,
            "FAMILY_CLASS_MISMATCH",
        ):
            store.restore_snapshot(mixed_snapshot)

        self.assertEqual(store.export_snapshot(), before_snapshot)

    def test_16bb_flat_legacy_mixed_robot_hierarchy_is_atomic(self):
        """无 slot_store 的旧版顶层 task_state 也必须经过同一四级闸门。"""
        before_snapshot = copy.deepcopy(self.dm.export_snapshot())
        legacy_snapshot = {
            "conversation_history": [],
            "mode": "normal",
            "phase": "collecting",
            "task_state": {
                "task_type_key": "pipeline_inspection",
                "equipment_class": "auv",
                "equipment_family": "观察级深海机器人",
                "equipment_type": "观察级深海机器人 75HP",
                "equipment_unit_id": "OBSROV-75-001",
            },
        }

        with self.assertRaisesRegex(
            SnapshotValidationError,
            "FAMILY_CLASS_MISMATCH",
        ):
            self.dm.load_snapshot(legacy_snapshot)

        self.assertEqual(self.dm.export_snapshot(), before_snapshot)

    def test_16bc_flat_legacy_unit_only_restore_derives_canonical_ancestors(self):
        """旧版顶层 Unit-only 状态经同一 SlotStore 边界反推完整父级。"""
        legacy_snapshot = {
            "conversation_history": [],
            "mode": "normal",
            "phase": "collecting",
            "task_state": {
                "task_type_key": "pipeline_inspection",
                "equipment_unit_id": "OBSROV-75-001",
            },
        }

        self.dm.load_snapshot(legacy_snapshot)

        state = self.dm.task_state
        self.assertEqual(state["equipment_class"], "observation_rov")
        self.assertEqual(state["equipment_family"], "观察级深海机器人")
        self.assertEqual(state["equipment_type"], "观察级深海机器人 75HP")
        self.assertEqual(state["equipment_unit_id"], "OBSROV-75-001")
        for key in (
            "equipment_class",
            "equipment_family",
            "equipment_type",
        ):
            self.assertEqual(
                self.dm.slot_store.slots[key].source,
                "snapshot_migration",
            )

    def test_16c_partial_robot_hierarchy_restore_remains_supported(self):
        """收集中的部分级联没有 Unit 时仍可恢复。"""
        store = SlotStore(kb=self.kb)
        snapshot = copy.deepcopy(store.export_snapshot())
        snapshot["slots"].update(
            {
                "task_type_key": Slot(
                    "task_type_key",
                    value="pipeline_inspection",
                    status="valid",
                ).to_dict(),
                "equipment_class": Slot(
                    "equipment_class",
                    value="auv",
                    status="valid",
                ).to_dict(),
            }
        )

        store.restore_snapshot(snapshot)

        self.assertEqual(store.slots["equipment_class"].value, "auv")
        self.assertIsNone(store.slots["equipment_unit_id"].value)

    def test_16d_restore_rejects_explicit_invalid_robot_selectors(self):
        """valid 状态的空值或错误类型不是“部分选择”，必须原子拒绝。"""
        cases = (
            ("equipment_unit_id", "", "INVALID_UNIT_SELECTOR"),
            ("equipment_unit_id", 123, "INVALID_UNIT_SELECTOR"),
            ("equipment_class", "", "INVALID_ROBOT_CLASS_SELECTOR"),
            ("equipment_family", 0, "INVALID_FAMILY_SELECTOR"),
            ("equipment_type", "", "INVALID_VARIANT_SELECTOR"),
        )

        for field, value, error_code in cases:
            with self.subTest(field=field, value=value):
                store = SlotStore(kb=self.kb)
                before_snapshot = copy.deepcopy(store.export_snapshot())
                snapshot = copy.deepcopy(before_snapshot)
                snapshot["slots"]["task_type_key"] = Slot(
                    "task_type_key",
                    value="pipeline_inspection",
                    status="valid",
                ).to_dict()
                snapshot["slots"][field] = Slot(
                    field,
                    value=value,
                    status="valid",
                ).to_dict()

                with self.assertRaisesRegex(SnapshotValidationError, error_code):
                    store.restore_snapshot(snapshot)

                self.assertEqual(store.export_snapshot(), before_snapshot)

    def test_16e_restore_rejects_mixed_partial_hierarchy_without_unit(self):
        """即使尚未选择 Unit，已提供的 Class/Family/Variant 也必须逐边一致。"""
        store = SlotStore(kb=self.kb)
        before_snapshot = copy.deepcopy(store.export_snapshot())
        snapshot = copy.deepcopy(before_snapshot)
        snapshot["slots"].update(
            {
                "task_type_key": Slot(
                    "task_type_key",
                    value="pipeline_inspection",
                    status="valid",
                ).to_dict(),
                "equipment_class": Slot(
                    "equipment_class",
                    value="auv",
                    status="valid",
                ).to_dict(),
                "equipment_family": Slot(
                    "equipment_family",
                    value="观察级深海机器人",
                    status="valid",
                ).to_dict(),
                "equipment_type": Slot(
                    "equipment_type",
                    value="观察级深海机器人 75HP",
                    status="valid",
                ).to_dict(),
            }
        )

        with self.assertRaisesRegex(
            SnapshotValidationError,
            "FAMILY_CLASS_MISMATCH",
        ):
            store.restore_snapshot(snapshot)

        self.assertEqual(store.export_snapshot(), before_snapshot)

    def test_16f_restore_migrates_legacy_unit_alias_to_canonical_id(self):
        """可唯一解析的旧 Unit alias 在候选快照中迁移为正式 fleet unit_id。"""
        store = SlotStore(kb=self.kb)
        snapshot = copy.deepcopy(store.export_snapshot())
        snapshot["slots"].update(
            {
                "task_type_key": Slot(
                    "task_type_key",
                    value="pipeline_inspection",
                    status="valid",
                ).to_dict(),
                "equipment_unit_id": Slot(
                    "equipment_unit_id",
                    value="OBSROV--001",
                    status="valid",
                    source="snapshot",
                ).to_dict(),
            }
        )

        store.restore_snapshot(snapshot)

        restored = store.slots["equipment_unit_id"]
        self.assertEqual(restored.value, "OBSROV-75-001")
        self.assertEqual(restored.raw_value, "OBSROV--001")
        self.assertEqual(restored.source, "snapshot_migration")

    def test_16g_restore_rejects_broken_validator_result_atomically(self):
        """可调用但返回空/错结构的 Registry validator 也必须失败关闭。"""
        for broken_result in (
            None,
            {},
            {"foo": "bar"},
            {"unit_id": "OBSROV-75-001"},
        ):
            with self.subTest(broken_result=broken_result):
                store = SlotStore(kb=self.kb)
                before_snapshot = copy.deepcopy(store.export_snapshot())
                snapshot = copy.deepcopy(before_snapshot)
                snapshot["slots"].update(
                    {
                        "task_type_key": Slot(
                            "task_type_key",
                            value="pipeline_inspection",
                            status="valid",
                        ).to_dict(),
                        "equipment_unit_id": Slot(
                            "equipment_unit_id",
                            value="OBSROV-75-001",
                            status="valid",
                        ).to_dict(),
                    }
                )
                original = self.kb.validate_robot_selection_from_task_state
                try:
                    self.kb.validate_robot_selection_from_task_state = (
                        lambda *_args, _result=broken_result, **_kwargs: _result
                    )
                    with self.assertRaisesRegex(
                        SnapshotValidationError,
                        "STATIC_ROBOT_VALIDATOR_FAILURE",
                    ):
                        store.restore_snapshot(snapshot)
                finally:
                    self.kb.validate_robot_selection_from_task_state = original

                self.assertEqual(store.export_snapshot(), before_snapshot)

    def test_16ga_broken_helper_cannot_materialize_descendants(self):
        """Registry helper 的多余返回字段不得替用户选择下级机器人。"""
        cases = (
            (
                "equipment_class",
                "observation_rov",
                (),
            ),
            (
                "equipment_family",
                "观察级深海机器人",
                (("equipment_class", "observation_rov"),),
            ),
        )
        complete_lineage = {
            "robot_class": "observation_rov",
            "family_id": "observation_rov",
            "family_name": "观察级深海机器人",
            "variant_id": "observation_rov_75hp",
            "equipment_type": "观察级深海机器人 75HP",
            "unit_id": "OBSROV-75-001",
        }

        for explicit_key, explicit_value, expected_ancestors in cases:
            with self.subTest(explicit_key=explicit_key):
                store = SlotStore(kb=self.kb)
                snapshot = copy.deepcopy(store.export_snapshot())
                snapshot["slots"].update(
                    {
                        "task_type_key": Slot(
                            "task_type_key",
                            value="pipeline_inspection",
                            status="valid",
                        ).to_dict(),
                        explicit_key: Slot(
                            explicit_key,
                            value=explicit_value,
                            status="valid",
                            source="user_input",
                        ).to_dict(),
                    }
                )
                original = self.kb.validate_robot_selection_from_task_state
                try:
                    self.kb.validate_robot_selection_from_task_state = (
                        lambda *_args, **_kwargs: copy.deepcopy(complete_lineage)
                    )
                    store.restore_snapshot(snapshot)
                finally:
                    self.kb.validate_robot_selection_from_task_state = original

                self.assertEqual(store.slots[explicit_key].source, "user_input")
                for ancestor_key, ancestor_value in expected_ancestors:
                    self.assertEqual(store.slots[ancestor_key].value, ancestor_value)
                    self.assertEqual(
                        store.slots[ancestor_key].source,
                        "snapshot_migration",
                    )
                deepest_index = (
                    "equipment_class",
                    "equipment_family",
                    "equipment_type",
                    "equipment_unit_id",
                ).index(explicit_key)
                for descendant_key in (
                    "equipment_class",
                    "equipment_family",
                    "equipment_type",
                    "equipment_unit_id",
                )[deepest_index + 1 :]:
                    self.assertEqual(store.slots[descendant_key].status, "missing")
                    self.assertIsNone(store.slots[descendant_key].value)

    def test_16j_type_only_restore_materializes_canonical_ancestors(self):
        """Variant 可唯一反推 Class/Family，但恢复不向下选 Unit。"""
        store = SlotStore(kb=self.kb)
        snapshot = copy.deepcopy(store.export_snapshot())
        snapshot["slots"].update(
            {
                "task_type_key": Slot(
                    "task_type_key",
                    value="pipeline_inspection",
                    status="valid",
                ).to_dict(),
                "equipment_type": Slot(
                    "equipment_type",
                    value="观察级深海机器人 75HP",
                    status="valid",
                    source="user_input",
                ).to_dict(),
            }
        )

        store.restore_snapshot(snapshot)

        self.assertEqual(store.slots["equipment_class"].value, "observation_rov")
        self.assertEqual(
            store.slots["equipment_family"].value,
            "观察级深海机器人",
        )
        self.assertEqual(
            store.slots["equipment_class"].source,
            "snapshot_migration",
        )
        self.assertEqual(
            store.slots["equipment_family"].source,
            "snapshot_migration",
        )
        self.assertEqual(store.slots["equipment_type"].source, "user_input")
        self.assertIsNone(store.slots["equipment_unit_id"].value)
        self.assertEqual(store.slots["equipment_unit_id"].status, "missing")

    def test_16k_unit_only_restore_materializes_all_canonical_ancestors(self):
        """Unit 可唯一反推 Class/Family/Variant，且保留显式 Unit。"""
        store = SlotStore(kb=self.kb)
        snapshot = copy.deepcopy(store.export_snapshot())
        snapshot["slots"].update(
            {
                "task_type_key": Slot(
                    "task_type_key",
                    value="pipeline_inspection",
                    status="valid",
                ).to_dict(),
                "equipment_unit_id": Slot(
                    "equipment_unit_id",
                    value="OBSROV-75-001",
                    status="valid",
                    source="user_input",
                ).to_dict(),
            }
        )

        store.restore_snapshot(snapshot)

        self.assertEqual(store.slots["equipment_class"].value, "observation_rov")
        self.assertEqual(
            store.slots["equipment_family"].value,
            "观察级深海机器人",
        )
        self.assertEqual(
            store.slots["equipment_type"].value,
            "观察级深海机器人 75HP",
        )
        for key in (
            "equipment_class",
            "equipment_family",
            "equipment_type",
        ):
            self.assertEqual(store.slots[key].source, "snapshot_migration")
        self.assertEqual(store.slots["equipment_unit_id"].value, "OBSROV-75-001")
        self.assertEqual(store.slots["equipment_unit_id"].source, "user_input")

    def test_16l_restore_preserves_nonvalid_ancestor_audit_state(self):
        """反推只补 missing 祖先，不覆盖 candidate/conflict/invalid 审计态。"""
        cases = (
            Slot(
                "equipment_family",
                status="candidate",
                source="user_input",
                raw_value="候选观察级",
                candidate_value="观察级深海机器人",
                version=7,
            ),
            Slot(
                "equipment_family",
                value="旧机器人族",
                status="conflict",
                source="user_input",
                raw_value="换成观察级",
                candidate_value="观察级深海机器人",
                version=7,
            ),
            Slot(
                "equipment_family",
                status="invalid",
                source="user_input",
                raw_value="未知机器人族",
                validation_error="无法规范化机器人族",
                candidate_value="未知机器人族",
                version=7,
            ),
        )

        for family_slot in cases:
            with self.subTest(status=family_slot.status):
                store = SlotStore(kb=self.kb)
                snapshot = copy.deepcopy(store.export_snapshot())
                expected_family = family_slot.to_dict()
                snapshot["slots"].update(
                    {
                        "task_type_key": Slot(
                            "task_type_key",
                            value="pipeline_inspection",
                            status="valid",
                        ).to_dict(),
                        "equipment_family": expected_family,
                        "equipment_unit_id": Slot(
                            "equipment_unit_id",
                            value="OBSROV-75-001",
                            status="valid",
                            source="user_input",
                        ).to_dict(),
                    }
                )

                store.restore_snapshot(snapshot)

                self.assertEqual(
                    store.slots["equipment_family"].to_dict(),
                    expected_family,
                )
                self.assertEqual(
                    store.slots["equipment_class"].value,
                    "observation_rov",
                )
                self.assertEqual(
                    store.slots["equipment_type"].value,
                    "观察级深海机器人 75HP",
                )

    def test_16m_nonvalid_parent_does_not_constrain_valid_unit_on_recollapse(self):
        """非 valid Family 的旧值不属于 task_state，不能误清有效 Type/Unit。"""
        families = (
            Slot(
                "equipment_family",
                value="轻型工作级深海机器人",
                status="conflict",
                source="user_input",
                raw_value="改成观察级",
                candidate_value="观察级深海机器人",
                validation_error="等待用户确认机器人族修改",
                version=7,
            ),
            Slot(
                "equipment_family",
                value="轻型工作级深海机器人",
                status="candidate",
                source="user_input",
                raw_value="候选轻型工作级",
                candidate_value="轻型工作级深海机器人",
                version=7,
            ),
        )

        for nonvalid_family in families:
            with self.subTest(status=nonvalid_family.status):
                store = SlotStore(kb=self.kb)
                snapshot = copy.deepcopy(store.export_snapshot())
                snapshot["slots"].update(
                    {
                        "task_type_key": Slot(
                            "task_type_key",
                            value="pipeline_inspection",
                            status="valid",
                        ).to_dict(),
                        "equipment_family": nonvalid_family.to_dict(),
                        "equipment_unit_id": Slot(
                            "equipment_unit_id",
                            value="OBSROV-75-001",
                            status="valid",
                            source="user_input",
                        ).to_dict(),
                    }
                )
                store.restore_snapshot(snapshot)
                before_family = copy.deepcopy(
                    store.slots["equipment_family"].to_dict()
                )

                slots = store.clone_slots()
                self.dm._normalize_and_validate_in_transaction(
                    slots,
                    "pipeline_inspection",
                )

                self.assertEqual(
                    slots["equipment_family"].to_dict(),
                    before_family,
                )
                self.assertEqual(
                    slots["equipment_type"].value,
                    "观察级深海机器人 75HP",
                )
                self.assertEqual(slots["equipment_type"].status, "valid")
                self.assertEqual(
                    slots["equipment_unit_id"].value,
                    "OBSROV-75-001",
                )
                self.assertEqual(slots["equipment_unit_id"].status, "valid")

                # Cache rebuild is also exercised by unrelated confirmations
                # (for example oilfield confirmation). It may derive a truly
                # missing Family, but must not overwrite audit-bearing states.
                self.dm.slot_store = store
                self.dm._rebuild_cache()
                self.assertEqual(
                    self.dm.slot_store.slots["equipment_family"].to_dict(),
                    before_family,
                )
                self.assertEqual(
                    self.dm.slot_store.slots["equipment_unit_id"].value,
                    "OBSROV-75-001",
                )

    def test_16n_nonvalid_variant_does_not_filter_payload_normalization(self):
        """未确认 Type 不能作为动态 payload 合法值的机器人事实。"""
        variants = (
            Slot(
                "equipment_type",
                value="水下无人自主航行器 324CC",
                status="candidate",
                source="user_input",
                candidate_value="水下无人自主航行器 324CC",
                version=5,
            ),
            Slot(
                "equipment_type",
                value="观察级深海机器人 75HP",
                status="conflict",
                source="user_input",
                candidate_value="水下无人自主航行器 324CC",
                validation_error="等待用户确认型号修改",
                version=5,
            ),
        )

        for nonvalid_variant in variants:
            with self.subTest(status=nonvalid_variant.status):
                store = SlotStore(kb=self.kb)
                store.init_task_slots(
                    self.dm.builder.get_schema(
                        "pipeline_inspection",
                        "normal",
                    )
                )
                snapshot = copy.deepcopy(store.export_snapshot())
                snapshot["slots"].update(
                    {
                        "task_type_key": Slot(
                            "task_type_key",
                            value="pipeline_inspection",
                            status="valid",
                        ).to_dict(),
                        "equipment_type": nonvalid_variant.to_dict(),
                        "equipment_unit_id": Slot(
                            "equipment_unit_id",
                            value="OBSROV-75-001",
                            status="valid",
                            source="user_input",
                        ).to_dict(),
                    }
                )
                store.restore_snapshot(snapshot)
                before_variant = copy.deepcopy(
                    store.slots["equipment_type"].to_dict()
                )

                slots = store.clone_slots()
                self.dm._apply_updates_in_transaction(
                    {"payload": ["TSS管缆跟踪传感器"]},
                    slots,
                    allow_overwrite=True,
                )
                self.dm._normalize_and_validate_in_transaction(
                    slots,
                    "pipeline_inspection",
                )

                self.assertEqual(
                    slots["equipment_type"].to_dict(),
                    before_variant,
                )
                self.assertEqual(slots["payload"].status, "valid")
                self.assertEqual(
                    slots["payload"].value,
                    ["TSS管缆跟踪传感器"],
                )
                self.assertEqual(
                    slots["equipment_unit_id"].value,
                    "OBSROV-75-001",
                )

    def test_16o_task_switch_normalizes_payload_against_target_schema(self):
        """同轮切换任务时，新任务字段必须按目标任务 Schema 归一化。"""
        self.dm.slot_store.init_task_slots(
            self.dm.builder.get_schema("pipeline_inspection", "normal")
        )
        slots = self.dm.slot_store.clone_slots()
        slots["task_type_key"].value = "pipeline_inspection"
        slots["task_type_key"].status = "valid"

        self.dm._apply_updates_in_transaction(
            {
                "task_type_key": "pipeline_burial",
                "payload": ["高压水射流喷冲埋设模块"],
            },
            slots,
            allow_overwrite=True,
        )
        self.dm._normalize_and_validate_in_transaction(
            slots,
            "pipeline_burial",
        )

        self.assertEqual(slots["task_type_key"].value, "pipeline_burial")
        self.assertEqual(slots["task_type_key"].status, "valid")
        self.assertEqual(slots["payload"].status, "valid")
        self.assertEqual(
            slots["payload"].value,
            ["高压水射流喷冲埋设模块"],
        )

    def test_16p_locked_task_switch_keeps_current_schema_for_payload(self):
        """任务编号拒绝切类时，同轮字段仍按当前任务 Schema 独立校验。"""
        self.dm.slot_store.init_task_slots(
            self.dm.builder.get_schema("pipeline_inspection", "normal")
        )
        slots = self.dm.slot_store.clone_slots()
        slots["task_type_key"].value = "pipeline_inspection"
        slots["task_type_key"].status = "valid"
        slots["task_type"].value = "管缆巡检"
        slots["task_type"].status = "valid"
        slots["task_id"].value = "PI-20260813-001"
        slots["task_id"].status = "valid"
        slots["task_id"].source = "auto_reserved"
        slots["payload"].value = ["TSS管缆跟踪传感器"]
        slots["payload"].status = "valid"
        slots["water_depth"].value = 100.0
        slots["water_depth"].status = "valid"
        self.dm._normalize_and_validate_in_transaction(
            slots,
            "pipeline_inspection",
        )

        self.dm._apply_updates_in_transaction(
            {
                "task_type_key": "pipeline_burial",
                "payload": ["高压水射流喷冲埋设模块"],
                "water_depth": 321.0,
            },
            slots,
            allow_overwrite=True,
        )

        self.assertEqual(slots["task_type_key"].value, "pipeline_inspection")
        self.assertEqual(slots["task_type_key"].status, "valid")
        self.assertIn("任务编号已锁定", slots["task_type_key"].validation_error)
        self.assertEqual(slots["task_type"].value, "管缆巡检")
        self.assertEqual(slots["task_id"].value, "PI-20260813-001")
        self.assertEqual(slots["water_depth"].value, 321.0)
        self.assertEqual(slots["water_depth"].status, "candidate")
        self.assertEqual(slots["payload"].value, ["TSS管缆跟踪传感器"])
        self.assertEqual(slots["payload"].status, "conflict")
        self.assertEqual(
            slots["payload"].candidate_value,
            ["高压水射流喷冲埋设模块"],
        )
        self.assertIsNotNone(slots["payload"].validation_error)

        # Production performs the regular second-stage reconciliation after
        # applying updates. It may derive robot slots from the still-valid old
        # facts, but it must not revive the rejected burial payload.
        self.dm._normalize_and_validate_in_transaction(
            slots,
            slots["task_type_key"].value,
        )
        self.assertEqual(slots["task_type_key"].value, "pipeline_inspection")
        self.assertEqual(slots["task_type"].value, "管缆巡检")
        self.assertEqual(slots["task_id"].value, "PI-20260813-001")
        self.assertEqual(slots["water_depth"].value, 321.0)
        self.assertEqual(slots["water_depth"].status, "valid")
        self.assertEqual(slots["payload"].value, ["TSS管缆跟踪传感器"])
        self.assertEqual(slots["payload"].status, "conflict")

    def test_16pa_task_type_key_display_value_uses_target_schema(self):
        """task_type_key 的中文任务值与实际 handler 使用同一解析规则。"""
        self.dm.slot_store.init_task_slots(
            self.dm.builder.get_schema("pipeline_inspection", "normal")
        )
        slots = self.dm.slot_store.clone_slots()
        slots["task_type_key"].value = "pipeline_inspection"
        slots["task_type_key"].status = "valid"

        self.dm._apply_updates_in_transaction(
            {
                "task_type_key": "管缆埋设",
                "payload": ["高压水射流喷冲埋设模块"],
            },
            slots,
            allow_overwrite=True,
        )
        self.dm._normalize_and_validate_in_transaction(
            slots,
            slots["task_type_key"].value,
        )

        self.assertEqual(slots["task_type_key"].value, "pipeline_burial")
        self.assertEqual(slots["task_type"].value, "管缆埋设")
        self.assertEqual(slots["payload"].status, "valid")
        self.assertEqual(
            slots["payload"].value,
            ["高压水射流喷冲埋设模块"],
        )

    def test_16pb_conflicting_task_selectors_reject_turn_atomically(self):
        """同轮 task_type 与 task_type_key 指向不同任务时不得按顺序部分提交。"""
        self.dm.slot_store.init_task_slots(
            self.dm.builder.get_schema("pipeline_inspection", "normal")
        )
        slots = self.dm.slot_store.clone_slots()
        slots["task_type_key"].value = "pipeline_inspection"
        slots["task_type_key"].status = "valid"
        slots["task_type"].value = "管缆巡检"
        slots["task_type"].status = "valid"
        before = {
            key: copy.deepcopy(slot.to_dict())
            for key, slot in slots.items()
        }

        self.dm._apply_updates_in_transaction(
            {
                "task_type": "采油树控制面板插入",
                "task_type_key": "pipeline_burial",
                "payload": ["高压水射流喷冲埋设模块"],
            },
            slots,
            allow_overwrite=True,
        )

        self.assertEqual(slots["task_type_key"].value, "pipeline_inspection")
        self.assertIn("互相冲突", slots["task_type_key"].validation_error)
        for key, slot in slots.items():
            if key == "task_type_key":
                continue
            self.assertEqual(slot.to_dict(), before[key], key)

    def test_16pc_conflicting_concrete_task_values_are_order_independent(self):
        """同一类别的插入/拔出冲突也必须在任何写入前拒绝。"""
        cases = (
            (
                ("task_type", "采油树控制面板插入"),
                ("task_type_key", "采油树控制面板拔出"),
            ),
            (
                ("task_type_key", "采油树控制面板拔出"),
                ("task_type", "采油树控制面板插入"),
            ),
        )
        for ordered_items in cases:
            with self.subTest(order=ordered_items):
                self.dm.slot_store.init_task_slots(
                    self.dm.builder.get_schema("pipeline_inspection", "normal")
                )
                slots = self.dm.slot_store.clone_slots()
                slots["task_type_key"].value = "pipeline_inspection"
                slots["task_type_key"].status = "valid"
                slots["task_type"].value = "管缆巡检"
                slots["task_type"].status = "valid"
                before_task_type = slots["task_type"].to_dict()

                self.dm._apply_updates_in_transaction(
                    dict(ordered_items),
                    slots,
                    allow_overwrite=True,
                )

                self.assertEqual(
                    slots["task_type_key"].value,
                    "pipeline_inspection",
                )
                self.assertIn(
                    "具体任务类型互相冲突",
                    slots["task_type_key"].validation_error,
                )
                self.assertEqual(
                    slots["task_type"].to_dict(),
                    before_task_type,
                )

    def test_16q_list_mutation_ignores_nonvalid_variant_context(self):
        """列表增量的 fallback 也只能使用 valid 机器人型号。"""
        store = SlotStore(kb=self.kb)
        required_schema = self.dm.builder.get_schema(
            "pipeline_inspection",
            "normal",
        )
        store.init_task_slots(required_schema)
        slots = store.clone_slots()
        slots["task_type_key"].value = "pipeline_inspection"
        slots["task_type_key"].status = "valid"
        variants = (
            Slot(
                "equipment_type",
                value="水下无人自主航行器 324CC",
                status="candidate",
                source="user_input",
                candidate_value="水下无人自主航行器 324CC",
            ),
            Slot(
                "equipment_type",
                value="水下无人自主航行器 324CC",
                status="conflict",
                source="user_input",
                candidate_value="观察级深海机器人 75HP",
                validation_error="等待用户确认型号修改",
            ),
        )

        for variant in variants:
            with self.subTest(status=variant.status):
                case_slots = copy.deepcopy(slots)
                case_slots["equipment_type"] = copy.deepcopy(variant)
                before_variant = case_slots["equipment_type"].to_dict()

                result = store.apply_list_mutation(
                    case_slots,
                    {
                        "field": "payload",
                        "operation": "add",
                        "items": ["TSS管缆跟踪传感器"],
                        "raw_text": "添加TSS管缆跟踪传感器",
                        "confidence": 1.0,
                        "source": "user_input",
                    },
                    required_schema=required_schema,
                    payload_catalog=self.kb.assets.get("payload_catalog", {}),
                )

                self.assertTrue(result["success"])
                self.assertTrue(result["changed"])
                self.assertEqual(case_slots["payload"].status, "candidate")
                self.assertEqual(
                    case_slots["payload"].value,
                    ["TSS管缆跟踪传感器"],
                )
                self.assertIsNone(case_slots["payload"].validation_error)
                self.assertEqual(
                    case_slots["equipment_type"].to_dict(),
                    before_variant,
                )

    def test_16r_candidate_task_type_cannot_authorize_robot_selection(self):
        """未确认的任务类型不构成 capability/registry 选型上下文。"""
        slots = self.dm.slot_store.clone_slots()
        slots["task_type_key"].value = "pipeline_inspection"
        slots["task_type_key"].status = "candidate"

        self.dm._apply_updates_in_transaction(
            {"equipment_unit_id": "OBSROV-75-001"},
            slots,
            allow_overwrite=True,
        )

        self.assertEqual(slots["task_type_key"].status, "candidate")
        for key in (
            "equipment_class",
            "equipment_family",
            "equipment_type",
            "equipment_unit_id",
        ):
            self.assertNotEqual(slots[key].status, "valid", key)

    def test_16s_oilfield_linker_ignores_nonvalid_coordinate_context(self):
        """候选/冲突坐标不得参与油田实体规范化打分。"""
        for status in ("candidate", "conflict", "invalid"):
            with self.subTest(status=status):
                slots = self.dm.slot_store.clone_slots()
                slots["start_point"] = Slot(
                    "start_point",
                    value={"lat": 19.5, "lon": 115.5},
                    status=status,
                    candidate_value={"lat": 20.0, "lon": 116.0},
                    validation_error=(
                        "等待用户确认坐标" if status != "candidate" else None
                    ),
                )
                self.dm.oilfield_linker.link = MagicMock(
                    wraps=self.dm.oilfield_linker.link,
                )

                self.dm._link_oilfield_update_in_transaction(
                    {"oilfield_name": "流花"},
                    slots,
                )

                _, coords = self.dm.oilfield_linker.link.call_args.args
                self.assertIsNone(coords)

    def test_16h_restore_rejects_fuzzy_unit_substrings_atomically(self):
        """只有显式 Registry alias 可迁移，模糊 Unit 片段必须拒绝。"""
        for selector in ("OBS", "75", "OBSROV-75"):
            with self.subTest(selector=selector):
                store = SlotStore(kb=self.kb)
                before_snapshot = copy.deepcopy(store.export_snapshot())
                snapshot = copy.deepcopy(before_snapshot)
                snapshot["slots"].update(
                    {
                        "task_type_key": Slot(
                            "task_type_key",
                            value="pipeline_inspection",
                            status="valid",
                        ).to_dict(),
                        "equipment_unit_id": Slot(
                            "equipment_unit_id",
                            value=selector,
                            status="valid",
                        ).to_dict(),
                    }
                )

                with self.assertRaisesRegex(
                    SnapshotValidationError,
                    "UNIT_NOT_FOUND",
                ):
                    store.restore_snapshot(snapshot)
                self.assertEqual(store.export_snapshot(), before_snapshot)

    def test_16i_v1_variant_fallback_revalidates_helper_result(self):
        """V1 清退旧 Variant 后的第二次 helper 调用也必须校验返回契约。"""
        for broken_result in (None, {}, {"foo": "bar"}):
            with self.subTest(broken_result=broken_result):
                store = SlotStore(kb=self.kb)
                before_snapshot = copy.deepcopy(store.export_snapshot())
                snapshot = copy.deepcopy(before_snapshot)
                snapshot.pop("snapshot_schema_version", None)
                snapshot["slots"].update(
                    {
                        "task_type_key": Slot(
                            "task_type_key",
                            value="pipeline_inspection",
                            status="valid",
                        ).to_dict(),
                        "equipment_class": Slot(
                            "equipment_class",
                            value="observation_rov",
                            status="valid",
                        ).to_dict(),
                        "equipment_type": Slot(
                            "equipment_type",
                            value="已淘汰的旧型号",
                            status="valid",
                        ).to_dict(),
                    }
                )
                call_count = 0
                original = self.kb.validate_robot_selection_from_task_state

                def stateful_validator(*_args, **_kwargs):
                    nonlocal call_count
                    call_count += 1
                    if call_count == 1:
                        raise RobotSelectionDataError(
                            "legacy variant is stale",
                            error_code="VARIANT_NOT_FOUND",
                        )
                    return broken_result

                try:
                    self.kb.validate_robot_selection_from_task_state = stateful_validator
                    with self.assertRaisesRegex(
                        SnapshotValidationError,
                        "STATIC_ROBOT_VALIDATOR_FAILURE",
                    ):
                        store.restore_snapshot(snapshot)
                finally:
                    self.kb.validate_robot_selection_from_task_state = original

                self.assertEqual(store.export_snapshot(), before_snapshot)

    def test_17_class_change_clears_all_downstream_slots(self):
        """17. 修改 equipment_class 时，旧 AUV family/type/unit 不得泄漏，新 domain 唯一项重新自动收敛，不唯一的保持 missing。"""
        self._apply_updates(
            {
                "equipment_class": "auv",
                "equipment_family": "水下无人自主航行器",
                "equipment_type": "水下无人自主航行器 324CC",
            },
            task_type_key="pipeline_inspection",
        )
        self._apply_updates({"equipment_unit_id": "AUV-324cc-001"}, task_type_key="pipeline_inspection")
        slots = self.dm.slot_store.slots
        self.assertEqual(slots["equipment_class"].value, "auv")
        self.assertEqual(slots["equipment_family"].value, "水下无人自主航行器")
        self.assertEqual(slots["equipment_unit_id"].value, "AUV-324cc-001")

        # 修改为 observation_rov
        self._apply_updates({"equipment_class": "observation_rov"}, allow_overwrite=True, task_type_key="pipeline_inspection")
        slots = self.dm.slot_store.slots

        # 1. 旧 AUV 事实完全不存在
        self.assertNotEqual(slots["equipment_family"].value, "水下无人自主航行器")
        self.assertNotEqual(slots["equipment_type"].value, "水下无人自主航行器 324CC")
        self.assertNotEqual(slots["equipment_unit_id"].value, "AUV-324cc-001")

        # 2. observation_rov 下有 2 个 families (>1) -> 所有下级必须保持 missing
        self.assertEqual(slots["equipment_class"].value, "observation_rov")
        self.assertEqual(slots["equipment_family"].status, "missing")
        self.assertIsNone(slots["equipment_family"].value)
        self.assertEqual(slots["equipment_type"].status, "missing")
        self.assertIsNone(slots["equipment_type"].value)
        self.assertEqual(slots["equipment_unit_id"].status, "missing")
        self.assertIsNone(slots["equipment_unit_id"].value)

    def test_18_family_change_clears_type_unit_name(self):
        """18. family 合法变化时清空 unit_id。"""
        self._apply_updates({"equipment_family": "水下无人自主航行器"}, task_type_key="pipeline_inspection")
        self.assertEqual(self.dm.slot_store.slots["equipment_family"].value, "水下无人自主航行器")

        self._apply_updates({"equipment_family": "轻型工作级深海机器人"}, task_type_key="pipeline_inspection")
        slots = self.dm.slot_store.slots
        self.assertEqual(slots["equipment_family"].value, "轻型工作级深海机器人")
        self.assertEqual(slots["equipment_unit_id"].status, "missing")

    def test_19_equipment_type_change_clears_unit_and_name(self):
        """19. equipment_type 合法变化时清空 unit_id 和 name。"""
        self._apply_updates(
            {"equipment_unit_id": "WROV-250-001"},
            task_type_key="tree_valve_operation",
        )
        self.assertEqual(self.dm.slot_store.slots["equipment_unit_id"].value, "WROV-250-001")

        self._apply_updates(
            {"equipment_type": "通用工作级深海机器人 250HP"},
            task_type_key="tree_valve_operation",
        )
        slots = self.dm.slot_store.slots
        self.assertEqual(slots["equipment_type"].value, "通用工作级深海机器人 250HP")

    def test_19b_equipment_type_change_clears_payload(self):
        """19b. payload 依赖 equipment_type；型号变化后必须重新选择工具。"""
        new_slots = {
            "equipment_type": Slot(
                "equipment_type",
                value="轻型工作级深海机器人 150HP",
                status="valid",
            ),
            "payload": Slot(
                "payload",
                value=["激光标尺"],
                value_type="list",
                status="valid",
            ),
            "equipment_unit_id": Slot(
                "equipment_unit_id",
                value="LROV-150-001",
                status="valid",
            ),
            "equipment_name": Slot(
                "equipment_name",
                value="LROV-150-001",
                status="valid",
            ),
        }

        invalidate_robot_cascade_dependents(new_slots, ["equipment_type"])

        self.assertEqual(new_slots["payload"].status, "missing")
        self.assertEqual(new_slots["payload"].value, [])
        self.assertEqual(
            new_slots["payload"].source,
            "system_dependency_invalidation",
        )

    def test_19b2_equipment_family_change_clears_payload_transitively(self):
        """19b2. 系列变化会使型号变化；payload 必须随型号传递失效。"""
        new_slots = {
            "equipment_family": Slot(
                "equipment_family",
                value="轻型工作级深海机器人",
                status="valid",
            ),
            "equipment_type": Slot(
                "equipment_type",
                value="轻型工作级深海机器人 150HP",
                status="valid",
            ),
            "payload": Slot(
                "payload",
                value=["TSS管缆跟踪传感器"],
                value_type="list",
                status="valid",
            ),
        }

        invalidate_robot_cascade_dependents(new_slots, ["equipment_family"])

        self.assertEqual(new_slots["equipment_type"].status, "missing")
        self.assertEqual(new_slots["payload"].status, "missing")
        self.assertEqual(new_slots["payload"].value, [])

    def test_19c_equipment_type_switch_clears_committed_payload(self):
        """19c. 已选工具后切换不同 equipment_type，payload 回到待填。"""
        self._apply_updates(
            {
                "equipment_type": "轻型工作级深海机器人 150HP",
                "payload": ["激光标尺"],
            },
            task_type_key="pipeline_inspection",
        )
        self.assertEqual(self.dm.slot_store.slots["payload"].status, "valid")
        self.assertEqual(self.dm.slot_store.slots["payload"].value, ["激光标尺"])

        self._apply_updates(
            {"equipment_type": "观察级深海机器人 75HP"},
            task_type_key="pipeline_inspection",
        )

        slots = self.dm.slot_store.slots
        self.assertEqual(slots["equipment_type"].value, "观察级深海机器人 75HP")
        self.assertEqual(slots["payload"].status, "missing")
        self.assertEqual(slots["payload"].value, [])

    def test_19d_same_turn_equipment_type_and_payload_keeps_new_payload(self):
        """19d. 同轮选择新型号和新工具时，不清掉本轮显式 payload。"""
        self._apply_updates(
            {
                "equipment_type": "轻型工作级深海机器人 150HP",
                "payload": ["激光标尺"],
            },
            task_type_key="pipeline_inspection",
        )
        self._apply_updates(
            {
                "equipment_type": "观察级深海机器人 75HP",
                "payload": ["腐蚀检测探头"],
            },
            task_type_key="pipeline_inspection",
        )

        slots = self.dm.slot_store.slots
        self.assertEqual(slots["equipment_type"].value, "观察级深海机器人 75HP")
        self.assertEqual(slots["payload"].status, "valid")
        self.assertEqual(slots["payload"].value, ["腐蚀检测探头"])

    def test_20_same_parent_value_recommitted_does_not_clear_downstream(self):
        """20. 相同 parent 值重复提交不清空下级有效槽位。"""
        self._apply_updates(
            {"equipment_unit_id": "WROV-250-001"},
            task_type_key="tree_valve_operation",
        )
        unit_before = self.dm.slot_store.slots["equipment_unit_id"].value

        self._apply_updates(
            {"equipment_family": "通用工作级深海机器人"},
            task_type_key="tree_valve_operation",
        )
        self.assertEqual(self.dm.slot_store.slots["equipment_unit_id"].value, unit_before)

    def test_21_unknown_class_does_not_clear_existing_cascade(self):
        """21. 未知 class 输入标记 invalid/conflict，不清空原有的已确认级联。"""
        self._apply_updates(
            {"equipment_unit_id": "WROV-250-001"},
            task_type_key="tree_valve_operation",
        )
        fam_before = self.dm.slot_store.slots["equipment_family"].value

        self._apply_updates(
            {"equipment_class": "未知太空飞船"},
            task_type_key="tree_valve_operation",
        )
        self.assertIn(self.dm.slot_store.slots["equipment_class"].status, ("invalid", "conflict"))
        self.assertEqual(self.dm.slot_store.slots["equipment_family"].value, fam_before)

    def test_22_unknown_family_does_not_clear_existing_cascade(self):
        """22. 未知 family 输入标记 invalid/conflict，不清空原有的已确认级联。"""
        self._apply_updates(
            {"equipment_unit_id": "WROV-250-001"},
            task_type_key="tree_valve_operation",
        )
        unit_before = self.dm.slot_store.slots["equipment_unit_id"].value

        self._apply_updates(
            {"equipment_family": "未知系列"},
            task_type_key="tree_valve_operation",
        )
        self.assertIn(self.dm.slot_store.slots["equipment_family"].status, ("invalid", "conflict"))
        self.assertEqual(self.dm.slot_store.slots["equipment_unit_id"].value, unit_before)

    def test_23_conflict_parent_does_not_prematurely_clear_downstream(self):
        """23. allow_overwrite=False 时，父级处于 conflict 状态，不提前清空下级有效槽位。"""
        self._apply_updates({"equipment_family": "观察级深海机器人"}, task_type_key="pipeline_inspection")
        fam_before = self.dm.slot_store.slots["equipment_family"].value

        self._apply_updates({"equipment_family": "轻型工作级深海机器人"}, allow_overwrite=False, task_type_key="pipeline_inspection")
        fam_slot = self.dm.slot_store.slots["equipment_family"]
        self.assertEqual(fam_slot.status, "conflict")
        self.assertEqual(fam_slot.value, fam_before)

    def test_24_explicitly_confirmed_parent_conflict_triggers_invalidation(self):
        """24. 显式确认父级修改 (allow_overwrite=True) 后才执行下级清空。"""
        self._apply_updates({"equipment_family": "水下无人自主航行器"}, task_type_key="pipeline_inspection")

        self._apply_updates({"equipment_family": "轻型工作级深海机器人"}, allow_overwrite=False, task_type_key="pipeline_inspection")
        self.assertEqual(self.dm.slot_store.slots["equipment_family"].status, "conflict")

        self._apply_updates({"equipment_family": "轻型工作级深海机器人"}, allow_overwrite=True, task_type_key="pipeline_inspection")
        self.assertEqual(self.dm.slot_store.slots["equipment_family"].value, "轻型工作级深海机器人")
        self.assertEqual(self.dm.slot_store.slots["equipment_unit_id"].status, "missing")

    # ──────────────────────────────────────────────────────────────────────────
    # E. 同轮多层更新 (25-29)
    # ──────────────────────────────────────────────────────────────────────────

    def test_25_same_turn_class_and_family_update_preserves_family(self):
        """25. 同轮合法提供 class + family 时，family 不会被 class 清理逻辑误删。"""
        self._apply_updates({"equipment_class": "auv", "equipment_family": "水下无人自主航行器"}, task_type_key="pipeline_inspection")
        slots = self.dm.slot_store.slots
        self.assertEqual(slots["equipment_class"].value, "auv")
        self.assertEqual(slots["equipment_family"].value, "水下无人自主航行器")

    def test_26_same_turn_class_family_type_preserves_all_three(self):
        """26. 同轮合法提供 class + family + equipment_type 时全部保留。"""
        self._apply_updates({"equipment_class": "auv", "equipment_family": "水下无人自主航行器", "equipment_type": "水下无人自主航行器 324CC"}, task_type_key="pipeline_inspection")
        slots = self.dm.slot_store.slots
        self.assertEqual(slots["equipment_class"].value, "auv")
        self.assertEqual(slots["equipment_family"].value, "水下无人自主航行器")
        self.assertEqual(slots["equipment_type"].value, "水下无人自主航行器 324CC")

    def test_27_same_turn_full_four_level_cascade_all_preserved(self):
        """27. 同轮提供完整四级组合时，通过后端校验后全部保留。"""
        self._apply_updates({
            "equipment_class": "auv",
            "equipment_family": "水下无人自主航行器",
            "equipment_type": "水下无人自主航行器 324CC",
            "equipment_unit_id": "AUV-324cc-001",
        }, task_type_key="pipeline_inspection")
        slots = self.dm.slot_store.slots
        self.assertEqual(slots["equipment_class"].value, "auv")
        self.assertEqual(slots["equipment_family"].value, "水下无人自主航行器")
        self.assertEqual(slots["equipment_type"].value, "水下无人自主航行器 324CC")
        self.assertEqual(slots["equipment_unit_id"].value, "AUV-324cc-001")

    def test_28_inconsistent_same_turn_combination_rejects_child(self):
        """28. 同轮 class/family 组合不合法时原子拒绝，不得产生半提交状态。"""
        updates = {
            "equipment_class": "auv",
            "equipment_family": "通用工作级深海机器人",  # belongs to work_class_rov
        }
        self._apply_updates(updates, task_type_key="pipeline_inspection")
        slots = self.dm.slot_store.slots
        self.assertIsNone(slots["equipment_class"].value)
        self.assertEqual(slots["equipment_class"].status, "missing")
        self.assertEqual(slots["equipment_family"].status, "invalid")
        self.assertEqual(
            slots["equipment_family"].candidate_value,
            "通用工作级深海机器人",
        )

    def test_29_no_hybrid_new_class_and_old_family_state(self):
        """29. 不得出现新 class + 旧 family/type/unit 的混合残余提交。"""
        self._apply_updates(
            {"equipment_unit_id": "WROV-250-001"},
            task_type_key="tree_valve_operation",
        )
        self._apply_updates({"equipment_class": "cable_burial_robot"}, task_type_key="pipeline_burial")
        slots = self.dm.slot_store.slots
        self.assertEqual(slots["equipment_class"].value, "cable_burial_robot")
        self.assertEqual(slots["equipment_family"].status, "missing")

    # ──────────────────────────────────────────────────────────────────────────
    # F. 旧字段与反填 (30-35)
    # ──────────────────────────────────────────────────────────────────────────

    def test_30_legacy_equipment_type_populates_canonical_slots(self):
        """30. 提交 equipment_type 时，能够自动补全 canonical equipment_class 与 equipment_family。"""
        self._apply_updates(
            {"equipment_type": "通用工作级深海机器人 250HP"},
            task_type_key="tree_valve_operation",
        )
        slots = self.dm.slot_store.slots
        self.assertEqual(slots["equipment_class"].value, "work_class_rov")
        self.assertEqual(slots["equipment_family"].value, "通用工作级深海机器人")
        self.assertEqual(slots["equipment_type"].value, "通用工作级深海机器人 250HP")

    def test_31_auv_equipment_type_populates_family(self):
        """31. AUV equipment_type 自动补全 family 及 class。"""
        self._apply_updates(
            {"equipment_type": "水下无人自主航行器 324CC"},
            task_type_key="pipeline_inspection",
        )
        slots = self.dm.slot_store.slots
        self.assertEqual(slots["equipment_class"].value, "auv")
        self.assertEqual(slots["equipment_family"].value, "水下无人自主航行器")
        self.assertEqual(slots["equipment_type"].value, "水下无人自主航行器 324CC")

    def test_32_non_auv_equipment_type_populates_family(self):
        """32. 非 AUV equipment_type 自动补全 family 及 class。"""
        self._apply_updates(
            {"equipment_type": "通用工作级深海机器人 250HP"},
            task_type_key="tree_valve_operation",
        )
        slots = self.dm.slot_store.slots
        self.assertEqual(slots["equipment_class"].value, "work_class_rov")
        self.assertEqual(slots["equipment_family"].value, "通用工作级深海机器人")

    def test_33_equipment_type_change_clears_old_unit_id(self):
        """33. equipment_type 变化继续触发清空旧 unit_id 与 equipment_name。"""
        self._apply_updates(
            {"equipment_unit_id": "WROV-250-001"},
            task_type_key="tree_valve_operation",
        )
        self.assertEqual(self.dm.slot_store.slots["equipment_unit_id"].value, "WROV-250-001")

        self._apply_updates({"equipment_type": "轻型工作级深海机器人 150HP"}, task_type_key="pipeline_inspection")
        self.assertEqual(self.dm.slot_store.slots["equipment_unit_id"].status, "missing")

    def test_33b_unknown_equipment_type_preserves_old_unit_atomically(self):
        """未注册 Variant 必须保留旧级联，而不是为清 Unit 而破坏已确认状态。"""
        self._apply_updates(
            {"equipment_unit_id": "WROV-250-001"},
            task_type_key="tree_valve_operation",
        )

        self._apply_updates(
            {"equipment_type": "轻型工作级深海机器人 600MSW"},
            task_type_key="tree_valve_operation",
        )

        slots = self.dm.slot_store.slots
        self.assertEqual(slots["equipment_type"].status, "conflict")
        self.assertEqual(slots["equipment_type"].value, "通用工作级深海机器人 250HP")
        self.assertEqual(slots["equipment_unit_id"].value, "WROV-250-001")

    def test_34_direct_unit_input_populates_full_canonical_cascade(self):
        """34. 现有 unit 输入成功后反填完整 canonical 级联 (class, family, type, unit_id, name)。"""
        self._apply_updates(
            {"equipment_unit_id": "WROV-250-001"},
            task_type_key="tree_valve_operation",
        )
        slots = self.dm.slot_store.slots
        self.assertEqual(slots["equipment_class"].value, "work_class_rov")
        self.assertEqual(slots["equipment_family"].value, "通用工作级深海机器人")
        self.assertEqual(slots["equipment_type"].value, "通用工作级深海机器人 250HP")
        self.assertEqual(slots["equipment_unit_id"].value, "WROV-250-001")


    # ──────────────────────────────────────────────────────────────────────────
    # G. 原子性与事务性 (36-40)
    # ──────────────────────────────────────────────────────────────────────────

    def test_36_transaction_rollback_preserves_all_slots_on_error(self):
        """36. 事务提交失败时，所有新增 canonical slots 和旧兼容 slots 均回滚。"""
        store = SlotStore(kb=self.kb)
        store.commit_transaction(
            {"equipment_class": Slot("equipment_class", value="auv", status="valid")},
            [],
            request_id="req_initial",
        )
        initial_ver = store.version
        initial_class = store.slots["equipment_class"].value

        with self.assertRaises(SlotVersionConflict):
            store.commit_transaction(
                {"equipment_class": Slot("equipment_class", value="work_class_rov", status="valid")},
                [],
                request_id="req_conflict",
                expected_version=initial_ver - 1,
            )

        self.assertEqual(store.version, initial_ver)
        self.assertEqual(store.slots["equipment_class"].value, initial_class)

    def test_37_slot_version_conflict_leaves_no_half_cleaned_state(self):
        """37. SlotVersionConflict 冲突发生时不得留下半清理状态。"""
        store = SlotStore(kb=self.kb)
        store.commit_transaction(
            {
                "equipment_class": Slot("equipment_class", value="work_class_rov", status="valid"),
                "equipment_family": Slot("equipment_family", value="通用工作级深海机器人", status="valid"),
            },
            [],
            request_id="req_base",
        )

        slots_before = store.get_slot_snapshot()
        with self.assertRaises(SlotVersionConflict):
            store.commit_transaction(
                {"equipment_class": Slot("equipment_class", value="auv", status="valid")},
                [],
                request_id="req_err",
                expected_version=999,
            )

        self.assertEqual(store.get_slot_snapshot(), slots_before)

    def test_38_single_user_request_uses_single_commit_transaction(self):
        """38. 一次用户请求在 DialogueManager 中仍然只有一次 commit_transaction()。"""
        ver_before = self.dm.slot_store.version
        slots = self.dm.slot_store.clone_slots()
        self.dm._apply_updates_in_transaction({"task_type_key": "tree_valve_operation"}, slots, allow_overwrite=True)
        self.dm.slot_store.commit_transaction(slots, [])
        ver_after = self.dm.slot_store.version
        self.assertEqual(ver_after - ver_before, 1)

    def test_39_slot_version_only_increments_for_changed_slots(self):
        """39. 槽位版本只对真实发生变化的槽位递增。"""
        store = SlotStore(kb=self.kb)
        store.commit_transaction(
            {
                "equipment_class": Slot("equipment_class", value="auv", status="valid", version=1),
                "equipment_family": Slot("equipment_family", value="水下无人自主航行器", status="valid", version=1),
            },
            [],
        )
        class_ver_before = store.slots["equipment_class"].version
        fam_ver_before = store.slots["equipment_family"].version

        new_slots = store.clone_slots()
        new_slots["equipment_family"].value = "新的航行器"
        store.commit_transaction(new_slots, [])

        self.assertEqual(store.slots["equipment_class"].version, class_ver_before)
        self.assertEqual(store.slots["equipment_family"].version, fam_ver_before + 1)

    def test_40_cleared_downstream_slot_version_increments_and_updated_at_refreshed(self):
        """40. 清理的下级槽位 version 正确增加，且 updated_at 刷新。"""
        store = SlotStore(kb=self.kb)
        store.commit_transaction(
            {
                "equipment_class": Slot("equipment_class", value="work_class_rov", status="valid", version=1),
                "equipment_family": Slot("equipment_family", value="通用工作级深海机器人", status="valid", version=1),
            },
            [],
        )
        fam_ver_before = store.slots["equipment_family"].version
        fam_updated_before = store.slots["equipment_family"].updated_at

        new_slots = store.clone_slots()
        invalidate_robot_cascade_dependents(new_slots, ["equipment_class"])
        store.commit_transaction(new_slots, [])

        self.assertEqual(store.slots["equipment_family"].status, "missing")
        self.assertEqual(store.slots["equipment_family"].version, fam_ver_before + 1)
        self.assertNotEqual(store.slots["equipment_family"].updated_at, fam_updated_before)


class TestIssue12P1AuthoritativeValidation(unittest.TestCase):
    """Tests 41-52: P1 authority-validation closure for 4-level cascade."""

    def setUp(self):
        self.kb = KnowledgeBase()
        self.llm = MagicMock(spec=LLMClient)
        self.llm.generate.return_value = "null"
        self.dm = DialogueManager(self.llm, self.kb)
        schema = self.kb.get_task_schema("pipeline_inspection")
        req = schema.get("required_fields", []) if isinstance(schema, dict) else []
        opt = schema.get("optional_fields", []) if isinstance(schema, dict) else []
        self.dm.slot_store.init_task_slots(req + opt)

    def _apply_updates(self, updates, allow_overwrite=True, task_type_key="pipeline_inspection"):
        new_slots = self.dm.slot_store.clone_slots()
        if task_type_key:
            new_slots["task_type_key"].value = task_type_key
            new_slots["task_type_key"].status = "valid"
        self.dm._apply_updates_in_transaction(
            updates, new_slots, allow_overwrite=allow_overwrite
        )
        self.dm._normalize_and_validate_in_transaction(
            new_slots, task_type_key
        )
        self.dm.slot_store.commit_transaction(new_slots, [])
        self.dm.task_state = self.dm.slot_store.get_task_state()

    def test_41_registry_error_in_variant_path_fail_closed(self):
        """41. KnowledgeBase 抛出 RobotSelectionDataError 时 fail closed。"""
        from unittest.mock import patch

        with patch.object(
            self.kb,
            "resolve_robot_unit",
            return_value=None,
        ):
            self._apply_updates({"equipment_unit_id": "AUV-324cc-001"})

        slots = self.dm.slot_store.slots
        cls_slot = slots.get("equipment_class")
        fam_slot = slots.get("equipment_family")
        self.assertIsNotNone(cls_slot)
        self.assertIsNotNone(fam_slot)
        self.assertNotEqual(cls_slot.status, "valid")
        self.assertNotEqual(fam_slot.status, "valid")

    def test_42_registry_error_in_unit_path_fail_closed(self):
        """42. unit 路径 resolve_robot_unit 抛 RobotSelectionDataError → fail closed。"""
        from unittest.mock import patch

        with patch.object(
            self.kb,
            "resolve_robot_unit",
            return_value=None,
        ):
            self._apply_updates({"equipment_unit_id": "AUV-324cc-001"})

        slots = self.dm.slot_store.slots
        for key in ("equipment_class", "equipment_family", "equipment_type"):
            sl = slots.get(key)
            self.assertIsNotNone(sl, f"{key} must exist in the canonical slot schema")
            self.assertNotEqual(sl.status, "valid")

    def test_43_non_registry_exception_propagates(self):
        """43. resolve_robot_unit 抛出 TypeError（程序 bug）→ 向上传播。"""
        from unittest.mock import patch

        with patch.object(
            self.kb,
            "resolve_robot_unit",
            side_effect=TypeError("unexpected attribute access"),
        ):
            with self.assertRaises(TypeError):
                self._apply_updates({"equipment_unit_id": "AUV-324cc-001"})

    def test_50_validate_static_robot_selection_called_on_complete_four_level(self):
        """50. 输入合法 AUV unit_id 时，四级组合形成后真实调用 validate_static_robot_selection()。"""
        from unittest.mock import patch

        auv_unit_id = "AUV-324cc-001"

        with patch.object(
            self.kb,
            "validate_static_robot_selection",
            wraps=self.kb.validate_static_robot_selection,
        ) as mock_validate:
            self._apply_updates({"equipment_unit_id": auv_unit_id})

        self.assertTrue(mock_validate.called, "validate_static_robot_selection must be called when 4-level combination forms")
        self.assertGreaterEqual(mock_validate.call_count, 1)

        u_slot = self.dm.slot_store.slots.get("equipment_unit_id")
        self.assertIsNotNone(u_slot)
        self.assertIn(u_slot.status, ("valid", "candidate"))
        self.assertEqual(u_slot.value, auv_unit_id)

    def test_51_failed_four_level_validation_leaves_slot_store_intact(self):
        """51. validate_static_robot_selection 失败时，equipment_unit_id 被拒绝标记为 invalid。"""
        from unittest.mock import patch
        from src.knowledge_retriever import RobotSelectionDataError

        auv_unit_id = "AUV-324cc-001"

        with patch.object(
            self.kb,
            "validate_static_robot_selection",
            side_effect=RobotSelectionDataError(
                "Unit selection blocked by static policy",
                error_code="STATIC_SELECTION_BLOCKED",
            ),
        ):
            self._apply_updates({"equipment_unit_id": auv_unit_id})

        u_slot = self.dm.slot_store.slots.get("equipment_unit_id")
        self.assertIsNotNone(u_slot)
        self.assertIn(u_slot.status, ("invalid", "conflict"))
        self.assertNotEqual(u_slot.value, auv_unit_id)
        self.assertIn("STATIC_SELECTION_BLOCKED", u_slot.validation_error or "")

    def test_52_unit_only_input_can_replace_complete_cascade(self):
        """52. tree_valve_operation 中仅输入 unit_id，能反查完整 class/family/type 并替换旧级联。"""
        self._apply_updates({"equipment_unit_id": "WROV-250-001"}, task_type_key="tree_valve_operation")
        cls_init = self.dm.slot_store.slots.get("equipment_class")
        self.assertIsNotNone(cls_init)
        self.assertIn(cls_init.status, ("valid", "candidate"))
        self.assertEqual(cls_init.value, "work_class_rov")

        slots = self.dm.slot_store.slots
        cls_slot = slots.get("equipment_class")
        self.assertIsNotNone(cls_slot)
        self.assertIn(cls_slot.status, ("valid", "candidate"))
        u_slot = slots.get("equipment_unit_id")
        self.assertIsNotNone(u_slot)
        self.assertIn(u_slot.status, ("valid", "candidate"))
        self.assertEqual(u_slot.value, "WROV-250-001")

    def test_57_existing_valid_unit_failed_validator_sets_conflict_status(self):
        """57. 已有 valid unit 时，新 unit 校验失败保持原 unit value，status 置为 conflict。"""
        from unittest.mock import patch
        from src.knowledge_retriever import RobotSelectionDataError
        self._apply_updates({"equipment_unit_id": "AUV-324cc-001"}, task_type_key="pipeline_inspection")
        self.assertEqual(self.dm.slot_store.slots["equipment_unit_id"].status, "valid")

        with patch.object(
            self.kb,
            "validate_static_robot_selection",
            side_effect=RobotSelectionDataError("Mock unit failure", error_code="UNIT_REJECTED"),
        ):
            self._apply_updates({"equipment_unit_id": "WROV-250-001"}, task_type_key="pipeline_inspection", allow_overwrite=False)

        unit_slot = self.dm.slot_store.slots["equipment_unit_id"]
        self.assertEqual(unit_slot.value, "AUV-324cc-001")
        self.assertEqual(unit_slot.status, "conflict")
        self.assertEqual(unit_slot.candidate_value, "WROV-250-001")
        self.assertEqual(unit_slot.validation_error, "Unknown fleet unit 'WROV-250-001'")

    def test_58_changing_class_clears_orphan_equipment_name(self):
        """58. 已有 valid AUV 级联时，切换 class 为 work_class_rov (allow_overwrite=True) 清理旧 AUV name。"""
        self._apply_updates({"equipment_unit_id": "AUV-324cc-001"}, task_type_key="pipeline_inspection")
        self.assertEqual(self.dm.slot_store.slots["equipment_name"].status, "valid")

        self._apply_updates(
            {"equipment_class": "work_class_rov"},
            allow_overwrite=True,
            task_type_key="tree_valve_operation",
        )

        slots = self.dm.slot_store.slots
        self.assertEqual(slots["equipment_class"].value, "work_class_rov")
        self.assertEqual(slots["equipment_name"].status, "missing")

    def test_62_unknown_class_retains_valid_parent_and_marks_conflict(self):
        """62. 已有完整 valid 级联后输入 unknown class 时，旧 class/value 保留并进入 conflict。"""
        self._apply_updates({"equipment_unit_id": "AUV-324cc-001"}, task_type_key="pipeline_inspection")
        slots = self.dm.slot_store.slots
        self.assertEqual(slots["equipment_class"].status, "valid")
        self.assertEqual(slots["equipment_class"].value, "auv")

        self._apply_updates({"equipment_class": "未知飞船"}, task_type_key="pipeline_inspection", allow_overwrite=False)
        slots = self.dm.slot_store.slots
        self.assertEqual(slots["equipment_class"].value, "auv")
        self.assertEqual(slots["equipment_class"].status, "conflict")
        self.assertEqual(slots["equipment_class"].candidate_value, "未知飞船")
        self.assertEqual(slots["equipment_family"].value, "水下无人自主航行器")
        self.assertEqual(slots["equipment_family"].status, "valid")
        self.assertEqual(slots["equipment_unit_id"].value, "AUV-324cc-001")
        self.assertEqual(slots["equipment_unit_id"].status, "valid")

    def test_63_unknown_family_retains_valid_parent_and_marks_conflict(self):
        """63. 已有完整 valid 级联后输入 unknown family 时，旧 family/value 保留并进入 conflict。"""
        self._apply_updates({"equipment_unit_id": "AUV-324cc-001"}, task_type_key="pipeline_inspection")
        self._apply_updates({"equipment_family": "未知机器人系列"}, task_type_key="pipeline_inspection", allow_overwrite=False)
        slots = self.dm.slot_store.slots
        self.assertEqual(slots["equipment_family"].value, "水下无人自主航行器")
        self.assertEqual(slots["equipment_family"].status, "conflict")
        self.assertEqual(slots["equipment_family"].candidate_value, "未知机器人系列")
        self.assertEqual(slots["equipment_class"].value, "auv")
        self.assertEqual(slots["equipment_class"].status, "valid")
        self.assertEqual(slots["equipment_unit_id"].value, "AUV-324cc-001")
        self.assertEqual(slots["equipment_unit_id"].status, "valid")

    def test_65_full_new_cascade_blocks_on_conflict_fence_when_allow_overwrite_false(self):
        """65. 已有 AUV 级联下，同轮提交完整 WROV 级联在 allow_overwrite=False 时被冲突隔离壁障拦截。"""
        self._apply_updates({"equipment_unit_id": "AUV-324cc-001"}, task_type_key="pipeline_inspection")

        wrov_cascade = {
            "equipment_class": "work_class_rov",
            "equipment_family": "通用工作级深海机器人",
            "equipment_type": "通用工作级深海机器人 250HP",
            "equipment_unit_id": "WROV-250-001",
        }

        self._apply_updates(wrov_cascade, task_type_key="pipeline_inspection", allow_overwrite=False)
        slots = self.dm.slot_store.slots

        self.assertEqual(slots["equipment_class"].value, "auv")
        self.assertEqual(slots["equipment_class"].status, "conflict")
        self.assertEqual(slots["equipment_class"].candidate_value, "work_class_rov")
        self.assertEqual(slots["equipment_family"].value, "水下无人自主航行器")
        self.assertEqual(slots["equipment_family"].status, "valid")
        self.assertEqual(slots["equipment_unit_id"].value, "AUV-324cc-001")
        self.assertEqual(slots["equipment_unit_id"].status, "valid")

    def test_66_family_variant_mismatch_rollback_prevents_hybrid_state(self):
        """66. 已有 AUV 级联后，同轮 observation family + AUV variant 在 allow_overwrite=True 下触发回滚。"""
        self._apply_updates({"equipment_unit_id": "AUV-324cc-001"}, task_type_key="pipeline_inspection")

        mismatch_update = {
            "equipment_family": "观察级深海机器人",
            "equipment_type": "水下无人自主航行器 324CC",
        }

        self._apply_updates(mismatch_update, task_type_key="pipeline_inspection", allow_overwrite=True)
        slots = self.dm.slot_store.slots

        self.assertEqual(slots["equipment_family"].value, "观察级深海机器人")
        self.assertEqual(slots["equipment_class"].value, "observation_rov")
        self.assertEqual(slots["equipment_type"].status, "invalid")
        self.assertIsNone(slots["equipment_unit_id"].value)


if __name__ == "__main__":
    unittest.main()
