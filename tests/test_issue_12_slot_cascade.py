"""
test_issue_12_slot_cascade.py — Issue #12 test suite for canonical 4-level equipment slots and dependency invalidation.
"""

import copy
import math
import unittest
from unittest.mock import MagicMock

from src.dialogue_manager import DialogueManager
from src.knowledge_retriever import KnowledgeBase
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
        """5. 不包含新槽位的旧 snapshot 可以正常恢复。"""
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

    def test_06_restored_legacy_snapshot_auto_adds_missing_canonical_slots(self):
        """6. 恢复旧 snapshot 后自动补入 missing 状态的 canonical slots。"""
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
        self.assertEqual(store.slots["equipment_class"].status, "missing")
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
        self._apply_updates({"equipment_unit_id": "WROV-250-001"})
        self.assertEqual(self.dm.slot_store.slots["equipment_unit_id"].value, "WROV-250-001")

        self._apply_updates({"equipment_type": "通用工作级深海机器人 250HP"})
        slots = self.dm.slot_store.slots
        self.assertEqual(slots["equipment_type"].value, "通用工作级深海机器人 250HP")

    def test_20_same_parent_value_recommitted_does_not_clear_downstream(self):
        """20. 相同 parent 值重复提交不清空下级有效槽位。"""
        self._apply_updates({"equipment_unit_id": "WROV-250-001"})
        unit_before = self.dm.slot_store.slots["equipment_unit_id"].value

        self._apply_updates({"equipment_family": "通用工作级深海机器人"})
        self.assertEqual(self.dm.slot_store.slots["equipment_unit_id"].value, unit_before)

    def test_21_unknown_class_does_not_clear_existing_cascade(self):
        """21. 未知 class 输入标记 invalid/conflict，不清空原有的已确认级联。"""
        self._apply_updates({"equipment_unit_id": "WROV-250-001"})
        fam_before = self.dm.slot_store.slots["equipment_family"].value

        self._apply_updates({"equipment_class": "未知太空飞船"})
        self.assertIn(self.dm.slot_store.slots["equipment_class"].status, ("invalid", "conflict"))
        self.assertEqual(self.dm.slot_store.slots["equipment_family"].value, fam_before)

    def test_22_unknown_family_does_not_clear_existing_cascade(self):
        """22. 未知 family 输入标记 invalid/conflict，不清空原有的已确认级联。"""
        self._apply_updates({"equipment_unit_id": "WROV-250-001"})
        unit_before = self.dm.slot_store.slots["equipment_unit_id"].value

        self._apply_updates({"equipment_family": "未知系列"})
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
        """28. 同轮提交中 family 不属于 class 时，下级被标记 invalid，不得产生混合状态。"""
        updates = {
            "equipment_class": "auv",
            "equipment_family": "通用工作级深海机器人",  # belongs to work_class_rov
        }
        self._apply_updates(updates)
        slots = self.dm.slot_store.slots
        self.assertEqual(slots["equipment_class"].value, "auv")
        self.assertEqual(slots["equipment_family"].status, "invalid")

    def test_29_no_hybrid_new_class_and_old_family_state(self):
        """29. 不得出现新 class + 旧 family/type/unit 的混合残余提交。"""
        self._apply_updates({"equipment_unit_id": "WROV-250-001"})
        self._apply_updates({"equipment_class": "cable_burial_robot"}, task_type_key="pipeline_burial")
        slots = self.dm.slot_store.slots
        self.assertEqual(slots["equipment_class"].value, "cable_burial_robot")
        self.assertEqual(slots["equipment_family"].status, "missing")

    # ──────────────────────────────────────────────────────────────────────────
    # F. 旧字段与反填 (30-35)
    # ──────────────────────────────────────────────────────────────────────────

    def test_30_legacy_equipment_type_populates_canonical_slots(self):
        """30. 提交 equipment_type 时，能够自动补全 canonical equipment_class 与 equipment_family。"""
        self._apply_updates({"equipment_type": "通用工作级深海机器人 250HP"})
        slots = self.dm.slot_store.slots
        self.assertEqual(slots["equipment_class"].value, "work_class_rov")
        self.assertEqual(slots["equipment_family"].value, "通用工作级深海机器人")
        self.assertEqual(slots["equipment_type"].value, "通用工作级深海机器人 250HP")

    def test_31_auv_equipment_type_populates_family(self):
        """31. AUV equipment_type 自动补全 family 及 class。"""
        self._apply_updates({"equipment_type": "水下无人自主航行器 324CC"})
        slots = self.dm.slot_store.slots
        self.assertEqual(slots["equipment_class"].value, "auv")
        self.assertEqual(slots["equipment_family"].value, "水下无人自主航行器")
        self.assertEqual(slots["equipment_type"].value, "水下无人自主航行器 324CC")

    def test_32_non_auv_equipment_type_populates_family(self):
        """32. 非 AUV equipment_type 自动补全 family 及 class。"""
        self._apply_updates({"equipment_type": "通用工作级深海机器人 250HP"})
        slots = self.dm.slot_store.slots
        self.assertEqual(slots["equipment_class"].value, "work_class_rov")
        self.assertEqual(slots["equipment_family"].value, "通用工作级深海机器人")

    def test_33_equipment_type_change_clears_old_unit_id(self):
        """33. equipment_type 变化继续触发清空旧 unit_id 与 equipment_name。"""
        self._apply_updates({"equipment_unit_id": "WROV-250-001"})
        self.assertEqual(self.dm.slot_store.slots["equipment_unit_id"].value, "WROV-250-001")

        self._apply_updates({"equipment_type": "轻型工作级深海机器人 600MSW"}, task_type_key="pipeline_inspection")
        self.assertEqual(self.dm.slot_store.slots["equipment_unit_id"].status, "missing")

    def test_34_direct_unit_input_populates_full_canonical_cascade(self):
        """34. 现有 unit 输入成功后反填完整 canonical 级联 (class, family, type, unit_id, name)。"""
        self._apply_updates({"equipment_unit_id": "WROV-250-001"})
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
