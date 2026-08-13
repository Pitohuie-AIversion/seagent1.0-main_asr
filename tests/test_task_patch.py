"""test_task_patch.py — TaskPatch 模块与 Feature Flag、Effect Parity 规范化测试套件"""

import copy
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, MagicMock
import yaml

from src.task_patch import (
    SlotPatch,
    ListMutationPatch,
    TaskPatch,
    TaskPatchError,
    TaskPatchValidationError,
    TaskPatchAdapterError,
    build_task_patch,
    task_patch_to_legacy_updates,
)
from src.model_profile import (
    is_task_patch_v2_enabled,
    ModelProfileConfigError,
)
from src.dialogue_manager import DialogueManager
from src.slot_store import Slot
from tests.interaction_plan_support import make_plan


def snapshot_effect(dm: DialogueManager) -> dict:
    """提取 DialogueManager 槽位与任务状态 Snapshot 的 Effect 映射。"""
    working_slots = dm.slot_store.slots
    slots_summary = {}
    for key, s in working_slots.items():
        val = s.value
        if key == "internal_id" and val:
            val = "MOCKED_INTERNAL_ID"
        slots_summary[key] = {
            "value": val,
            "status": s.status,
            "candidate_value": s.candidate_value,
            "raw_value": s.raw_value,
            "validation_error": s.validation_error,
            "source": s.source,
            "confidence": s.confidence,
        }

    task_state = copy.deepcopy(dm.slot_store.get_task_state())
    task_state.pop("store_id", None)
    if "internal_id" in task_state and task_state["internal_id"]:
        task_state["internal_id"] = "MOCKED_INTERNAL_ID"

    return {
        "slot_version": dm.slot_store.version,
        "slots": slots_summary,
        "task_state": task_state,
        "unresolved": list(dm.slot_store.unresolved),
        "phase": dm.phase,
        "published": dm.final_result is not None,
    }


