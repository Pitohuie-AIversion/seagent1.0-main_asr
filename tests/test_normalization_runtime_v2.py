"""test_normalization_runtime_v2.py — NormalizationContract V2 Runtime Integration Tests

验证 TaskPatch V2 + NormalizationContract V2 接入 DM WRITE Runtime 的 Flow、
Feature Flag Matrix (A/B/C/D 组合)、Apply Adapter、双重规范化防范与错误 Fail Closed 机制。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.dialogue_manager import DialogueManager
from src.intent_router import IntentRouteResult
from src.model_profile import (
    ModelProfileConfigError,
    is_normalization_contract_v2_enabled,
    is_task_patch_v2_enabled,
)
from src.normalization_contract import (
    NORMALIZATION_RUNTIME_PASSTHROUGH_KEYS,
    NormalizationApplyPlan,
    NormalizationContractError,
    NormalizedSlotApply,
    NormalizedTaskPatch,
    SlotNormalizationOutcome,
    normalize_task_patch,
    normalized_task_patch_to_apply_plan,
    validate_normalization_runtime_flags,
)
from src.normalizer import FieldNormalizer
from src.slot_store import Slot, SlotStore
from src.task_patch import ListMutationPatch, SlotPatch, TaskPatch


class TestNormalizationRuntimeV2Flags(unittest.TestCase):
    """验证 Feature Flag Matrix Loader 与 Fail Closed 校验。"""

    def test_flag_loader_helper_default_false(self):
        self.assertFalse(is_normalization_contract_v2_enabled())

    def test_flag_loader_helper_reads_yaml(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            yaml_path = Path(tmpdir) / "features.yaml"
            yaml_path.write_text(
                "schema_version: 1\nfeatures:\n  normalization_contract_v2: true\n",
                encoding="utf-8",
            )
            self.assertTrue(is_normalization_contract_v2_enabled(features_path=yaml_path))

    def test_flag_matrix_validation(self):
        # A: false/false -> valid
        validate_normalization_runtime_flags(False, False)
        # B: true/false -> valid
        validate_normalization_runtime_flags(True, False)
        # C: true/true -> valid
        validate_normalization_runtime_flags(True, True)
        # D: false/true -> invalid -> raises NormalizationContractError
        with self.assertRaises(NormalizationContractError) as ctx:
            validate_normalization_runtime_flags(False, True)
        self.assertIn("非法 Feature Flag 组合", str(ctx.exception))

    def test_flags_false_true_fails_closed_in_dm(self):
        dm = DialogueManager(llm=MagicMock())
        version_before = dm.slot_store.version
        slots_before = {k: (s.value, s.status) for k, s in dm.slot_store.slots.items()}

        with patch("src.dialogue_manager.is_task_patch_v2_enabled", return_value=False), \
             patch("src.dialogue_manager.is_normalization_contract_v2_enabled", return_value=True):
            with self.assertRaises(NormalizationContractError):
                dm.process("创建一个水深300米的任务")

        version_after = dm.slot_store.version
        slots_after = {k: (s.value, s.status) for k, s in dm.slot_store.slots.items()}
        self.assertEqual(version_before, version_after)
        self.assertEqual(slots_before, slots_after)


class TestApplyAdapter(unittest.TestCase):
    """验证 Apply Adapter 纯函数转换、不可变性与零副作用。"""

    def test_apply_adapter_purity_and_immutability(self):
        outcome_succ = SlotNormalizationOutcome(
            key="water_depth",
            success=True,
            normalized_value=300,
            candidate_value=300,
            raw_value="300米",
            confidence=1.0,
            source="user_input",
        )
        outcome_fail = SlotNormalizationOutcome(
            key="support_vessel",
            success=False,
            normalized_value=None,
            candidate_value="魔幻飞船",
            raw_value="魔幻飞船",
            confidence=0.9,
            source="user_input",
            error_code="invalid_enum",
            error_message="无法规范化",
        )
        pass_patch = SlotPatch(
            key="equipment_type",
            candidate_value="观察级ROV",
            raw_value="观察级ROV",
            confidence=0.95,
            source="user_input",
        )
        mut_patch = ListMutationPatch(
            field="payload",
            operation="add",
            items=("高清摄像机",),
            target_items=(),
            raw_text="配备高清摄像机",
            confidence=0.95,
            source="user_input",
        )
        norm_patch = NormalizedTaskPatch(
            schema_version=1,
            slot_outcomes=(outcome_succ, outcome_fail),
            passthrough_slot_updates=(pass_patch,),
            list_mutations=(mut_patch,),
            unresolved=("water_depth",),
        )

        plan = normalized_task_patch_to_apply_plan(norm_patch)

        self.assertIsInstance(plan, NormalizationApplyPlan)
        self.assertEqual(len(plan.successful_updates), 1)
        self.assertEqual(plan.successful_updates[0].key, "water_depth")
        self.assertEqual(plan.successful_updates[0].value, 300)
        self.assertEqual(plan.successful_updates[0].raw_value, "300米")
        self.assertEqual(plan.successful_updates[0].confidence, 1.0)

        self.assertEqual(len(plan.failures), 1)
        self.assertEqual(plan.failures[0].key, "support_vessel")
        self.assertFalse(plan.failures[0].success)

        self.assertEqual(plan.passthrough_slot_updates, (pass_patch,))
        self.assertEqual(plan.list_mutations, (mut_patch,))
        self.assertEqual(plan.unresolved, ("water_depth",))
        self.assertEqual(plan.normalized_schema_keys, frozenset({"water_depth", "support_vessel"}))

        # 验证输入对象未被修改
        self.assertEqual(len(norm_patch.slot_outcomes), 2)


class TestRuntimeSpecializedOwnership(unittest.TestCase):
    """验证 Runtime Specialized Passthrough 白名单。"""

    def test_runtime_ownership_constants(self):
        eq_keys = {
            "equipment_class",
            "equipment_family",
            "equipment_specification",
            "equipment_type",
            "equipment_name",
            "equipment_unit_id",
        }
        for k in eq_keys:
            self.assertIn(k, NORMALIZATION_RUNTIME_PASSTHROUGH_KEYS)

        # 控件与 entity linker intermediate 字段
        self.assertIn("emergency_mode", NORMALIZATION_RUNTIME_PASSTHROUGH_KEYS)
        self.assertIn("rov_description", NORMALIZATION_RUNTIME_PASSTHROUGH_KEYS)
        self.assertIn("raw_oilfield_name", NORMALIZATION_RUNTIME_PASSTHROUGH_KEYS)

        # Schema 字段不得存在于 passthrough allowlist
        for schema_key in ("water_depth", "start_time", "payload", "support_vessel", "cable_type"):
            self.assertNotIn(schema_key, NORMALIZATION_RUNTIME_PASSTHROUGH_KEYS)

        # Auto IDs 不得存在于 passthrough allowlist
        for auto_id in ("task_id", "intent_id", "internal_id"):
            self.assertNotIn(auto_id, NORMALIZATION_RUNTIME_PASSTHROUGH_KEYS)


class TestRuntimeV2Integration(unittest.TestCase):
    """验证 DM Stage2 中的 Normalization V2 Runtime Flow。"""

    def setUp(self):
        self.dm = DialogueManager(llm=MagicMock())

    def _setup_stage2_task(self, task_type_key: str = "pipeline_inspection"):
        self.dm.task_type_key = task_type_key
        slot = Slot(slot_name="task_type_key")
        slot.value = task_type_key
        slot.status = "valid"
        self.dm.slot_store.commit_transaction({"task_type_key": slot}, [])
        self.dm.intent_router.route = MagicMock(
            return_value=IntentRouteResult(
                interaction_type="WRITE",
                confidence=1.0,
                reason="test",
            )
        )

    def test_v2_runtime_number_success(self):
        self._setup_stage2_task("pipeline_inspection")

        extraction_res = {
            "slot_candidates": [
                {
                    "canonical_key": "water_depth",
                    "normalized_value": 300,
                    "raw_value": "300米",
                    "confidence": 1.0,
                    "resolution_method": "canonical_exact",
                }
            ],
            "list_mutations": [],
            "unresolved": [],
        }
        self.dm.extractor.extract_updates = MagicMock(return_value=extraction_res)

        with patch("src.dialogue_manager.is_task_patch_v2_enabled", return_value=True), \
             patch("src.dialogue_manager.is_normalization_contract_v2_enabled", return_value=True):
            self.dm.process("水深300米")

        depth_slot = self.dm.slot_store.slots.get("water_depth")
        self.assertIsNotNone(depth_slot)
        self.assertEqual(depth_slot.value, 300)
        self.assertEqual(depth_slot.status, "valid")
        self.assertEqual(depth_slot.raw_value, "300米")
        self.assertEqual(depth_slot.confidence, 1.0)
        self.assertEqual(depth_slot.source, "user_input")

    def test_v2_runtime_number_failure_old_valid_conflict(self):
        self._setup_stage2_task("pipeline_inspection")

        # 先设置旧 valid 水深 = 300
        old_slot = Slot(slot_name="water_depth")
        old_slot.value = 300
        old_slot.status = "valid"
        tt_slot = self.dm.slot_store.slots["task_type_key"]
        self.dm.slot_store.commit_transaction({"task_type_key": tt_slot, "water_depth": old_slot}, [])

        # 提取到非法 candidate "300abc"
        extraction_res = {
            "slot_candidates": [
                {
                    "canonical_key": "water_depth",
                    "normalized_value": "300abc",
                    "raw_value": "300abc",
                    "confidence": 0.9,
                    "resolution_method": "canonical_exact",
                }
            ],
            "list_mutations": [],
            "unresolved": [],
        }
        self.dm.extractor.extract_updates = MagicMock(return_value=extraction_res)

        with patch("src.dialogue_manager.is_task_patch_v2_enabled", return_value=True), \
             patch("src.dialogue_manager.is_normalization_contract_v2_enabled", return_value=True):
            self.dm.process("水深改为300abc")

        depth_slot = self.dm.slot_store.slots.get("water_depth")
        self.assertIsNotNone(depth_slot)
        self.assertEqual(depth_slot.status, "conflict")
        self.assertEqual(depth_slot.value, 300)  # 旧 valid 值保留
        self.assertEqual(depth_slot.candidate_value, "300abc")
        self.assertEqual(depth_slot.raw_value, "300abc")
        self.assertTrue(depth_slot.validation_error)

    def test_v2_runtime_number_failure_no_old_invalid(self):
        self._setup_stage2_task("pipeline_inspection")

        extraction_res = {
            "slot_candidates": [
                {
                    "canonical_key": "water_depth",
                    "normalized_value": "300abc",
                    "raw_value": "300abc",
                    "confidence": 0.9,
                    "resolution_method": "canonical_exact",
                }
            ],
            "list_mutations": [],
            "unresolved": [],
        }
        self.dm.extractor.extract_updates = MagicMock(return_value=extraction_res)

        with patch("src.dialogue_manager.is_task_patch_v2_enabled", return_value=True), \
             patch("src.dialogue_manager.is_normalization_contract_v2_enabled", return_value=True):
            self.dm.process("水深300abc")

        depth_slot = self.dm.slot_store.slots.get("water_depth")
        self.assertIsNotNone(depth_slot)
        self.assertEqual(depth_slot.status, "invalid")
        self.assertIsNone(depth_slot.value)
        self.assertEqual(depth_slot.candidate_value, "300abc")
        self.assertEqual(depth_slot.raw_value, "300abc")
        self.assertTrue(depth_slot.validation_error)

    def test_v2_runtime_equipment_passthrough_effect_parity(self):
        self._setup_stage2_task("pipeline_inspection")

        extraction_res = {
            "slot_candidates": [
                {
                    "canonical_key": "equipment_type",
                    "normalized_value": "观察级ROV",
                    "raw_value": "观察级ROV",
                    "confidence": 0.95,
                    "resolution_method": "canonical_exact",
                }
            ],
            "list_mutations": [],
            "unresolved": [],
        }
        self.dm.extractor.extract_updates = MagicMock(return_value=extraction_res)

        with patch("src.dialogue_manager.is_task_patch_v2_enabled", return_value=True), \
             patch("src.dialogue_manager.is_normalization_contract_v2_enabled", return_value=True):
            self.dm.process("使用观察级ROV")

        eq_slot = self.dm.slot_store.slots.get("equipment_type")
        self.assertIsNotNone(eq_slot)
        self.assertTrue(eq_slot.value is not None)
        self.assertEqual(eq_slot.status, "valid")

    def test_v2_runtime_payload_add_parity(self):
        self._setup_stage2_task("pipeline_inspection")

        extraction_res = {
            "slot_candidates": [],
            "list_mutations": [
                {
                    "field": "payload",
                    "operation": "add",
                    "items": ["高清摄像机"],
                    "target_items": [],
                    "raw_text": "配备高清摄像机",
                    "confidence": 0.95,
                    "source": "user_input",
                }
            ],
            "unresolved": [],
        }
        self.dm.extractor.extract_updates = MagicMock(return_value=extraction_res)

        with patch("src.dialogue_manager.is_task_patch_v2_enabled", return_value=True), \
             patch("src.dialogue_manager.is_normalization_contract_v2_enabled", return_value=True):
            self.dm.process("配备高清摄像机")

        payload_slot = self.dm.slot_store.slots.get("payload")
        self.assertIsNotNone(payload_slot)
        self.assertEqual(payload_slot.status, "valid")
        self.assertEqual(payload_slot.value, ["高清水下摄像机"])

    def test_v2_normalization_does_not_force_stage1_schema(self):
        # Stage 1 时 task_type_key 为 None，系统正常识别 task_type
        extraction_res = {
            "slot_candidates": [
                {
                    "canonical_key": "task_type",
                    "normalized_value": "管缆巡检",
                    "raw_value": "管缆巡检",
                    "confidence": 1.0,
                    "resolution_method": "canonical_exact",
                },
                {
                    "canonical_key": "task_type_key",
                    "normalized_value": "pipeline_inspection",
                    "raw_value": "pipeline_inspection",
                    "confidence": 1.0,
                    "resolution_method": "canonical_exact",
                },
            ],
            "list_mutations": [],
            "unresolved": [],
        }
        self.dm.extractor.extract_updates = MagicMock(return_value=extraction_res)
        self.dm.intent_router.route = MagicMock(
            return_value=IntentRouteResult(
                interaction_type="WRITE",
                confidence=1.0,
                reason="test",
            )
        )

        with patch("src.dialogue_manager.is_task_patch_v2_enabled", return_value=True), \
             patch("src.dialogue_manager.is_normalization_contract_v2_enabled", return_value=True):
            self.dm.process("我要进行管缆巡检作业")

        tt_slot = self.dm.slot_store.slots.get("task_type_key")
        self.assertIsNotNone(tt_slot)
        self.assertEqual(tt_slot.value, "pipeline_inspection")

    def test_v2_runtime_schema_field_normalized_once(self):
        self._setup_stage2_task("pipeline_inspection")

        extraction_res = {
            "slot_candidates": [
                {
                    "canonical_key": "water_depth",
                    "normalized_value": 300,
                    "raw_value": "300米",
                    "confidence": 1.0,
                    "resolution_method": "canonical_exact",
                }
            ],
            "list_mutations": [],
            "unresolved": [],
        }
        self.dm.extractor.extract_updates = MagicMock(return_value=extraction_res)

        with patch.object(FieldNormalizer, "normalize", wraps=self.dm.normalizer.normalize) as spy_normalize, \
             patch.object(FieldNormalizer, "normalize_updates_with_failures", wraps=self.dm.normalizer.normalize_updates_with_failures) as spy_legacy_norm, \
             patch("src.dialogue_manager.is_task_patch_v2_enabled", return_value=True), \
             patch("src.dialogue_manager.is_normalization_contract_v2_enabled", return_value=True):

            self.dm.process("水深300米")

            # 1. 验证 Legacy normalize_updates_with_failures 完全未被调用
            spy_legacy_norm.assert_not_called()

            # 2. 验证 FieldNormalizer.normalize 在整个 process 过程中只处理 water_depth 一次
            water_depth_calls = [
                c for c in spy_normalize.call_args_list
                if c[0] and c[0][0] in (300, "300米", "300")
            ]
            self.assertEqual(len(water_depth_calls), 1)


if __name__ == "__main__":
    unittest.main()
