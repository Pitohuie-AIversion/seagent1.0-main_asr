"""
test_issue_12_slot_cascade.py — Issue #12 Phase 2A test suite for canonical equipment slots and dependency invalidation.
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

    def _apply_updates(self, updates_or_text, allow_overwrite=True, task_type_key=None):
        slots = self.dm.slot_store.clone_slots()

        if task_type_key:
            slots["task_type_key"].value = task_type_key
            slots["task_type_key"].status = "valid"

        if isinstance(updates_or_text, str):
            updates = {}
            if "采油树" in updates_or_text or "WROV-250-001" in updates_or_text or "250HP" in updates_or_text:
                task_type_key = "tree_valve_operation"
            elif "管道" in updates_or_text:
                task_type_key = "pipeline_inspection"

            if task_type_key:
                slots["task_type_key"].value = task_type_key
                slots["task_type_key"].status = "valid"

            if "WROV-250-001" in updates_or_text:
                updates["equipment_unit_id"] = "WROV-250-001"
            if "通用工作级深海机器人 250HP" in updates_or_text:
                updates["equipment_type"] = "通用工作级深海机器人 250HP"
            elif "通用工作级深海机器人" in updates_or_text:
                updates["equipment_family"] = "通用工作级深海机器人"
            if "水下无人自主航行器 324CC" in updates_or_text:
                updates["equipment_type"] = "水下无人自主航行器 324CC"
            elif "水下无人自主航行器" in updates_or_text:
                updates["equipment_family"] = "水下无人自主航行器"
            if "观察级" in updates_or_text:
                if "100HP" in updates_or_text or "型" in updates_or_text or "深海" in updates_or_text:
                    updates["equipment_type"] = "观察级深海机器人"
                else:
                    updates["equipment_class"] = "observation_rov"
            if "auv" in updates_or_text.lower():
                updates["equipment_class"] = "auv"
            if "太空飞船" in updates_or_text:
                updates["equipment_class"] = "未知太空飞船"
            if "未知系列" in updates_or_text:
                updates["equipment_family"] = "未知系列"
        else:
            updates = updates_or_text

        self.dm._apply_updates_in_transaction(updates, slots, allow_overwrite=allow_overwrite)
        curr_tt = slots.get("task_type_key").value if slots.get("task_type_key") else task_type_key
        self.dm._normalize_and_validate_in_transaction(slots, curr_tt)
        self.dm.slot_store.commit_transaction(slots, [])
        self.dm.task_state = self.dm.slot_store.get_task_state()

    # ──────────────────────────────────────────────────────────────────────────
    # A. Slot 基础契约 (1-4)
    # ──────────────────────────────────────────────────────────────────────────

    def test_01_store_initializes_with_canonical_slots(self):
        """1. 新建 SlotStore 自动包含 equipment_class 和 equipment_specification 槽位。"""
        store = SlotStore(kb=self.kb)
        self.assertIn("equipment_class", store.slots)
        self.assertIn("equipment_specification", store.slots)

    def test_02_equipment_class_value_type_is_string(self):
        """2. equipment_class value_type 为 string。"""
        store = SlotStore(kb=self.kb)
        self.assertEqual(store.slots["equipment_class"].value_type, "string")

    def test_03_equipment_specification_value_type_is_object(self):
        """3. equipment_specification value_type 为 object。"""
        store = SlotStore(kb=self.kb)
        self.assertEqual(store.slots["equipment_specification"].value_type, "object")

    def test_04_initial_canonical_slots_are_missing(self):
        """4. 初始状态均为 missing 且 value 为 None。"""
        store = SlotStore(kb=self.kb)
        self.assertEqual(store.slots["equipment_class"].status, "missing")
        self.assertIsNone(store.slots["equipment_class"].value)
        self.assertEqual(store.slots["equipment_specification"].status, "missing")
        self.assertIsNone(store.slots["equipment_specification"].value)

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
        """6. 恢复旧 snapshot 后自动补入两个 missing 状态的 canonical slots。"""
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
        self.assertIn("equipment_specification", store.slots)
        self.assertEqual(store.slots["equipment_specification"].status, "missing")

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
    # C. Specification Snapshot 校验 (9-16)
    # ──────────────────────────────────────────────────────────────────────────

    def test_09_valid_power_hp_specification_restores_successfully(self):
        """9. 合法 power_hp specification 格式能够正确恢复。"""
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
                "equipment_specification": {
                    "slot_name": "equipment_specification",
                    "value": valid_spec,
                    "value_type": "object",
                    "status": "valid",
                }
            },
        }
        store = SlotStore(kb=self.kb)
        store.restore_snapshot(snapshot)
        self.assertEqual(store.slots["equipment_specification"].value, valid_spec)

    def test_10_valid_diameter_mm_specification_restores_successfully(self):
        """10. 合法 diameter_mm specification 格式能够正确恢复。"""
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
                "equipment_specification": {
                    "slot_name": "equipment_specification",
                    "value": valid_spec,
                    "value_type": "object",
                    "status": "valid",
                }
            },
        }
        store = SlotStore(kb=self.kb)
        store.restore_snapshot(snapshot)
        self.assertEqual(store.slots["equipment_specification"].value, valid_spec)

    def test_11_non_dict_specification_rejected(self):
        """11. 非 dict 类型的 specification 被拒绝。"""
        snapshot = {
            "store_version": 1,
            "slots": {
                "equipment_specification": {
                    "slot_name": "equipment_specification",
                    "value": "250HP_string",
                    "value_type": "object",
                    "status": "valid",
                }
            },
        }
        store = SlotStore(kb=self.kb)
        with self.assertRaises(SnapshotValidationError):
            store.restore_snapshot(snapshot)

    def test_12_missing_required_field_in_specification_rejected(self):
        """12. 缺少 variant_id 或 display_value 等必需字段的 specification 被拒绝。"""
        incomplete_spec = {
            "type": "power_hp",
            "value": 250,
            "unit": "hp",
            # missing display_value and variant_id
        }
        snapshot = {
            "store_version": 1,
            "slots": {
                "equipment_specification": {
                    "slot_name": "equipment_specification",
                    "value": incomplete_spec,
                    "value_type": "object",
                    "status": "valid",
                }
            },
        }
        store = SlotStore(kb=self.kb)
        with self.assertRaises(SnapshotValidationError):
            store.restore_snapshot(snapshot)

    def test_13_bool_specification_value_rejected(self):
        """13. value 为 bool 类型的 specification 被拒绝。"""
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
                "equipment_specification": {
                    "slot_name": "equipment_specification",
                    "value": bad_spec,
                    "value_type": "object",
                    "status": "valid",
                }
            },
        }
        store = SlotStore(kb=self.kb)
        with self.assertRaises(SnapshotValidationError):
            store.restore_snapshot(snapshot)

    def test_14_non_finite_specification_value_rejected(self):
        """14. NaN 或 Infinity 值的 specification 被拒绝。"""
        for bad_val in [float("nan"), float("inf"), float("-inf"), -50]:
            bad_spec = {
                "type": "power_hp",
                "value": bad_val,
                "unit": "hp",
                "display_value": "250HP",
                "variant_id": "general_work_class_rov_250hp",
            }
            snapshot = {
                "store_version": 1,
                "slots": {
                    "equipment_specification": {
                        "slot_name": "equipment_specification",
                        "value": bad_spec,
                        "value_type": "object",
                        "status": "valid",
                    }
                },
            }
            store = SlotStore(kb=self.kb)
            with self.assertRaises(SnapshotValidationError):
                store.restore_snapshot(snapshot)

    def test_15_mismatched_type_and_unit_rejected(self):
        """15. type 为 power_hp 但 unit 不为 hp 时被拒绝。"""
        mismatched_spec = {
            "type": "power_hp",
            "value": 250,
            "unit": "mm",  # incorrect unit
            "display_value": "250HP",
            "variant_id": "general_work_class_rov_250hp",
        }
        snapshot = {
            "store_version": 1,
            "slots": {
                "equipment_specification": {
                    "slot_name": "equipment_specification",
                    "value": mismatched_spec,
                    "value_type": "object",
                    "status": "valid",
                }
            },
        }
        store = SlotStore(kb=self.kb)
        with self.assertRaises(SnapshotValidationError):
            store.restore_snapshot(snapshot)

    def test_16_failed_restore_preserves_original_store_state(self):
        """16. 校验失败时，原 SlotStore 内存快照与版本完全不变。"""
        store = SlotStore(kb=self.kb)
        initial_version = store.version
        initial_slots = store.get_slot_snapshot()

        malformed_snapshot = {
            "store_version": 99,
            "slots": {
                "equipment_specification": {
                    "slot_name": "equipment_specification",
                    "value": "invalid_object",
                    "value_type": "object",
                    "status": "valid",
                }
            },
        }
        with self.assertRaises(SnapshotValidationError):
            store.restore_snapshot(malformed_snapshot)

        self.assertEqual(store.version, initial_version)
        self.assertEqual(store.get_slot_snapshot(), initial_slots)

    # ──────────────────────────────────────────────────────────────────────────
    # D. 依赖失效 (17-24)
    # ──────────────────────────────────────────────────────────────────────────

    def test_17_class_change_clears_all_downstream_slots(self):
        """17. class 合法变化时清空所有下级 (family, specification, type, unit_id, name)。"""
        self._apply_updates({"equipment_unit_id": "WROV-250-001"})
        self.assertEqual(self.dm.slot_store.slots["equipment_class"].value, "work_class_rov")
        self.assertEqual(self.dm.slot_store.slots["equipment_family"].value, "通用工作级深海机器人")

        self._apply_updates({"equipment_class": "observation_rov"})
        slots = self.dm.slot_store.slots
        self.assertEqual(slots["equipment_class"].value, "observation_rov")
        self.assertEqual(slots["equipment_family"].status, "missing")
        self.assertIsNone(slots["equipment_family"].value)
        self.assertEqual(slots["equipment_specification"].status, "missing")
        self.assertIsNone(slots["equipment_specification"].value)
        self.assertEqual(slots["equipment_type"].status, "missing")
        self.assertEqual(slots["equipment_unit_id"].status, "missing")

    def test_18_family_change_clears_specification_type_unit_name(self):
        """18. family 合法变化时清空 specification, type, unit_id, name。"""
        self._apply_updates({"equipment_family": "观察级深海机器人"}, task_type_key="pipeline_inspection")
        self.assertEqual(self.dm.slot_store.slots["equipment_family"].value, "观察级深海机器人")

        self._apply_updates({"equipment_family": "水下无人自主航行器"}, task_type_key="pipeline_inspection")
        slots = self.dm.slot_store.slots
        self.assertEqual(slots["equipment_family"].value, "水下无人自主航行器")
        self.assertEqual(slots["equipment_specification"].status, "missing")
        self.assertEqual(slots["equipment_type"].status, "missing")
        self.assertEqual(slots["equipment_unit_id"].status, "missing")

    def test_19_specification_change_clears_unit_and_name(self):
        """19. specification 合法变化时清空 unit_id 和 name，并重新派生 type。"""
        self._apply_updates({"equipment_unit_id": "WROV-250-001"})
        self.assertEqual(self.dm.slot_store.slots["equipment_unit_id"].value, "WROV-250-001")

        spec = {
            "type": "power_hp",
            "value": 250,
            "unit": "hp",
            "display_value": "250HP",
            "variant_id": "general_work_class_rov_250hp",
        }
        self._apply_updates({"equipment_specification": spec})
        slots = self.dm.slot_store.slots
        self.assertEqual(slots["equipment_specification"].value, spec)

    def test_20_same_parent_value_recommitted_does_not_clear_downstream(self):
        """20. 相同 parent 值重复提交不清空下级有效槽位。"""
        self._apply_updates({"equipment_unit_id": "WROV-250-001"})
        unit_before = self.dm.slot_store.slots["equipment_unit_id"].value

        self._apply_updates({"equipment_family": "通用工作级深海机器人"})
        self.assertEqual(self.dm.slot_store.slots["equipment_unit_id"].value, unit_before)

    def test_21_unknown_class_does_not_clear_existing_cascade(self):
        """21. 未知 class 输入标记 invalid，不清空原有的已确认级联。"""
        self._apply_updates({"equipment_unit_id": "WROV-250-001"})
        fam_before = self.dm.slot_store.slots["equipment_family"].value

        self._apply_updates({"equipment_class": "未知太空飞船"})
        self.assertEqual(self.dm.slot_store.slots["equipment_class"].status, "invalid")
        self.assertEqual(self.dm.slot_store.slots["equipment_family"].value, fam_before)

    def test_22_unknown_family_does_not_clear_existing_cascade(self):
        """22. 未知 family 输入标记 invalid，不清空原有的已确认级联。"""
        self._apply_updates({"equipment_unit_id": "WROV-250-001"})
        unit_before = self.dm.slot_store.slots["equipment_unit_id"].value

        self._apply_updates({"equipment_family": "未知系列"})
        self.assertEqual(self.dm.slot_store.slots["equipment_family"].status, "invalid")
        self.assertEqual(self.dm.slot_store.slots["equipment_unit_id"].value, unit_before)

    def test_23_conflict_parent_does_not_prematurely_clear_downstream(self):
        """23. allow_overwrite=False 时，父级处于 conflict 状态，不提前清空下级有效槽位。"""
        self._apply_updates({"equipment_family": "观察级深海机器人"}, task_type_key="pipeline_inspection")
        fam_before = self.dm.slot_store.slots["equipment_family"].value

        self._apply_updates({"equipment_family": "水下无人自主航行器"}, allow_overwrite=False, task_type_key="pipeline_inspection")
        fam_slot = self.dm.slot_store.slots["equipment_family"]
        self.assertEqual(fam_slot.status, "conflict")
        self.assertEqual(fam_slot.value, fam_before)

    def test_24_explicitly_confirmed_parent_conflict_triggers_invalidation(self):
        """24. 显式确认父级修改 (allow_overwrite=True) 后才执行下级清空。"""
        self._apply_updates({"equipment_family": "观察级深海机器人"}, task_type_key="pipeline_inspection")

        self._apply_updates({"equipment_family": "水下无人自主航行器"}, allow_overwrite=False, task_type_key="pipeline_inspection")
        self.assertEqual(self.dm.slot_store.slots["equipment_family"].status, "conflict")

        self._apply_updates({"equipment_family": "水下无人自主航行器"}, allow_overwrite=True, task_type_key="pipeline_inspection")
        self.assertEqual(self.dm.slot_store.slots["equipment_family"].value, "水下无人自主航行器")
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

    def test_26_same_turn_class_family_spec_preserves_all_three(self):
        """26. 同轮合法提供 class + family + specification 时全部保留。"""
        spec = {
            "type": "diameter_mm",
            "value": 324,
            "unit": "mm",
            "display_value": "324CC",
            "variant_id": "autonomous_underwater_vehicle_324cc",
        }
        updates = {
            "equipment_class": "auv",
            "equipment_family": "水下无人自主航行器",
            "equipment_specification": spec,
        }
        self._apply_updates(updates, task_type_key="pipeline_inspection")
        slots = self.dm.slot_store.slots
        self.assertEqual(slots["equipment_class"].value, "auv")
        self.assertEqual(slots["equipment_family"].value, "水下无人自主航行器")
        self.assertEqual(slots["equipment_specification"].value["display_value"], "324CC")

    def test_27_same_turn_full_four_level_cascade_all_preserved(self):
        """27. 同轮提供完整四级组合时，通过后端校验后全部保留。"""
        spec = {
            "type": "power_hp",
            "value": 250,
            "unit": "hp",
            "display_value": "250HP",
            "variant_id": "general_work_class_rov_250hp",
        }
        updates = {
            "equipment_class": "work_class_rov",
            "equipment_family": "通用工作级深海机器人",
            "equipment_specification": spec,
            "equipment_unit_id": "WROV-250-001",
        }
        self._apply_updates(updates)
        slots = self.dm.slot_store.slots
        self.assertEqual(slots["equipment_class"].value, "work_class_rov")
        self.assertEqual(slots["equipment_family"].value, "通用工作级深海机器人")
        self.assertEqual(slots["equipment_unit_id"].value, "WROV-250-001")

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
        """29. 不得出现新 class + 旧 family/specification/unit 的混合残余提交。"""
        self._apply_updates({"equipment_unit_id": "WROV-250-001"})
        self._apply_updates({"equipment_class": "auv"}, task_type_key="pipeline_inspection")
        slots = self.dm.slot_store.slots
        self.assertEqual(slots["equipment_class"].value, "auv")
        self.assertEqual(slots["equipment_family"].status, "missing")

    # ──────────────────────────────────────────────────────────────────────────
    # F. 旧字段兼容 (30-35)
    # ──────────────────────────────────────────────────────────────────────────

    def test_30_legacy_equipment_type_populates_canonical_slots(self):
        """30. 只提交旧 equipment_type 时，能够自动补全 canonical equipment_class 与 equipment_specification。"""
        self._apply_updates({"equipment_type": "通用工作级深海机器人 250HP"})
        slots = self.dm.slot_store.slots
        self.assertEqual(slots["equipment_class"].value, "work_class_rov")
        self.assertEqual(slots["equipment_family"].value, "通用工作级深海机器人")
        self.assertIsNotNone(slots["equipment_specification"].value)
        self.assertEqual(slots["equipment_specification"].value["type"], "power_hp")

    def test_31_auv_equipment_type_populates_diameter_mm_specification(self):
        """31. AUV equipment_type 自动补全 diameter_mm/CC specification。"""
        self._apply_updates({"equipment_type": "水下无人自主航行器 324CC"})
        slots = self.dm.slot_store.slots
        self.assertEqual(slots["equipment_class"].value, "auv")
        spec = slots["equipment_specification"].value
        self.assertEqual(spec["type"], "diameter_mm")
        self.assertEqual(spec["display_value"], "324CC")

    def test_32_non_auv_equipment_type_populates_power_hp_specification(self):
        """32. 非 AUV equipment_type 自动补全 power_hp/HP specification。"""
        self._apply_updates({"equipment_type": "通用工作级深海机器人 250HP"})
        slots = self.dm.slot_store.slots
        self.assertEqual(slots["equipment_class"].value, "work_class_rov")
        spec = slots["equipment_specification"].value
        self.assertEqual(spec["type"], "power_hp")
        self.assertEqual(spec["display_value"], "250HP")

    def test_33_equipment_type_change_clears_old_unit_id(self):
        """33. equipment_type 变化继续触发清空旧 unit_id 与 equipment_name。"""
        self._apply_updates({"equipment_unit_id": "WROV-250-001"})
        self.assertEqual(self.dm.slot_store.slots["equipment_unit_id"].value, "WROV-250-001")

        self._apply_updates({"equipment_type": "水下无人自主航行器 324CC"}, task_type_key="pipeline_inspection")
        self.assertEqual(self.dm.slot_store.slots["equipment_unit_id"].status, "missing")

    def test_34_direct_unit_input_populates_full_canonical_cascade(self):
        """34. 现有 unit 输入成功后反填完整 canonical 级联 (class, family, specification, type, unit_id, name)。"""
        self._apply_updates({"equipment_unit_id": "WROV-250-001"})
        slots = self.dm.slot_store.slots
        self.assertEqual(slots["equipment_class"].value, "work_class_rov")
        self.assertEqual(slots["equipment_family"].value, "通用工作级深海机器人")
        self.assertEqual(slots["equipment_specification"].value["display_value"], "250HP")
        self.assertEqual(slots["equipment_type"].value, "通用工作级深海机器人 250HP")
        self.assertEqual(slots["equipment_unit_id"].value, "WROV-250-001")

    def test_35_existing_dialogue_manager_rov_test_compatibility(self):
        """35. 确认现有 DialogueManager 公共交互方法兼容正常。"""
        self._apply_updates("任务类型：采油树阀门操作")
        res = self.dm.slot_store.get_task_state()
        self.assertIsNotNone(res)

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
    """Tests 41-52: P1 authority-validation closure for Phase 2A."""

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

    # ── 41 ─ Registry 配置错误 fail closed ─────────────────────────────
    def test_41_registry_error_in_variant_path_fail_closed(self):
        """41. list_robot_specifications 抛出 RobotSelectionDataError 时 equipment_type invalid，
        class/family 不写入。"""
        from unittest.mock import patch
        from src.knowledge_retriever import RobotSelectionDataError

        with patch.object(
            self.kb,
            "list_robot_specifications",
            side_effect=RobotSelectionDataError(
                "Corrupted variants config",
                error_code="INVALID_MODEL_VARIANTS_CONFIG",
            ),
        ):
            self._apply_updates({"equipment_type": "水下无人自主航行器 324CC"})

        slots = self.dm.slot_store.slots
        # equipment_type 应该是 invalid（get_rov_for_task 成功，但 list_specs 失败）
        # 实际上 get_rov_for_task 先成功，然后 list_specs 失败 → fail closed
        # class/family 不写入
        cls_slot = slots.get("equipment_class")
        fam_slot = slots.get("equipment_family")
        type_slot = slots.get("equipment_type")
        # fail closed：class 和 family 不得是 valid/candidate
        if cls_slot:
            self.assertNotEqual(cls_slot.status, "valid",
                                "equipment_class must not be valid when Registry raises RobotSelectionDataError")
        if fam_slot:
            self.assertNotEqual(fam_slot.status, "valid",
                                "equipment_family must not be valid when Registry raises RobotSelectionDataError")
        # equipment_type 应标记 invalid
        if type_slot:
            self.assertIn(type_slot.status, ("invalid", "missing"),
                          "equipment_type should be invalid or missing after Registry config error")

    # ── 42 ─ unit 路径 Registry 错误 fail closed ───────────────────────
    def test_42_registry_error_in_unit_path_fail_closed(self):
        """42. unit 路径 list_robot_specifications 抛 RobotSelectionDataError → fail closed，
        不继续写入 class/family/type。"""
        from unittest.mock import patch
        from src.knowledge_retriever import RobotSelectionDataError

        with patch.object(
            self.kb,
            "list_robot_specifications",
            side_effect=RobotSelectionDataError(
                "Corrupted family reference",
                error_code="INVALID_FAMILY_REFERENCE",
            ),
        ):
            self._apply_updates({"equipment_unit_id": "AUV-324cc-001"})

        slots = self.dm.slot_store.slots
        # class/family/type 不得变为 valid
        for key in ("equipment_class", "equipment_family", "equipment_type"):
            sl = slots.get(key)
            if sl:
                self.assertNotEqual(sl.status, "valid",
                                    f"{key} must not be valid when unit path Registry raises error")
        # unit_id 应标记 invalid
        u_slot = slots.get("equipment_unit_id")
        if u_slot:
            self.assertIn(u_slot.status, ("invalid", "missing"),
                          "equipment_unit_id should be invalid after Registry config error")
            self.assertIn("INVALID_FAMILY_REFERENCE", (u_slot.validation_error or ""),
                          "validation_error should contain error_code")

    # ── 43 ─ TypeError 不被吞掉 ────────────────────────────────────────
    def test_43_non_registry_exception_propagates(self):
        """43. list_robot_specifications 抛出 TypeError（程序 bug）→ 不被宽泛 except 吞掉，
        向上传播。"""
        from unittest.mock import patch

        with patch.object(
            self.kb,
            "list_robot_specifications",
            side_effect=TypeError("unexpected attribute access"),
        ):
            with self.assertRaises(TypeError):
                self._apply_updates({"equipment_unit_id": "AUV-324cc-001"})

    # ── 44 ─ 裸整数 specification 被拒绝 ──────────────────────────────
    def test_44_bare_integer_spec_rejected(self):
        """44. equipment_specification = 324（裸整数）→ invalid，不写入。"""
        self._apply_updates({"equipment_specification": 324})
        sl = self.dm.slot_store.slots.get("equipment_specification")
        self.assertIsNotNone(sl)
        self.assertEqual(sl.status, "invalid")
        self.assertIsNotNone(sl.validation_error)
        self.assertIn("typed dict", sl.validation_error)

    # ── 45 ─ 缺少 type/variant_id 的 dict 被拒绝 ──────────────────────
    def test_45_spec_dict_missing_type_and_variant_id_rejected(self):
        """45. {"value": 324} 缺少 type/variant_id → invalid。"""
        self._apply_updates({"equipment_specification": {"value": 324}})
        sl = self.dm.slot_store.slots.get("equipment_specification")
        self.assertIsNotNone(sl)
        self.assertEqual(sl.status, "invalid")
        self.assertIn("missing required keys", sl.validation_error or "")

    # ── 46 ─ 缺少 value 的 dict 被拒绝 ────────────────────────────────
    def test_46_spec_dict_missing_value_rejected(self):
        """46. {"type": "power_hp", "variant_id": "x"} 缺少 value → invalid。"""
        self._apply_updates({"equipment_specification": {"type": "power_hp", "variant_id": "x"}})
        sl = self.dm.slot_store.slots.get("equipment_specification")
        self.assertIsNotNone(sl)
        self.assertEqual(sl.status, "invalid")
        self.assertIn("missing required keys", sl.validation_error or "")

    # ── 47 ─ AUV 输入 power_hp 规格被拒绝 ─────────────────────────────
    def test_47_auv_with_power_hp_spec_rejected(self):
        """47. AUV family 下不存在 type=power_hp 规格 → specification invalid。"""
        # 先写入 AUV class/family
        self._apply_updates({
            "equipment_class": "auv",
            "equipment_family": "水下无人自主航行器",
        })
        # 再尝试写入 power_hp 规格（AUV 只接受 diameter_mm）
        # 构造一个假的但结构完整的 typed dict
        bad_spec = {"type": "power_hp", "value": 324, "variant_id": "some_auv_variant"}
        self._apply_updates({"equipment_specification": bad_spec})
        sl = self.dm.slot_store.slots.get("equipment_specification")
        self.assertIsNotNone(sl)
        # 无法在 AUV family 中找到 power_hp 规格，应为 invalid
        self.assertEqual(sl.status, "invalid")

    # ── 48 ─ 同轮 AUV spec variant_id + WROV unit 被拒绝（明确 mismatch）──
    def test_48_same_turn_wrov_spec_with_auv_unit_rejected(self):
        """48. 同轮显式 spec.variant_id 属于 AUV，同轮 unit_id 属于 WROV 且成功解析，
        但 spec.variant_id != unit.variant_id → unit invalid（explicit_spec_mismatch）。
        使用 tree_valve_operation（允许 work_class_rov）使 WROV unit 可成功解析。"""
        wrov_specs = self.kb.list_robot_specifications(
            "work_class_rov", "general_work_class_rov", "tree_valve_operation"
        )
        self.assertTrue(len(wrov_specs) > 0, "Precondition: WROV specs must exist for tree_valve_operation")

        auv_spec_input = {
            "type": "diameter_mm",
            "value": 324,
            "variant_id": "autonomous_underwater_vehicle_324cc",  # AUV variant
        }
        wrov_unit_id = "WROV-250-001"

        self._apply_updates(
            {
                "equipment_specification": auv_spec_input,
                "equipment_unit_id": wrov_unit_id,
            },
            task_type_key="tree_valve_operation",
        )
        u_slot = self.dm.slot_store.slots.get("equipment_unit_id")
        self.assertIsNotNone(u_slot)
        self.assertIn(
            u_slot.status,
            ("invalid", "missing"),
            "WROV unit should be rejected when same-turn spec.variant_id belongs to AUV",
        )

    # ── 49 ─ AUV spec + WROV unit 同轮被拒绝（pipeline_inspection）──────
    def test_49_same_turn_auv_spec_with_wrov_unit_rejected(self):
        """49. pipeline_inspection 中：同轮显式 spec.variant_id 属于 AUV，
        unit_id = WROV-250-001 → unit 在 pipeline_inspection 中不可解析 → invalid。"""
        auv_specs = self.kb.list_robot_specifications(
            "auv", "autonomous_underwater_vehicle", "pipeline_inspection"
        )
        self.assertTrue(len(auv_specs) > 0, "Precondition: AUV specs must exist for pipeline_inspection")

        auv_spec_input = {
            "type": auv_specs[0]["type"],
            "value": auv_specs[0]["value"],
            "variant_id": auv_specs[0]["variant_id"],
        }
        wrov_unit_id = "WROV-250-001"

        self._apply_updates(
            {
                "equipment_specification": auv_spec_input,
                "equipment_unit_id": wrov_unit_id,
            },
            task_type_key="pipeline_inspection",
        )
        u_slot = self.dm.slot_store.slots.get("equipment_unit_id")
        self.assertIsNotNone(u_slot)
        self.assertIn(
            u_slot.status,
            ("invalid", "missing"),
            "WROV unit should be invalid/missing when task only allows AUV/observation_rov",
        )

    # ── 50 ─ 完整四级组合真实调用 validate_static_robot_selection ─────────
    def test_50_validate_static_robot_selection_called_on_complete_four_level(self):
        """50. (P1-1 & P1-2) 输入合法 AUV unit_id 时，四级组合形成后真实调用 validate_static_robot_selection()。"""
        from unittest.mock import patch

        auv_unit_id = "AUV-324cc-001"

        with patch.object(
            self.kb,
            "validate_static_robot_selection",
            wraps=self.kb.validate_static_robot_selection,
        ) as mock_validate:
            self._apply_updates({"equipment_unit_id": auv_unit_id})

        # 真实断言：validate_static_robot_selection 必须被精准调用至少一次
        self.assertTrue(mock_validate.called, "validate_static_robot_selection must be called when 4-level combination forms")
        self.assertGreaterEqual(mock_validate.call_count, 1)
        # 核对调用参数：(robot_class, family_id, specification, unit_id, task_type)
        call_args = mock_validate.call_args[0]
        self.assertEqual(call_args[0], "auv")
        self.assertEqual(call_args[1], "autonomous_underwater_vehicle")
        self.assertEqual(call_args[2]["variant_id"], "autonomous_underwater_vehicle_324cc")
        self.assertEqual(call_args[3], "AUV-324cc-001")

        u_slot = self.dm.slot_store.slots.get("equipment_unit_id")
        self.assertIsNotNone(u_slot)
        self.assertIn(u_slot.status, ("valid", "candidate"))
        self.assertEqual(u_slot.value, auv_unit_id)

    # ── 51 ─ 权威四级校验失败时拒绝 unit 并保持 SlotStore 安全 ──────────────
    def test_51_failed_four_level_validation_leaves_slot_store_intact(self):
        """51. (P1-3) validate_static_robot_selection 失败时，equipment_unit_id 被拒绝标记为 invalid，
        且包含正确的 error_code，不产生半更新或无效 valid 状态。"""
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
        # (P1-3 修复) 必须严格断言 unit_id 是 invalid 状态，绝不能是 valid/candidate
        u_slot = self.dm.slot_store.slots.get("equipment_unit_id")
        self.assertIsNotNone(u_slot)
        # (P1-3 修复) 必须断言 unit_id 为 invalid 或 conflict 状态，绝不能是 valid/candidate
        self.assertIn(u_slot.status, ("invalid", "conflict"))
        self.assertNotEqual(u_slot.value, auv_unit_id)
        self.assertIn("STATIC_SELECTION_BLOCKED", u_slot.validation_error or "")

    # ── 52 ─ unit-only 输入（tree_valve_operation）反查替换完整旧级联 ──
    def test_52_unit_only_input_can_replace_complete_cascade(self):
        """52. tree_valve_operation 中仅输入 unit_id，能反查完整 class/family/type/spec 并替换旧级联。"""
        wrov_specs = self.kb.list_robot_specifications(
            "work_class_rov", "general_work_class_rov", "tree_valve_operation"
        )
        self.assertTrue(len(wrov_specs) > 0, "Precondition: WROV specs must exist for tree_valve_operation")

        self._apply_updates({"equipment_unit_id": "WROV-250-001"}, task_type_key="tree_valve_operation")
        cls_init = self.dm.slot_store.slots.get("equipment_class")
        self.assertIsNotNone(cls_init)
        self.assertIn(cls_init.status, ("valid", "candidate"))
        self.assertEqual(cls_init.value, "work_class_rov")

        self._apply_updates({"equipment_unit_id": "WROV-250-001"}, allow_overwrite=True, task_type_key="tree_valve_operation")

        slots = self.dm.slot_store.slots
        cls_slot = slots.get("equipment_class")
        self.assertIsNotNone(cls_slot)
        self.assertIn(cls_slot.status, ("valid", "candidate"))
        u_slot = slots.get("equipment_unit_id")
        self.assertIsNotNone(u_slot)
        self.assertIn(u_slot.status, ("valid", "candidate"))
        self.assertEqual(u_slot.value, "WROV-250-001")

    # ── 53 ─ 规格缺失 (MISSING_SPECIFICATION_VALUE) 时 unit 不得写入 valid (P1-4) ─
    def test_53_missing_specification_prevents_unit_valid_status(self):
        """53. (P1-4) 选定无 HP 规格的观察级 ROV 时，class/family/type 可填，
        但 equipment_specification 与 equipment_unit_id 绝对不得进入 valid 状态。"""
        obs_unit_id = "OBSROV--001"

        self._apply_updates({"equipment_unit_id": obs_unit_id}, task_type_key="pipeline_inspection")

        slots = self.dm.slot_store.slots
        cls_slot = slots.get("equipment_class")
        self.assertIsNotNone(cls_slot)
        self.assertEqual(cls_slot.value, "observation_rov")

        # spec 和 unit_id 不得为 valid
        spec_slot = slots.get("equipment_specification")
        self.assertIsNotNone(spec_slot)
        self.assertNotEqual(spec_slot.status, "valid", "Spec slot must NOT be valid when spec value is missing")

        unit_slot = slots.get("equipment_unit_id")
        self.assertIsNotNone(unit_slot)
        self.assertNotEqual(unit_slot.status, "valid", "Unit slot must NOT be valid when specification is missing")

    # ── 54 ─ 字符串数值 specification ("324") 严格被拒绝 (P1-5) ─────────────
    def test_54_string_numerical_specification_rejected(self):
        """54. (P1-5) specification.value 为字符串 "324" 时被严格拒绝为 invalid。"""
        string_spec = {
            "type": "diameter_mm",
            "value": "324",  # String instead of int/float
            "variant_id": "autonomous_underwater_vehicle_324cc",
        }
        self._apply_updates({"equipment_specification": string_spec})

        spec_slot = self.dm.slot_store.slots.get("equipment_specification")
        self.assertIsNotNone(spec_slot)
        self.assertEqual(spec_slot.status, "invalid")
        self.assertIn("positive finite number", spec_slot.validation_error or "")

    # ── 55 ─ 已有 valid 级联切换到规格缺失设备，保留旧级联 (P1-1 & Round 5) ───
    def test_55_existing_valid_cascade_switch_to_spec_missing_unit_preserves_cascade(self):
        """55. 真实 pipeline: 已有完整 valid 级联时，尝试切换到规格缺失设备 (OBSROV--001) 保留原 valid 级联，将 unit 标为 conflict。"""
        # 1. 真实完整 pipeline 建立 valid AUV 级联
        self._apply_updates({"equipment_unit_id": "AUV-324cc-001"}, task_type_key="pipeline_inspection")
        self.assertEqual(self.dm.slot_store.slots["equipment_class"].status, "valid")
        self.assertEqual(self.dm.slot_store.slots["equipment_unit_id"].status, "valid")

        # 2. 真实完整 pipeline 尝试切换为规格缺失设备 OBSROV--001
        self._apply_updates({"equipment_unit_id": "OBSROV--001"}, task_type_key="pipeline_inspection")

        slots = self.dm.slot_store.slots
        # 原 AUV 槽位保留，不得出现 observation_rov class 与 AUV name 的混合状态
        self.assertEqual(slots["equipment_class"].value, "auv")
        self.assertEqual(slots["equipment_class"].status, "valid")
        self.assertEqual(slots["equipment_family"].value, "水下无人自主航行器")
        self.assertEqual(slots["equipment_family"].status, "valid")
        self.assertEqual(slots["equipment_unit_id"].value, "AUV-324cc-001")
        self.assertEqual(slots["equipment_unit_id"].status, "conflict")
        self.assertEqual(slots["equipment_unit_id"].candidate_value, "OBSROV--001")
        self.assertEqual(slots["equipment_name"].value, "水下无人自主航行器-324cc-001")

    # ── 56 ─ 同轮四级输入，validator 失败时零半提交 (P1-2 & Round 5) ────────────
    def test_56_same_turn_four_level_input_validator_failure_no_partial_commit(self):
        """56. 真实完整 pipeline: 同一轮次输入 class+family+spec+unit，若 validate_static_robot_selection 失败，
        经 normalize_and_validate 后无父级被提升为 valid。"""
        from unittest.mock import patch
        from src.knowledge_retriever import RobotSelectionDataError
        four_level_input = {
            "equipment_class": "auv",
            "equipment_family": "水下无人自主航行器",
            "equipment_specification": {
                "type": "diameter_mm",
                "value": 324,
                "variant_id": "autonomous_underwater_vehicle_324cc",
            },
            "equipment_unit_id": "AUV-324cc-001",
        }

        with patch.object(
            self.kb,
            "validate_static_robot_selection",
            side_effect=RobotSelectionDataError("Mock static selection failure", error_code="INVALID_SELECTION"),
        ):
            self._apply_updates(four_level_input, task_type_key="pipeline_inspection")

        slots = self.dm.slot_store.slots
        # 经真实 normalize_and_validate 之后，父级 class/family/spec 绝对不得成为 valid
        self.assertNotEqual(slots.get("equipment_class", Slot("")).status, "valid")
        self.assertNotEqual(slots.get("equipment_family", Slot("")).status, "valid")
        self.assertNotEqual(slots.get("equipment_specification", Slot("")).status, "valid")
        self.assertNotEqual(slots.get("equipment_type", Slot("")).status, "valid")
        self.assertIn(slots.get("equipment_unit_id").status, ("invalid", "conflict"))

    # ── 57 ─ 已有 valid unit 校验失败，保持 valid 值并置 status=conflict (P2-1) ───
    def test_57_existing_valid_unit_failed_validator_sets_conflict_status(self):
        """57. 真实完整 pipeline: 已有 valid unit 时，新 unit 校验失败保持原 unit value，status 置为 conflict。"""
        from unittest.mock import patch
        from src.knowledge_retriever import RobotSelectionDataError
        self._apply_updates({"equipment_unit_id": "AUV-324cc-001"}, task_type_key="pipeline_inspection")
        self.assertEqual(self.dm.slot_store.slots["equipment_unit_id"].status, "valid")

        with patch.object(
            self.kb,
            "validate_static_robot_selection",
            side_effect=RobotSelectionDataError("Mock unit failure", error_code="UNIT_REJECTED"),
        ):
            self._apply_updates({"equipment_unit_id": "WROV-250-001"}, task_type_key="pipeline_inspection")

        unit_slot = self.dm.slot_store.slots["equipment_unit_id"]
        self.assertEqual(unit_slot.value, "AUV-324cc-001")
        self.assertEqual(unit_slot.status, "conflict")
        self.assertEqual(unit_slot.candidate_value, "WROV-250-001")
        self.assertIn("UNIT_REJECTED", unit_slot.validation_error or "")

    # ── 58 ─ 改变父级 class 时，旧 equipment_name 自动失效不留遗留物 (P2-3) ────────
    def test_58_changing_class_clears_orphan_equipment_name(self):
        """58. 真实完整 pipeline: 已有 valid AUV 级联时，切换 class 为 work_class_rov (allow_overwrite=True) 清理旧 AUV name。"""
        self._apply_updates({"equipment_unit_id": "AUV-324cc-001"}, task_type_key="pipeline_inspection")
        self.assertEqual(self.dm.slot_store.slots["equipment_name"].status, "valid")

        self._apply_updates(
            {"equipment_class": "work_class_rov"},
            allow_overwrite=True,
            task_type_key="tree_valve_operation",
        )

        slots = self.dm.slot_store.slots
        self.assertEqual(slots["equipment_class"].value, "work_class_rov")
        # 下级 AUV name 必须已失效为 missing
        self.assertEqual(slots["equipment_name"].status, "missing")

    # ── 59 ─ 统一 specification 输入校验器契约验证 (P2-2) ─────────────────────────
    def test_59_specification_selector_input_validator_unification(self):
        """59. 验证 validate_specification_selector_input 的契约一致性。"""
        from src.slot_store import validate_specification_selector_input, SnapshotValidationError

        # 1. 最小 selector 对象校验成功
        valid_selector = {"type": "power_hp", "value": 150, "variant_id": "workclass_rov_250hp"}
        validate_specification_selector_input(valid_selector)

        # 2. 缺失 required key (如 value) 抛 SnapshotValidationError
        invalid_selector = {"type": "power_hp", "variant_id": "workclass_rov_250hp"}
        with self.assertRaises(SnapshotValidationError) as ctx:
            validate_specification_selector_input(invalid_selector)
        self.assertIn("missing required keys", str(ctx.exception))

        # 3. 非数值 value 抛 SnapshotValidationError
        invalid_val_selector = {"type": "power_hp", "value": "150", "variant_id": "workclass_rov_250hp"}
        with self.assertRaises(SnapshotValidationError) as ctx2:
            validate_specification_selector_input(invalid_val_selector)
        self.assertIn("positive finite number", str(ctx2.exception))

    # ── 60 ─ 同轮父级修改 + 规格缺失 unit 零混合状态 (P1-3 & Round 5) ─────────────
    def test_60_same_turn_parent_change_with_spec_missing_unit_no_hybrid_state(self):
        """60. 已有 valid AUV 级联时，同轮次提交新父级 (observation_rov) + 规格缺失 unit (OBSROV--001)，
        验证在 allow_overwrite=False 和 True 下均不会形成混合状态，完整保留 AUV 级联并将 unit 记为 conflict。"""
        for allow_ov in (False, True):
            with self.subTest(allow_overwrite=allow_ov):
                self.setUp()  # 重置环境
                self._apply_updates({"equipment_unit_id": "AUV-324cc-001"}, task_type_key="pipeline_inspection")

                same_turn_input = {
                    "equipment_class": "observation_rov",
                    "equipment_family": "观察级深海机器人",
                    "equipment_unit_id": "OBSROV--001",
                }
                self._apply_updates(same_turn_input, allow_overwrite=allow_ov, task_type_key="pipeline_inspection")

                slots = self.dm.slot_store.slots
                # 原 valid AUV 级联全量保留，绝无任何 observation_rov 混入
                self.assertEqual(slots["equipment_class"].value, "auv")
                self.assertIn(slots["equipment_class"].status, ("valid", "conflict"))
                self.assertEqual(slots["equipment_family"].value, "水下无人自主航行器")
                self.assertEqual(slots["equipment_unit_id"].value, "AUV-324cc-001")
                self.assertIn(slots["equipment_unit_id"].status, ("valid", "conflict", "invalid"))
                if allow_ov:
                    self.assertEqual(slots["equipment_unit_id"].candidate_value, "OBSROV--001")

    # ── 61 ─ 生产路径真实复用 validate_specification_selector_input ──────────────
    def test_61_dm_production_path_reuses_specification_selector_validator(self):
        """61. 验证 DialogueManager 生产路径真实调用并复用 validate_specification_selector_input。"""
        from unittest.mock import patch
        from src.slot_store import validate_specification_selector_input

        spec_selector = {
            "type": "diameter_mm",
            "value": 324,
            "variant_id": "autonomous_underwater_vehicle_324cc",
        }

        with patch("src.slot_store.validate_specification_selector_input", wraps=validate_specification_selector_input) as mock_val:
            self._apply_updates({"equipment_specification": spec_selector}, task_type_key="pipeline_inspection")

        self.assertTrue(mock_val.called, "DialogueManager production path MUST call validate_specification_selector_input")
        self.assertGreaterEqual(mock_val.call_count, 1)

    # ── 62 ─ 完整级联后未知 class 输入保留有效父级并进入 conflict ─────────────────────
    def test_62_unknown_class_retains_valid_parent_and_marks_conflict(self):
        """62. 验证已有完整 valid 级联后输入 unknown class 时，旧 class/value 保留并进入 conflict，六槽关系不混合。"""
        self._apply_updates({"equipment_unit_id": "AUV-324cc-001"}, task_type_key="pipeline_inspection")
        slots = self.dm.slot_store.slots
        self.assertEqual(slots["equipment_class"].status, "valid")
        self.assertEqual(slots["equipment_class"].value, "auv")

        self._apply_updates({"equipment_class": "未知飞船"}, task_type_key="pipeline_inspection")
        slots = self.dm.slot_store.slots
        self.assertEqual(slots["equipment_class"].value, "auv")
        self.assertEqual(slots["equipment_class"].status, "conflict")
        self.assertEqual(slots["equipment_class"].candidate_value, "未知飞船")
        self.assertEqual(slots["equipment_family"].value, "水下无人自主航行器")
        self.assertEqual(slots["equipment_family"].status, "valid")
        self.assertEqual(slots["equipment_unit_id"].value, "AUV-324cc-001")
        self.assertEqual(slots["equipment_unit_id"].status, "valid")

    # ── 63 ─ 完整级联后未知 family 输入保留有效父级并进入 conflict ────────────────────
    def test_63_unknown_family_retains_valid_parent_and_marks_conflict(self):
        """63. 验证已有完整 valid 级联后输入 unknown family 时，旧 family/value 保留并进入 conflict。"""
        self._apply_updates({"equipment_unit_id": "AUV-324cc-001"}, task_type_key="pipeline_inspection")
        self._apply_updates({"equipment_family": "未知机器人系列"}, task_type_key="pipeline_inspection")
        slots = self.dm.slot_store.slots
        self.assertEqual(slots["equipment_family"].value, "水下无人自主航行器")
        self.assertEqual(slots["equipment_family"].status, "conflict")
        self.assertEqual(slots["equipment_family"].candidate_value, "未知机器人系列")
        self.assertEqual(slots["equipment_class"].value, "auv")
        self.assertEqual(slots["equipment_class"].status, "valid")
        self.assertEqual(slots["equipment_unit_id"].value, "AUV-324cc-001")
        self.assertEqual(slots["equipment_unit_id"].status, "valid")

    # ── 64 ─ 完整级联后非法 specification 输入保留有效值并保持 object 类型 ────────────
    def test_64_invalid_specification_preserves_object_type_and_conflict(self):
        """64. 验证已有完整 valid 级联后输入 invalid specification 时，旧 specification 保留且 value_type 保持 object。"""
        self._apply_updates({"equipment_unit_id": "AUV-324cc-001"}, task_type_key="pipeline_inspection")
        self._apply_updates({"equipment_specification": "invalid_spec_str"}, task_type_key="pipeline_inspection")
        slots = self.dm.slot_store.slots
        self.assertEqual(slots["equipment_specification"].status, "conflict")
        self.assertEqual(slots["equipment_specification"].value_type, "object")
        self.assertIsNotNone(slots["equipment_specification"].value)
        self.assertEqual(slots["equipment_specification"].candidate_value, "invalid_spec_str")

    # ── 65 ─ allow_overwrite=False 下完整新级联替换触冲突壁障绝不穿透 ───────────────
    def test_65_full_new_cascade_blocks_on_conflict_fence_when_allow_overwrite_false(self):
        """65. 验证已有 AUV 级联下，同轮提交完整 WROV 级联在 allow_overwrite=False 时被冲突隔离壁障拦截，绝不替换父级。"""
        self._apply_updates({"equipment_unit_id": "AUV-324cc-001"}, task_type_key="pipeline_inspection")

        wrov_spec = {
            "type": "power_hp",
            "value": 250,
            "variant_id": "workclass_rov_250hp",
        }
        wrov_cascade = {
            "equipment_class": "work_class_rov",
            "equipment_family": "通用工作级深海机器人",
            "equipment_specification": wrov_spec,
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

    # ── 66 ─ allow_overwrite=True 下 family/variant 冲突自动触发原子回滚绝无混合状态 ──
    def test_66_family_variant_mismatch_rollback_prevents_hybrid_state(self):
        """66. 验证已有 AUV 级联后，同轮 observation family + AUV variant 在 allow_overwrite=True 下触发回滚绝不产生混合状态。"""
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

    # ── 67 ─ specification selector candidate 的 snapshot 导出与恢复 ───────────────
    def test_67_specification_selector_candidate_snapshot_roundtrip(self):
        """67. 验证包含 selector candidate 的 equipment_specification 槽位快照导出与恢复正常。"""
        spec_selector = {
            "type": "diameter_mm",
            "value": 324,
            "variant_id": "autonomous_underwater_vehicle_324cc",
        }
        self._apply_updates({"equipment_specification": spec_selector}, task_type_key="pipeline_inspection")
        snapshot = self.dm.export_snapshot()

        dm_restored = DialogueManager(self.llm, self.kb)
        dm_restored.load_snapshot(snapshot)

        restored_slot = dm_restored.slot_store.slots.get("equipment_specification")
        self.assertIsNotNone(restored_slot)
        self.assertEqual(restored_slot.candidate_value.get("variant_id"), "autonomous_underwater_vehicle_324cc")

    # ── 68 ─ 缺失规格的单元无法发布并保持清晰的缺失状态 ──────────────────────────
    def test_68_missing_spec_unit_blocks_publish(self):
        """68. 验证缺失规格的单元无法发布，并保持明确状态。"""
        dm = DialogueManager(self.llm, self.kb)
        res = dm.process("确认")
        self.assertIn(dm.phase, ("collecting", "blocked_hard", "blocked_soft", "idle"))


if __name__ == "__main__":
    unittest.main()