class TestTaskPatchUnit(unittest.TestCase):
    """L1: TaskPatch 内部校验与 API 边界单元测试。"""

    def test_valid_slot_patch(self):
        sp = SlotPatch(
            key="water_depth",
            candidate_value=300,
            raw_value="300米",
            confidence=0.95,
            source="user_input",
            resolution_method="canonical_exact",
        )
        self.assertEqual(sp.key, "water_depth")
        self.assertEqual(sp.candidate_value, 300)
        self.assertEqual(sp.raw_value, "300米")
        self.assertEqual(sp.confidence, 0.95)
        self.assertEqual(sp.source, "user_input")
        self.assertEqual(sp.resolution_method, "canonical_exact")

    def test_reject_empty_key(self):
        with self.assertRaises(TaskPatchValidationError):
            SlotPatch(
                key="  ",
                candidate_value=100,
                raw_value="100",
                confidence=1.0,
                source="user_input",
            )

    def test_reject_unknown_key(self):
        res = {
            "slot_candidates": [
                {
                    "raw_key": "未知",
                    "canonical_key": "unknown_key",
                    "raw_value": "abc",
                    "normalized_value": "abc",
                    "confidence": 1.0,
                }
            ],
            "unresolved": [],
            "list_mutations": [],
        }
        with self.assertRaises(TaskPatchValidationError):
            build_task_patch(res, allowed_keys={"water_depth", "task_type"})

    def test_reject_bool_confidence(self):
        with self.assertRaises(TaskPatchValidationError):
            SlotPatch(
                key="water_depth",
                candidate_value=300,
                raw_value="300米",
                confidence=True,  # bool 非法
                source="user_input",
            )

    def test_reject_nan_confidence(self):
        with self.assertRaises(TaskPatchValidationError):
            SlotPatch(
                key="water_depth",
                candidate_value=300,
                raw_value="300米",
                confidence=float("nan"),
                source="user_input",
            )

    def test_reject_inf_confidence(self):
        with self.assertRaises(TaskPatchValidationError):
            SlotPatch(
                key="water_depth",
                candidate_value=300,
                raw_value="300米",
                confidence=float("inf"),
                source="user_input",
            )

    def test_reject_confidence_below_zero(self):
        with self.assertRaises(TaskPatchValidationError):
            SlotPatch(
                key="water_depth",
                candidate_value=300,
                raw_value="300米",
                confidence=-0.1,
                source="user_input",
            )

    def test_reject_confidence_above_one(self):
        with self.assertRaises(TaskPatchValidationError):
            SlotPatch(
                key="water_depth",
                candidate_value=300,
                raw_value="300米",
                confidence=1.05,
                source="user_input",
            )

    # Negative Candidate Contract Tests
    def test_reject_candidate_missing_canonical_key(self):
        res = {
            "slot_candidates": [
                {
                    "raw_value": "300米",
                    "normalized_value": 300,
                    "confidence": 0.95,
                }
            ],
            "list_mutations": [],
            "unresolved": [],
        }
        with self.assertRaises(TaskPatchValidationError):
            build_task_patch(res)

    def test_reject_candidate_missing_normalized_value(self):
        res = {
            "slot_candidates": [
                {
                    "canonical_key": "water_depth",
                    "raw_value": "300米",
                    "confidence": 0.95,
                }
            ],
            "list_mutations": [],
            "unresolved": [],
        }
        with self.assertRaises(TaskPatchValidationError):
            build_task_patch(res)

    def test_reject_candidate_missing_raw_value(self):
        res = {
            "slot_candidates": [
                {
                    "canonical_key": "water_depth",
                    "normalized_value": 300,
                    "confidence": 0.95,
                }
            ],
            "list_mutations": [],
            "unresolved": [],
        }
        with self.assertRaises(TaskPatchValidationError):
            build_task_patch(res)

    def test_reject_candidate_missing_confidence(self):
        res = {
            "slot_candidates": [
                {
                    "canonical_key": "water_depth",
                    "raw_value": "300米",
                    "normalized_value": 300,
                }
            ],
            "list_mutations": [],
            "unresolved": [],
        }
        with self.assertRaises(TaskPatchValidationError):
            build_task_patch(res)

    def test_reject_candidate_none_normalized_value(self):
        res = {
            "slot_candidates": [
                {
                    "canonical_key": "water_depth",
                    "raw_value": "300米",
                    "normalized_value": None,
                    "confidence": 0.95,
                }
            ],
            "list_mutations": [],
            "unresolved": [],
        }
        with self.assertRaises(TaskPatchValidationError):
            build_task_patch(res)

    def test_reject_candidate_empty_normalized_value(self):
        res = {
            "slot_candidates": [
                {
                    "canonical_key": "water_depth",
                    "raw_value": "300米",
                    "normalized_value": "",
                    "confidence": 0.95,
                }
            ],
            "list_mutations": [],
            "unresolved": [],
        }
        with self.assertRaises(TaskPatchValidationError):
            build_task_patch(res)

    def test_reject_non_string_resolution_method(self):
        res = {
            "slot_candidates": [
                {
                    "canonical_key": "water_depth",
                    "raw_value": "300米",
                    "normalized_value": 300,
                    "confidence": 0.95,
                    "resolution_method": 123,
                }
            ],
            "list_mutations": [],
            "unresolved": [],
        }
        with self.assertRaises(TaskPatchValidationError):
            build_task_patch(res)

    # Negative List Mutation Contract Tests
    def test_reject_mutation_missing_field(self):
        res = {
            "slot_candidates": [],
            "list_mutations": [
                {
                    "operation": "add",
                    "items": ["机械手"],
                    "target_items": [],
                    "raw_text": "增加机械手",
                    "confidence": 0.95,
                    "source": "user_input",
                }
            ],
            "unresolved": [],
        }
        with self.assertRaises(TaskPatchValidationError):
            build_task_patch(res)

    def test_reject_non_payload_mutation_field(self):
        res = {
            "slot_candidates": [],
            "list_mutations": [
                {
                    "field": "water_depth",
                    "operation": "remove",
                    "items": ["300米"],
                    "target_items": [],
                    "raw_text": "删除水深",
                    "confidence": 0.95,
                    "source": "user_input",
                }
            ],
            "unresolved": [],
        }
        with self.assertRaises(TaskPatchValidationError):
            build_task_patch(res)

    def test_reject_mutation_missing_operation(self):
        res = {
            "slot_candidates": [],
            "list_mutations": [
                {
                    "field": "payload",
                    "items": ["机械手"],
                    "target_items": [],
                    "raw_text": "增加机械手",
                    "confidence": 0.95,
                    "source": "user_input",
                }
            ],
            "unresolved": [],
        }
        with self.assertRaises(TaskPatchValidationError):
            build_task_patch(res)

    def test_reject_mutation_missing_items(self):
        res = {
            "slot_candidates": [],
            "list_mutations": [
                {
                    "field": "payload",
                    "operation": "add",
                    "target_items": [],
                    "raw_text": "增加机械手",
                    "confidence": 0.95,
                    "source": "user_input",
                }
            ],
            "unresolved": [],
        }
        with self.assertRaises(TaskPatchValidationError):
            build_task_patch(res)

    def test_reject_mutation_missing_target_items(self):
        res = {
            "slot_candidates": [],
            "list_mutations": [
                {
                    "field": "payload",
                    "operation": "add",
                    "items": ["机械手"],
                    "raw_text": "增加机械手",
                    "confidence": 0.95,
                    "source": "user_input",
                }
            ],
            "unresolved": [],
        }
        with self.assertRaises(TaskPatchValidationError):
            build_task_patch(res)

    def test_reject_mutation_missing_raw_text(self):
        res = {
            "slot_candidates": [],
            "list_mutations": [
                {
                    "field": "payload",
                    "operation": "add",
                    "items": ["机械手"],
                    "target_items": [],
                    "confidence": 0.95,
                    "source": "user_input",
                }
            ],
            "unresolved": [],
        }
        with self.assertRaises(TaskPatchValidationError):
            build_task_patch(res)

    def test_reject_mutation_missing_confidence(self):
        res = {
            "slot_candidates": [],
            "list_mutations": [
                {
                    "field": "payload",
                    "operation": "add",
                    "items": ["机械手"],
                    "target_items": [],
                    "raw_text": "增加机械手",
                    "source": "user_input",
                }
            ],
            "unresolved": [],
        }
        with self.assertRaises(TaskPatchValidationError):
            build_task_patch(res)

    def test_reject_mutation_missing_source(self):
        res = {
            "slot_candidates": [],
            "list_mutations": [
                {
                    "field": "payload",
                    "operation": "add",
                    "items": ["机械手"],
                    "target_items": [],
                    "raw_text": "增加机械手",
                    "confidence": 0.95,
                }
            ],
            "unresolved": [],
        }
        with self.assertRaises(TaskPatchValidationError):
            build_task_patch(res)

    def test_reject_non_string_raw_text(self):
        res = {
            "slot_candidates": [],
            "list_mutations": [
                {
                    "field": "payload",
                    "operation": "add",
                    "items": ["机械手"],
                    "target_items": [],
                    "raw_text": 123,
                    "confidence": 0.95,
                    "source": "user_input",
                }
            ],
            "unresolved": [],
        }
        with self.assertRaises(TaskPatchValidationError):
            build_task_patch(res)

    def test_reject_non_string_source(self):
        res = {
            "slot_candidates": [],
            "list_mutations": [
                {
                    "field": "payload",
                    "operation": "add",
                    "items": ["机械手"],
                    "target_items": [],
                    "raw_text": "增加机械手",
                    "confidence": 0.95,
                    "source": 123,
                }
            ],
            "unresolved": [],
        }
        with self.assertRaises(TaskPatchValidationError):
            build_task_patch(res)

    # Negative Unresolved Contract Tests
    def test_reject_non_string_unresolved(self):
        res = {
            "slot_candidates": [],
            "list_mutations": [],
            "unresolved": ["abc", 123],
        }
        with self.assertRaises(TaskPatchValidationError):
            build_task_patch(res)

    # Negative Top-level Contract Tests
    def test_reject_missing_slot_candidates(self):
        res = {
            "list_mutations": [],
            "unresolved": [],
        }
        with self.assertRaises(TaskPatchValidationError):
            build_task_patch(res)

    def test_reject_missing_list_mutations(self):
        res = {
            "slot_candidates": [],
            "unresolved": [],
        }
        with self.assertRaises(TaskPatchValidationError):
            build_task_patch(res)

    def test_reject_missing_unresolved(self):
        res = {
            "slot_candidates": [],
            "list_mutations": [],
        }
        with self.assertRaises(TaskPatchValidationError):
            build_task_patch(res)


class TestTaskPatchBuilderAndAdapter(unittest.TestCase):
    """L2: Builder & Legacy Adapter 转换逻辑测试。"""

    def test_build_task_patch_from_extraction_result(self):
        extraction_res = {
            "slot_candidates": [
                {
                    "raw_key": "水深",
                    "canonical_key": "water_depth",
                    "raw_value": "大约300米",
                    "normalized_value": 300,
                    "confidence": 0.95,
                    "resolution_method": "canonical_exact",
                }
            ],
            "list_mutations": [
                {
                    "field": "payload",
                    "operation": "add",
                    "items": ["高精声呐"],
                    "target_items": [],
                    "raw_text": "增加高精声呐",
                    "confidence": 0.95,
                    "source": "user_input",
                }
            ],
            "unresolved": ["  一些未识别说明  ", "一些未识别说明"],
        }
        patch_obj = build_task_patch(extraction_res, allowed_keys={"water_depth", "payload"})
        self.assertEqual(len(patch_obj.slot_updates), 1)
        self.assertEqual(patch_obj.slot_updates[0].key, "water_depth")
        self.assertEqual(patch_obj.slot_updates[0].candidate_value, 300)

        self.assertEqual(len(patch_obj.list_mutations), 1)
        self.assertEqual(patch_obj.list_mutations[0].operation, "add")
        self.assertEqual(patch_obj.list_mutations[0].items, ("高精声呐",))

        self.assertEqual(patch_obj.unresolved, ("一些未识别说明",))

    def test_preserves_raw_value(self):
        res = {
            "slot_candidates": [
                {
                    "canonical_key": "water_depth",
                    "raw_value": "差不多三百度米",
                    "normalized_value": "300",
                    "confidence": 0.8,
                    "resolution_method": "llm_semantic",
                }
            ],
            "unresolved": [],
            "list_mutations": [],
        }
        patch_obj = build_task_patch(res, allowed_keys={"water_depth"})
        self.assertEqual(patch_obj.slot_updates[0].raw_value, "差不多三百度米")

    def test_preserves_resolution_method(self):
        res = {
            "slot_candidates": [
                {
                    "canonical_key": "cable_type",
                    "raw_value": "光纤",
                    "normalized_value": "armored_cable",
                    "confidence": 0.9,
                    "resolution_method": "alias_exact",
                }
            ],
            "unresolved": [],
            "list_mutations": [],
        }
        patch_obj = build_task_patch(res, allowed_keys={"cable_type"})
        self.assertEqual(patch_obj.slot_updates[0].resolution_method, "alias_exact")

    def test_preserves_unresolved_order(self):
        res = {
            "slot_candidates": [],
            "list_mutations": [],
            "unresolved": ["item_b", "item_a", "item_c"],
        }
        patch_obj = build_task_patch(res)
        self.assertEqual(patch_obj.unresolved, ("item_b", "item_a", "item_c"))

    def test_deduplicates_unresolved_stably(self):
        res = {
            "slot_candidates": [],
            "list_mutations": [],
            "unresolved": ["abc", "", "abc", " def "],
        }
        patch_obj = build_task_patch(res)
        self.assertEqual(patch_obj.unresolved, ("abc", "def"))


class TestListMutationContract(unittest.TestCase):
    """ListMutation 契约与规则断言测试。"""

    def test_add_mutation_contract(self):
        m = ListMutationPatch(
            field="payload",
            operation="add",
            items=("机械手",),
            target_items=(),
            raw_text="加一个机械手",
            confidence=0.95,
            source="user_input",
        )
        self.assertEqual(m.operation, "add")

    def test_remove_mutation_contract(self):
        m = ListMutationPatch(
            field="payload",
            operation="remove",
            items=("机械手",),
            target_items=(),
            raw_text="不要机械手",
            confidence=0.95,
            source="user_input",
        )
        self.assertEqual(m.operation, "remove")

    def test_replace_mutation_contract(self):
        m = ListMutationPatch(
            field="payload",
            operation="replace",
            items=("云台摄像机",),
            target_items=("机械手",),
            raw_text="把机械手换成云台摄像机",
            confidence=0.95,
            source="user_input",
        )
        self.assertEqual(m.operation, "replace")

    def test_clear_mutation_contract(self):
        m = ListMutationPatch(
            field="payload",
            operation="clear",
            items=(),
            target_items=(),
            raw_text="清空载荷",
            confidence=0.95,
            source="user_input",
        )
        self.assertEqual(m.operation, "clear")

    def test_reject_invalid_operation(self):
        with self.assertRaises(TaskPatchValidationError):
            ListMutationPatch(
                field="payload",
                operation="invalid_op",  # type: ignore
                items=("机械手",),
                target_items=(),
                raw_text="无效",
                confidence=0.95,
                source="user_input",
            )

    def test_reject_replace_without_target(self):
        with self.assertRaises(TaskPatchValidationError):
            ListMutationPatch(
                field="payload",
                operation="replace",
                items=("云台摄像机",),
                target_items=(),  # replace 必须提供 target_items
                raw_text="换成云台摄像机",
                confidence=0.95,
                source="user_input",
            )

class TestTaskPatchAdapter(unittest.TestCase):
    """Legacy Adapter 纯函数转换逻辑测试。"""

    def test_task_patch_to_legacy_preserves_candidate(self):
        sp = SlotPatch(
            key="water_depth",
            candidate_value=300,
            raw_value="300米",
            confidence=0.95,
            source="user_input",
        )
        patch_obj = TaskPatch(schema_version=1, slot_updates=(sp,), list_mutations=(), unresolved=())
        stage_updates, mutations, unresolved = task_patch_to_legacy_updates(patch_obj)
        self.assertIn("water_depth", stage_updates)
        self.assertEqual(stage_updates["water_depth"]["value"], 300)

    def test_task_patch_to_legacy_preserves_raw(self):
        sp = SlotPatch(
            key="water_depth",
            candidate_value=300,
            raw_value="差不多三百度米",
            confidence=0.8,
            source="user_input",
        )
        patch_obj = TaskPatch(schema_version=1, slot_updates=(sp,), list_mutations=(), unresolved=())
        stage_updates, _, _ = task_patch_to_legacy_updates(patch_obj)
        self.assertEqual(stage_updates["water_depth"]["raw_value"], "差不多三百度米")

    def test_task_patch_to_legacy_preserves_confidence(self):
        sp = SlotPatch(
            key="water_depth",
            candidate_value=300,
            raw_value="300米",
            confidence=0.92,
            source="user_input",
        )
        patch_obj = TaskPatch(schema_version=1, slot_updates=(sp,), list_mutations=(), unresolved=())
        stage_updates, _, _ = task_patch_to_legacy_updates(patch_obj)
        self.assertEqual(stage_updates["water_depth"]["confidence"], 0.92)

    def test_task_patch_to_legacy_preserves_source_semantics(self):
        sp = SlotPatch(
            key="cable_type",
            candidate_value="armored_cable",
            raw_value="铠装电缆",
            confidence=0.9,
            source="alias_mapping",
            resolution_method="alias_exact",
        )
        patch_obj = TaskPatch(schema_version=1, slot_updates=(sp,), list_mutations=(), unresolved=())
        stage_updates, _, _ = task_patch_to_legacy_updates(patch_obj)
        self.assertEqual(stage_updates["cable_type"]["source"], "alias_mapping")

    def test_provenance_retention(self):
        cand = {
            "canonical_key": "water_depth",
            "raw_value": "差不多很深",
            "normalized_value": "300abc",
            "confidence": 0.83,
            "resolution_method": "type_normalization",
        }
        res = {
            "slot_candidates": [cand],
            "list_mutations": [],
            "unresolved": [],
        }
        patch_obj = build_task_patch(res, allowed_keys={"water_depth"})
        self.assertEqual(patch_obj.slot_updates[0].candidate_value, "300abc")
        self.assertEqual(patch_obj.slot_updates[0].raw_value, "差不多很深")
        self.assertEqual(patch_obj.slot_updates[0].confidence, 0.83)
        self.assertEqual(patch_obj.slot_updates[0].resolution_method, "type_normalization")

        stage_updates, mutations, unresolved = task_patch_to_legacy_updates(patch_obj)
        self.assertIn("water_depth", stage_updates)
        self.assertEqual(stage_updates["water_depth"]["value"], "300abc")
        self.assertEqual(stage_updates["water_depth"]["raw_value"], "差不多很深")
        self.assertEqual(stage_updates["water_depth"]["confidence"], 0.83)
        self.assertEqual(stage_updates["water_depth"]["source"], "user_input")


class TestFeatureFlag(unittest.TestCase):
    """Feature Flag 控制与 Fail-Closed 断言测试。"""

    @staticmethod
    def _make_write_llm():
        llm = MagicMock()
        llm.classify_interaction.return_value = make_plan("WRITE")
        return llm

    def test_task_patch_flag_defaults_false(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            yaml_path = Path(tmpdir) / "features.yaml"
            yaml_path.write_text("schema_version: 1\nfeatures: {}\n", encoding="utf-8")
            self.assertFalse(is_task_patch_v2_enabled(features_path=yaml_path))

    def test_task_patch_flag_true_uses_task_patch(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            yaml_path = Path(tmpdir) / "features.yaml"
            yaml_path.write_text(
                "schema_version: 1\nfeatures:\n  task_patch_v2: true\n",
                encoding="utf-8",
            )
            self.assertTrue(is_task_patch_v2_enabled(features_path=yaml_path))

    def test_task_patch_flag_false_does_not_construct_task_patch(self):
        dm = DialogueManager(llm=self._make_write_llm())
        with patch("src.dialogue_manager.is_task_patch_v2_enabled", return_value=False), \
             patch("src.dialogue_manager.build_task_patch") as mock_builder:
            dm.process("创建一个管缆巡检任务")
            mock_builder.assert_not_called()

    def test_task_patch_invalid_config_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            yaml_path = Path(tmpdir) / "features.yaml"
            # 字符串 "true" 非 bool
            yaml_path.write_text(
                'schema_version: 1\nfeatures:\n  task_patch_v2: "true"\n',
                encoding="utf-8",
            )
            with self.assertRaises(ModelProfileConfigError):
                is_task_patch_v2_enabled(features_path=yaml_path)

    def test_task_patch_build_failure_does_not_fallback_to_write(self):
        dm = DialogueManager(llm=self._make_write_llm())
        bad_extraction = {
            "slot_candidates": [
                {
                    "canonical_key": "water_depth",
                    "normalized_value": 300,
                    "raw_value": "300米",
                    "confidence": "INVALID_CONFIDENCE",  # 会引发 build_task_patch 校验失败
                }
            ],
            "list_mutations": [],
            "unresolved": [],
        }

        dm.extractor.extract_updates = MagicMock(return_value=bad_extraction)
        dm.task_type_key = "pipeline_inspection"
        dm.slot_store.commit_transaction(
            {
                "task_type_key": Slot(
                    "task_type_key",
                    value="pipeline_inspection",
                    status="valid",
                )
            },
            [],
        )

        with patch("src.dialogue_manager.is_task_patch_v2_enabled", return_value=True):
            with self.assertRaises(TaskPatchError):
                dm.process("水深300米")

    def test_v2_malformed_candidate_does_not_mutate_slotstore(self):
        dm = DialogueManager(llm=self._make_write_llm())
        bad_extraction = {
            "slot_candidates": [
                {
                    "canonical_key": "water_depth",
                    "normalized_value": 300,
                    # "raw_value" and "confidence" missing
                }
            ],
            "list_mutations": [],
            "unresolved": [],
        }

        dm.extractor.extract_updates = MagicMock(return_value=bad_extraction)
        dm.task_type_key = "pipeline_inspection"
        dm.slot_store.commit_transaction(
            {
                "task_type_key": Slot(
                    "task_type_key",
                    value="pipeline_inspection",
                    status="valid",
                )
            },
            [],
        )

        version_before = dm.slot_store.version
        snapshot_before = copy.deepcopy(dm.slot_store.export_snapshot())

        with patch("src.dialogue_manager.is_task_patch_v2_enabled", return_value=True):
            with self.assertRaises(TaskPatchValidationError):
                dm.process("水深300米")

        self.assertEqual(dm.slot_store.version, version_before)
        self.assertEqual(dm.slot_store.export_snapshot(), snapshot_before)
        self.assertIsNone(dm.final_result)
        self.assertNotEqual(dm.phase, "done")

    def test_v2_malformed_candidate_shapes_do_not_mutate_slotstore(self):
        """Schema 投影前必须原子拒绝非 dict、缺 key、非字符串 key。"""
        malformed_candidates = (
            "not-a-dict",
            {
                "normalized_value": 300,
                "raw_value": "水深300米",
                "confidence": 1.0,
            },
            {
                "canonical_key": 123,
                "normalized_value": 300,
                "raw_value": "水深300米",
                "confidence": 1.0,
            },
        )
        for malformed in malformed_candidates:
            with self.subTest(malformed=malformed):
                dm = DialogueManager(llm=self._make_write_llm())
                dm.extractor.extract_updates = MagicMock(
                    return_value={
                        "slot_candidates": [malformed],
                        "list_mutations": [],
                        "unresolved": [],
                    }
                )
                dm.task_type_key = "pipeline_inspection"
                dm.slot_store.commit_transaction(
                    {
                        "task_type_key": Slot(
                            "task_type_key",
                            value="pipeline_inspection",
                            status="valid",
                        )
                    },
                    [],
                )
                version_before = dm.slot_store.version
                snapshot_before = copy.deepcopy(dm.slot_store.export_snapshot())

                with patch(
                    "src.dialogue_manager.is_task_patch_v2_enabled",
                    return_value=True,
                ):
                    with self.assertRaises(TaskPatchValidationError):
                        dm.process("水深300米")

                self.assertEqual(dm.slot_store.version, version_before)
                self.assertEqual(dm.slot_store.export_snapshot(), snapshot_before)
                self.assertIsNone(dm.final_result)
                self.assertNotEqual(dm.phase, "done")

    def test_v2_missing_mutation_field_does_not_mutate_slotstore(self):
        dm = DialogueManager(llm=self._make_write_llm())
        bad_extraction = {
            "slot_candidates": [],
            "list_mutations": [
                {
                    # "field" intentionally missing
                    "operation": "remove",
                    "items": ["机械手"],
                    "target_items": [],
                    "raw_text": "去掉机械手",
                    "confidence": 0.95,
                    "source": "user_input",
                }
            ],
            "unresolved": [],
        }

        dm.extractor.extract_updates = MagicMock(return_value=bad_extraction)
        dm.task_type_key = "pipeline_inspection"
        dm.slot_store.commit_transaction(
            {
                "task_type_key": Slot(
                    "task_type_key",
                    value="pipeline_inspection",
                    status="valid",
                )
            },
            [],
        )

        version_before = dm.slot_store.version
        snapshot_before = copy.deepcopy(dm.slot_store.export_snapshot())

        with patch("src.dialogue_manager.is_task_patch_v2_enabled", return_value=True):
            with self.assertRaises(TaskPatchValidationError):
                dm.process("换成机械手")

        self.assertEqual(dm.slot_store.version, version_before)
        self.assertEqual(dm.slot_store.export_snapshot(), snapshot_before)
        self.assertIsNone(dm.final_result)
        self.assertNotEqual(dm.phase, "done")


class TestEffectParity(unittest.TestCase):
    """比较 legacy 与 TaskPatch v2 的确定性提交效果，不在测试替身中解析自然语言。"""

    def setUp(self):
        self.mock_llm = MagicMock()
        self.mock_llm.classify_interaction.return_value = make_plan("WRITE")

    @staticmethod
    def _task_extraction() -> dict:
        return {
            "slot_candidates": [
                {
                    "raw_key": "任务类型",
                    "canonical_key": "task_type",
                    "raw_value": "管缆巡检",
                    "normalized_value": "管缆巡检",
                    "confidence": 0.95,
                    "resolution_method": "canonical_exact",
                },
                {
                    "raw_key": "任务类型标识",
                    "canonical_key": "task_type_key",
                    "raw_value": "巡检",
                    "normalized_value": "pipeline_inspection",
                    "confidence": 0.95,
                    "resolution_method": "canonical_exact",
                },
            ],
            "list_mutations": [],
            "unresolved": [],
        }

    @staticmethod
    def _depth_extraction(value: int) -> dict:
        return {
            "slot_candidates": [
                {
                    "raw_key": "水深",
                    "canonical_key": "water_depth",
                    "raw_value": f"{value}米",
                    "normalized_value": value,
                    "confidence": 0.95,
                    "resolution_method": "canonical_exact",
                }
            ],
            "list_mutations": [],
            "unresolved": [],
        }

    @staticmethod
    def _invalid_depth_extraction() -> dict:
        return {
            "slot_candidates": [
                {
                    "raw_key": "水深",
                    "canonical_key": "water_depth",
                    "raw_value": "非法深度",
                    "normalized_value": "invalid_depth_val",
                    "confidence": 0.95,
                    "resolution_method": "type_normalization",
                }
            ],
            "list_mutations": [],
            "unresolved": [],
        }

    def _execute_steps(
        self,
        dm: DialogueManager,
        steps: list[tuple[str, dict]],
        *,
        task_patch_v2: bool,
    ) -> dict:
        with patch(
            "src.dialogue_manager.is_task_patch_v2_enabled",
            return_value=task_patch_v2,
        ):
            for label, extraction in steps:
                with patch.object(
                    dm.extractor,
                    "extract_updates",
                    return_value=copy.deepcopy(extraction),
                ) as mock_extract:
                    dm.process(label)
                    mock_extract.assert_called()
        return snapshot_effect(dm)

    def _run_parity(self, steps: list[tuple[str, dict]]) -> tuple[dict, dict]:
        legacy = self._execute_steps(
            DialogueManager(llm=self.mock_llm),
            steps,
            task_patch_v2=False,
        )
        v2 = self._execute_steps(
            DialogueManager(llm=self.mock_llm),
            steps,
            task_patch_v2=True,
        )
        self.assertEqual(legacy, v2)
        return legacy, v2

    def test_parity_01_create_task(self):
        _, effect = self._run_parity(
            [("turn:create", self._task_extraction())]
        )
        self.assertGreater(effect["slot_version"], 0)
        self.assertEqual(
            effect["task_state"]["task_type_key"],
            "pipeline_inspection",
        )

    def test_parity_02_valid_field(self):
        _, effect = self._run_parity(
            [
                ("turn:create", self._task_extraction()),
                ("turn:set-depth", self._depth_extraction(300)),
            ]
        )
        self.assertEqual(effect["task_state"]["water_depth"], 300.0)

    def test_parity_03_modify_valid(self):
        _, effect = self._run_parity(
            [
                ("turn:create", self._task_extraction()),
                ("turn:set-depth", self._depth_extraction(300)),
                ("turn:replace-depth", self._depth_extraction(500)),
            ]
        )
        self.assertEqual(effect["task_state"]["water_depth"], 500.0)

    def test_parity_04_normalization_failure_with_old_valid(self):
        _, effect = self._run_parity(
            [
                ("turn:create", self._task_extraction()),
                ("turn:set-depth", self._depth_extraction(300)),
                ("turn:invalid-depth", self._invalid_depth_extraction()),
            ]
        )
        slot = effect["slots"]["water_depth"]
        self.assertEqual(slot["value"], 300)
        self.assertEqual(slot["candidate_value"], "invalid_depth_val")
        self.assertEqual(slot["status"], "conflict")
        self.assertIsNotNone(slot["validation_error"])

    def test_parity_05_normalization_failure_without_old_valid(self):
        _, effect = self._run_parity(
            [
                ("turn:create", self._task_extraction()),
                ("turn:invalid-depth", self._invalid_depth_extraction()),
            ]
        )
        slot = effect["slots"]["water_depth"]
        self.assertIsNone(slot["value"])
        self.assertEqual(slot["candidate_value"], "invalid_depth_val")
        self.assertEqual(slot["status"], "invalid")
        self.assertIsNotNone(slot["validation_error"])

    def test_parity_06_unresolved_preserved(self):
        _, effect = self._run_parity(
            [
                ("turn:create", self._task_extraction()),
                (
                    "turn:unresolved",
                    {
                        "slot_candidates": [],
                        "list_mutations": [],
                        "unresolved": ["未识别信息XYZ"],
                    },
                ),
            ]
        )
        self.assertIn("未识别信息XYZ", effect["unresolved"])


if __name__ == "__main__":
    unittest.main()
