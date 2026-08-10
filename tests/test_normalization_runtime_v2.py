"""test_normalization_runtime_v2.py — NormalizationContract V2 Runtime Integration Tests

验证 TaskPatch V2 + NormalizationContract V2 接入 DM WRITE Runtime 的 Flow、
Feature Flag Matrix (A/B/C/D 组合)、Apply Adapter、双重规范化防范与错误 Fail Closed 机制。
"""

from __future__ import annotations

import copy
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
            "equipment_type",
            "equipment_name",
            "equipment_unit_id",
        }
        for k in eq_keys:
            self.assertIn(k, NORMALIZATION_RUNTIME_PASSTHROUGH_KEYS)

        # Task type domain
        self.assertIn("task_type", NORMALIZATION_RUNTIME_PASSTHROUGH_KEYS)
        self.assertIn("task_type_key", NORMALIZATION_RUNTIME_PASSTHROUGH_KEYS)

        # Control与 Specialized Linker Input
        self.assertIn("emergency_mode", NORMALIZATION_RUNTIME_PASSTHROUGH_KEYS)
        self.assertIn("rov_description", NORMALIZATION_RUNTIME_PASSTHROUGH_KEYS)
        self.assertIn("oilfield_name", NORMALIZATION_RUNTIME_PASSTHROUGH_KEYS)

        # Downstream-only Linker 内部/输出状态不得在 TaskPatch passthrough allowlist 中
        self.assertNotIn("raw_oilfield_name", NORMALIZATION_RUNTIME_PASSTHROUGH_KEYS)
        self.assertNotIn("pending_oilfield_name", NORMALIZATION_RUNTIME_PASSTHROUGH_KEYS)
        self.assertNotIn("pending_oilfield_candidates", NORMALIZATION_RUNTIME_PASSTHROUGH_KEYS)

        # Schema 字段不得存在于 passthrough allowlist
        for schema_key in ("water_depth", "start_time", "payload", "support_vessel", "cable_type"):
            self.assertNotIn(schema_key, NORMALIZATION_RUNTIME_PASSTHROUGH_KEYS)

        # Auto / Upstream-Excluded IDs 不得存在于 passthrough allowlist
        for auto_id in ("task_id", "intent_id", "internal_id"):
            self.assertNotIn(auto_id, NORMALIZATION_RUNTIME_PASSTHROUGH_KEYS)


def get_effect_snapshot(dm: DialogueManager, keys: list[str]) -> dict[str, Any]:
    slots_info = {}
    for k in keys:
        s = dm.slot_store.slots.get(k)
        if s is None:
            slots_info[k] = None
        else:
            slots_info[k] = {
                "value": copy.deepcopy(s.value),
                "status": s.status,
                "candidate_value": copy.deepcopy(s.candidate_value),
                "raw_value": s.raw_value,
                "validation_error": s.validation_error,
                "confidence": s.confidence,
                "source": s.source,
            }
    tstate = copy.deepcopy(dm.task_state)
    if isinstance(tstate, dict):
        tstate.pop("internal_id", None)
        tstate.pop("updated_at", None)
    return {
        "version": dm.slot_store.version,
        "phase": dm.phase,
        "task_state": tstate,
        "slots": slots_info,
    }


class TestRuntimeV2Integration(unittest.TestCase):
    """验证 DM Stage2 中的 Normalization V2 Runtime Flow。"""

    def setUp(self):
        self.dm = DialogueManager(llm=MagicMock())

    def _setup_stage2_task(self, task_type_key: str = "pipeline_inspection", dm: DialogueManager | None = None):
        target_dm = dm if dm is not None else self.dm
        target_dm.task_type_key = task_type_key
        schema_cfg = target_dm.kb.task_schemas.get(task_type_key, {})
        task_types = schema_cfg.get("task_type_values", [])
        tt_val = task_types[0] if task_types else "管缆巡检"

        tt_slot = Slot(slot_name="task_type")
        tt_slot.value = tt_val
        tt_slot.status = "valid"
        ttk_slot = Slot(slot_name="task_type_key")
        ttk_slot.value = task_type_key
        ttk_slot.status = "valid"
        target_dm.slot_store.commit_transaction({"task_type": tt_slot, "task_type_key": ttk_slot}, [])
        target_dm.intent_router.route = MagicMock(
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
                    "normalized_value": "invalid_number",
                    "raw_value": "abc米",
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
            self.dm.process("水深abc米")

        depth_slot = self.dm.slot_store.slots.get("water_depth")
        self.assertIsNotNone(depth_slot)
        # 确认 old valid 值保持不变，状态变为 conflict
        self.assertEqual(depth_slot.value, 300)
        self.assertEqual(depth_slot.status, "conflict")
        self.assertEqual(depth_slot.candidate_value, "invalid_number")
        self.assertEqual(depth_slot.raw_value, "abc米")
        self.assertIsNotNone(depth_slot.validation_error)

    def test_v2_runtime_number_failure_no_old_invalid(self):
        self._setup_stage2_task("pipeline_inspection")

        extraction_res = {
            "slot_candidates": [
                {
                    "canonical_key": "water_depth",
                    "normalized_value": "invalid_number",
                    "raw_value": "abc米",
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
            self.dm.process("水深abc米")

        depth_slot = self.dm.slot_store.slots.get("water_depth")
        self.assertIsNotNone(depth_slot)
        # 确认无旧值时 value=None，status=invalid
        self.assertIsNone(depth_slot.value)
        self.assertEqual(depth_slot.status, "invalid")
        self.assertEqual(depth_slot.candidate_value, "invalid_number")
        self.assertEqual(depth_slot.raw_value, "abc米")
        self.assertIsNotNone(depth_slot.validation_error)

    def test_v2_runtime_payload_add_parity(self):
        self._setup_stage2_task("pipeline_inspection")
        p_slot = Slot(slot_name="payload", value_type="list")
        p_slot.value = []
        p_slot.status = "valid"
        ttk_slot = self.dm.slot_store.slots["task_type_key"]
        tt_slot = self.dm.slot_store.slots["task_type"]
        self.dm.slot_store.commit_transaction({"task_type_key": ttk_slot, "task_type": tt_slot, "payload": p_slot}, [])

        extraction_res = {
            "slot_candidates": [],
            "list_mutations": [
                {
                    "field": "payload",
                    "operation": "add",
                    "items": ["高清水下摄像机"],
                    "target_items": [],
                    "raw_text": "配备高清水下摄像机",
                    "confidence": 1.0,
                    "source": "user_input",
                }
            ],
            "unresolved": [],
        }
        self.dm.extractor.extract_updates = MagicMock(return_value=extraction_res)

        with patch("src.dialogue_manager.is_task_patch_v2_enabled", return_value=True), \
             patch("src.dialogue_manager.is_normalization_contract_v2_enabled", return_value=True):
            self.dm.process("配备高清水下摄像机")

        payload_slot = self.dm.slot_store.slots.get("payload")
        self.assertIsNotNone(payload_slot)
        self.assertIn("高清水下摄像机", payload_slot.value)
        self.assertEqual(payload_slot.status, "valid")

    def test_v2_normalization_does_not_force_stage1_schema(self):
        # 验证 normalize_task_patch 在 Stage 2 可以正常处理 stage1_schema_keys 以外的 schema 字段
        self._setup_stage2_task("pipeline_inspection")

        extraction_res = {
            "slot_candidates": [
                {
                    "canonical_key": "water_depth",
                    "normalized_value": 500,
                    "raw_value": "500米",
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
            self.dm.process("水深500米")

        depth_slot = self.dm.slot_store.slots.get("water_depth")
        self.assertIsNotNone(depth_slot)
        self.assertEqual(depth_slot.value, 500)

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

        real_fn_normalize = FieldNormalizer().normalize

        with patch.object(self.dm.normalizer, "normalize_updates_with_failures", wraps=self.dm.normalizer.normalize_updates_with_failures) as spy_legacy_norm, \
             patch.object(FieldNormalizer, "normalize", side_effect=real_fn_normalize) as spy_normalize, \
             patch("src.dialogue_manager.is_task_patch_v2_enabled", return_value=True), \
             patch("src.dialogue_manager.is_normalization_contract_v2_enabled", return_value=True):

            self.dm.process("水深300米")

            # 1. 验证 Legacy normalize_updates_with_failures 未被触发
            spy_legacy_norm.assert_not_called()

            # 2. 验证 FieldNormalizer.normalize 在整个 process 过程中只处理 water_depth 一次
            water_depth_calls = [
                c for c in spy_normalize.call_args_list
                if c[0] and c[0][0] in (300, "300米", "300")
            ]
            self.assertEqual(len(water_depth_calls), 1)

    def test_v2_stage2_task_type_key_reaches_special_handler(self):
        self._setup_stage2_task("pipeline_inspection")

        extraction_res = {
            "slot_candidates": [
                {
                    "canonical_key": "task_type_key",
                    "normalized_value": "pipeline_inspection",
                    "raw_value": "pipeline_inspection",
                    "confidence": 1.0,
                    "resolution_method": "canonical_exact",
                }
            ],
            "list_mutations": [],
            "unresolved": [],
        }
        self.dm.extractor.extract_updates = MagicMock(return_value=extraction_res)

        with patch.object(DialogueManager, "_handle_task_type_update_in_transaction", wraps=self.dm._handle_task_type_update_in_transaction) as spy_tt, \
             patch("src.dialogue_manager.is_task_patch_v2_enabled", return_value=True), \
             patch("src.dialogue_manager.is_normalization_contract_v2_enabled", return_value=True):
            self.dm.process("执行管缆巡检")

            self.assertTrue(spy_tt.called)
            called_keys = [c[0][0] for c in spy_tt.call_args_list]
            self.assertIn("task_type_key", called_keys)
            ttk_calls = [c[0][1] for c in spy_tt.call_args_list if c[0][0] == "task_type_key"]
            self.assertEqual(ttk_calls[0], "pipeline_inspection")

    def test_v2_stage2_task_type_key_not_silently_dropped(self):
        self.dm.intent_router.route = MagicMock(
            return_value=IntentRouteResult(
                interaction_type="WRITE",
                confidence=1.0,
                reason="test",
            )
        )

        extraction_res = {
            "slot_candidates": [
                {
                    "canonical_key": "task_type_key",
                    "normalized_value": "pipeline_inspection",
                    "raw_value": "pipeline_inspection",
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
            self.dm.process("任务类型是管缆巡检")

        ttk_slot = self.dm.slot_store.slots.get("task_type_key")
        self.assertIsNotNone(ttk_slot)
        self.assertEqual(ttk_slot.value, "pipeline_inspection")
        self.assertEqual(ttk_slot.status, "valid")

    def test_v2_stage2_task_type_pair_uses_special_handler(self):
        self._setup_stage2_task("pipeline_inspection")

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

        with patch.object(DialogueManager, "_handle_task_type_update_in_transaction", wraps=self.dm._handle_task_type_update_in_transaction) as spy_tt, \
             patch("src.dialogue_manager.is_task_patch_v2_enabled", return_value=True), \
             patch("src.dialogue_manager.is_normalization_contract_v2_enabled", return_value=True):
            self.dm.process("修改为管缆巡检作业")

            self.assertTrue(spy_tt.called)

    def test_v2_task_type_category_lock_effect_parity(self):
        extraction_res = {
            "slot_candidates": [
                {
                    "canonical_key": "task_type",
                    "normalized_value": "采油树控制面板插入",
                    "raw_value": "采油树控制面板插入",
                    "confidence": 1.0,
                    "resolution_method": "canonical_exact",
                },
                {
                    "canonical_key": "task_type_key",
                    "normalized_value": "tree_valve_operation",
                    "raw_value": "tree_valve_operation",
                    "confidence": 1.0,
                    "resolution_method": "canonical_exact",
                },
            ],
            "list_mutations": [],
            "unresolved": [],
        }

        snapshots = {}
        for mode_name, flag_v2, flag_norm in [
            ("Legacy", False, False),
            ("G2.1", True, False),
            ("V2", True, True),
        ]:
            dm = DialogueManager(llm=MagicMock())
            self._setup_stage2_task("pipeline_inspection", dm=dm)
            tid_slot = Slot(slot_name="task_id")
            tid_slot.value = "PI-20260803-001"
            tid_slot.status = "valid"
            ttk_slot = dm.slot_store.slots["task_type_key"]
            tt_slot = dm.slot_store.slots["task_type"]
            dm.slot_store.commit_transaction(
                {"task_type": tt_slot, "task_type_key": ttk_slot, "task_id": tid_slot}, []
            )
            dm.intent_router.route = MagicMock(
                return_value=IntentRouteResult(
                    interaction_type="WRITE",
                    confidence=1.0,
                    reason="test",
                )
            )
            dm.extractor.extract_updates = MagicMock(return_value=extraction_res)

            with patch.object(DialogueManager, "_handle_task_type_update_in_transaction", wraps=dm._handle_task_type_update_in_transaction) as spy_tt, \
                 patch("src.dialogue_manager.is_task_patch_v2_enabled", return_value=flag_v2), \
                 patch("src.dialogue_manager.is_normalization_contract_v2_enabled", return_value=flag_norm):
                dm.process("改成采油树控制面板插入")
                if mode_name == "V2":
                    self.assertTrue(spy_tt.called, "V2 mode must actually call _handle_task_type_update_in_transaction to trigger category lock")

            snapshots[mode_name] = get_effect_snapshot(dm, ["task_type", "task_type_key", "task_id"])

        self.assertEqual(snapshots["V2"]["slots"]["task_type_key"]["value"], "pipeline_inspection")
        self.assertEqual(snapshots["V2"]["slots"]["task_type"]["value"], "管缆巡检")
        self.assertEqual(snapshots["G2.1"]["slots"]["task_type_key"]["value"], "pipeline_inspection")
        self.assertEqual(snapshots["Legacy"]["slots"]["task_type_key"]["value"], "pipeline_inspection")

    def test_v2_equipment_handler_called_exactly_once(self):
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

        with patch.object(DialogueManager, "_handle_equipment_updates_in_transaction", wraps=self.dm._handle_equipment_updates_in_transaction) as spy_eq, \
             patch("src.dialogue_manager.is_task_patch_v2_enabled", return_value=True), \
             patch("src.dialogue_manager.is_normalization_contract_v2_enabled", return_value=True):

            self.dm.process("使用观察级ROV")

            self.assertEqual(spy_eq.call_count, 1)

    def test_v2_equipment_hierarchy_full_effect_parity(self):
        extraction_res = {
            "slot_candidates": [
                {
                    "canonical_key": "equipment_type",
                    "normalized_value": "观察级ROV",
                    "raw_value": "观察级ROV",
                    "confidence": 0.95,
                    "resolution_method": "canonical_exact",
                },
            ],
            "list_mutations": [],
            "unresolved": [],
        }

        eq_keys = ["equipment_class", "equipment_family", "equipment_specification", "equipment_type", "equipment_name", "equipment_unit_id"]
        snapshots = {}
        for mode_name, flag_v2, flag_norm in [
            ("Legacy", False, False),
            ("G2.1", True, False),
            ("V2", True, True),
        ]:
            dm = DialogueManager(llm=MagicMock())
            self._setup_stage2_task("pipeline_inspection", dm=dm)
            dm.extractor.extract_updates = MagicMock(return_value=extraction_res)

            with patch("src.dialogue_manager.is_task_patch_v2_enabled", return_value=flag_v2), \
                 patch("src.dialogue_manager.is_normalization_contract_v2_enabled", return_value=flag_norm):
                dm.process("配备观察级ROV")

            snapshots[mode_name] = get_effect_snapshot(dm, eq_keys)

        self.assertEqual(snapshots["Legacy"], snapshots["G2.1"])
        self.assertEqual(snapshots["Legacy"], snapshots["V2"])
        self.assertEqual(snapshots["V2"]["slots"]["equipment_type"]["status"], "valid")

    def test_v2_real_oilfield_schema_accepted_parity(self):
        extraction_res = {
            "slot_candidates": [
                {
                    "canonical_key": "oilfield_name",
                    "normalized_value": "临水17-2",
                    "raw_value": "临水17-2",
                    "confidence": 1.0,
                    "resolution_method": "canonical_exact",
                }
            ],
            "list_mutations": [],
            "unresolved": [],
        }

        oil_keys = ["oilfield_name", "oilfield_entity_id", "raw_oilfield_name", "pending_oilfield_name", "pending_oilfield_candidates"]
        snapshots = {}
        for mode_name, flag_v2, flag_norm in [
            ("Legacy", False, False),
            ("G2.1", True, False),
            ("V2", True, True),
        ]:
            dm = DialogueManager(llm=MagicMock())
            self._setup_stage2_task("tree_valve_operation", dm=dm)
            dm.extractor.extract_updates = MagicMock(return_value=extraction_res)

            with patch("src.dialogue_manager.is_task_patch_v2_enabled", return_value=flag_v2), \
                 patch("src.dialogue_manager.is_normalization_contract_v2_enabled", return_value=flag_norm):
                dm.process("作业在临水17-2")

            snapshots[mode_name] = get_effect_snapshot(dm, oil_keys)

        self.assertEqual(snapshots["Legacy"], snapshots["G2.1"])
        self.assertEqual(snapshots["Legacy"], snapshots["V2"])
        self.assertEqual(snapshots["V2"]["slots"]["oilfield_name"]["value"], "陵水17-2气田")
        self.assertEqual(snapshots["V2"]["slots"]["oilfield_name"]["status"], "valid")

    def test_v2_real_oilfield_schema_pending_parity(self):
        extraction_res = {
            "slot_candidates": [
                {
                    "canonical_key": "oilfield_name",
                    "normalized_value": "南海",
                    "raw_value": "南海",
                    "confidence": 0.8,
                    "resolution_method": "canonical_exact",
                }
            ],
            "list_mutations": [],
            "unresolved": [],
        }

        oil_keys = ["oilfield_name", "oilfield_entity_id", "raw_oilfield_name", "pending_oilfield_name", "pending_oilfield_candidates"]
        snapshots = {}
        for mode_name, flag_v2, flag_norm in [
            ("Legacy", False, False),
            ("G2.1", True, False),
            ("V2", True, True),
        ]:
            dm = DialogueManager(llm=MagicMock())
            self._setup_stage2_task("tree_valve_operation", dm=dm)
            of_slot = Slot("oilfield_name")
            of_slot.value = "陵水17-2气田"
            of_slot.status = "valid"
            eid_slot = Slot("oilfield_entity_id")
            eid_slot.value = "LS17-2"
            eid_slot.status = "valid"
            ttk_slot = dm.slot_store.slots["task_type_key"]
            tt_slot = dm.slot_store.slots["task_type"]
            dm.slot_store.commit_transaction(
                {"task_type_key": ttk_slot, "task_type": tt_slot, "oilfield_name": of_slot, "oilfield_entity_id": eid_slot}, []
            )

            dm.extractor.extract_updates = MagicMock(return_value=extraction_res)

            with patch("src.dialogue_manager.is_task_patch_v2_enabled", return_value=flag_v2), \
                 patch("src.dialogue_manager.is_normalization_contract_v2_enabled", return_value=flag_norm):
                dm.process("作业在南海油田")

            snapshots[mode_name] = get_effect_snapshot(dm, oil_keys)

        self.assertEqual(snapshots["Legacy"], snapshots["G2.1"])
        self.assertEqual(snapshots["Legacy"], snapshots["V2"])
        self.assertEqual(snapshots["V2"]["slots"]["pending_oilfield_name"]["value"], "南海")
        self.assertEqual(snapshots["V2"]["slots"]["pending_oilfield_name"]["status"], "valid")
        self.assertIsNone(snapshots["V2"]["slots"]["oilfield_name"]["value"])
        self.assertEqual(snapshots["V2"]["slots"]["oilfield_name"]["status"], "missing")

    def test_v2_oilfield_removed_key_not_reapplied(self):
        self._setup_stage2_task("tree_valve_operation")

        extraction_res = {
            "slot_candidates": [
                {
                    "canonical_key": "oilfield_name",
                    "normalized_value": "南海",
                    "raw_value": "南海",
                    "confidence": 0.8,
                    "resolution_method": "canonical_exact",
                }
            ],
            "list_mutations": [],
            "unresolved": [],
        }
        self.dm.extractor.extract_updates = MagicMock(return_value=extraction_res)

        with patch("src.dialogue_manager.is_task_patch_v2_enabled", return_value=True), \
             patch("src.dialogue_manager.is_normalization_contract_v2_enabled", return_value=True):
            self.dm.process("作业在南海油田")

        of_slot = self.dm.slot_store.slots.get("oilfield_name")
        self.assertTrue(of_slot is None or of_slot.value is None)
        if of_slot is not None:
            self.assertEqual(of_slot.status, "missing")

    def test_v2_oilfield_clear_directive_reaches_apply_seam(self):
        self._setup_stage2_task("tree_valve_operation")

        of_slot = Slot("oilfield_name")
        of_slot.value = "陵水17-2气田"
        of_slot.status = "valid"
        ttk_slot = self.dm.slot_store.slots["task_type_key"]
        tt_slot = self.dm.slot_store.slots["task_type"]
        self.dm.slot_store.commit_transaction(
            {"task_type_key": ttk_slot, "task_type": tt_slot, "oilfield_name": of_slot}, []
        )

        extraction_res = {
            "slot_candidates": [
                {
                    "canonical_key": "oilfield_name",
                    "normalized_value": "南海",
                    "raw_value": "南海",
                    "confidence": 0.8,
                    "resolution_method": "canonical_exact",
                }
            ],
            "list_mutations": [],
            "unresolved": [],
        }
        self.dm.extractor.extract_updates = MagicMock(return_value=extraction_res)

        with patch.object(DialogueManager, "_apply_updates_in_transaction", wraps=self.dm._apply_updates_in_transaction) as spy_apply, \
             patch("src.dialogue_manager.is_task_patch_v2_enabled", return_value=True), \
             patch("src.dialogue_manager.is_normalization_contract_v2_enabled", return_value=True):
            self.dm.process("作业在南海油田")

            apply_calls = spy_apply.call_args_list
            has_clear = any(
                isinstance(c[0][0], dict) and "__clear_oilfield_name" in c[0][0]
                for c in apply_calls
            )
            self.assertTrue(has_clear, "__clear_oilfield_name directive must reach _apply_updates_in_transaction")

    def test_v2_datetime_success_parity(self):
        res_candidates = [
            {
                "canonical_key": "start_time",
                "normalized_value": "2026-08-10T08:00:00",
                "raw_value": "2026-08-10 08:00:00",
                "confidence": 1.0,
                "resolution_method": "canonical_exact",
            }
        ]
        snapshots = {}
        for mode_name, flag_v2, flag_norm in [("Legacy", False, False), ("G2.1", True, False), ("V2", True, True)]:
            dm = DialogueManager(llm=MagicMock())
            self._setup_stage2_task("pipeline_inspection", dm=dm)
            res = {"slot_candidates": res_candidates, "list_mutations": [], "unresolved": []}
            dm.extractor.extract_updates = MagicMock(return_value=res)
            with patch("src.dialogue_manager.is_task_patch_v2_enabled", return_value=flag_v2), \
                 patch("src.dialogue_manager.is_normalization_contract_v2_enabled", return_value=flag_norm):
                dm.process("开始时间2026-08-10 08:00:00")
            snapshots[mode_name] = get_effect_snapshot(dm, ["start_time"])

        self.assertEqual(snapshots["Legacy"], snapshots["G2.1"])
        self.assertEqual(snapshots["Legacy"], snapshots["V2"])
        self.assertEqual(snapshots["V2"]["slots"]["start_time"]["value"], "2026-08-10T08:00:00")
        self.assertEqual(snapshots["V2"]["slots"]["start_time"]["status"], "valid")

    def test_v2_datetime_failure_parity(self):
        res_candidates = [
            {
                "canonical_key": "start_time",
                "normalized_value": "invalid_datetime",
                "raw_value": "非法时间表达",
                "confidence": 1.0,
                "resolution_method": "canonical_exact",
            }
        ]
        # Case 1: Failure with old valid value
        snaps_old_valid = {}
        for mode_name, flag_v2, flag_norm in [("Legacy", False, False), ("G2.1", True, False), ("V2", True, True)]:
            dm = DialogueManager(llm=MagicMock())
            self._setup_stage2_task("pipeline_inspection", dm=dm)
            st_slot = Slot("start_time")
            st_slot.value = "2026-08-10T08:00:00"
            st_slot.status = "valid"
            ttk_slot = dm.slot_store.slots["task_type_key"]
            tt_slot = dm.slot_store.slots["task_type"]
            dm.slot_store.commit_transaction({"task_type": tt_slot, "task_type_key": ttk_slot, "start_time": st_slot}, [])

            res = {"slot_candidates": res_candidates, "list_mutations": [], "unresolved": []}
            dm.extractor.extract_updates = MagicMock(return_value=res)
            with patch("src.dialogue_manager.is_task_patch_v2_enabled", return_value=flag_v2), \
                 patch("src.dialogue_manager.is_normalization_contract_v2_enabled", return_value=flag_norm):
                dm.process("开始时间非法时间表达")
            snaps_old_valid[mode_name] = get_effect_snapshot(dm, ["start_time"])

        self.assertEqual(snaps_old_valid["Legacy"], snaps_old_valid["G2.1"])
        self.assertEqual(snaps_old_valid["Legacy"], snaps_old_valid["V2"])
        self.assertEqual(snaps_old_valid["V2"]["slots"]["start_time"]["value"], "2026-08-10T08:00:00")
        self.assertEqual(snaps_old_valid["V2"]["slots"]["start_time"]["status"], "conflict")
        self.assertEqual(snaps_old_valid["V2"]["slots"]["start_time"]["candidate_value"], "invalid_datetime")
        self.assertIsNotNone(snaps_old_valid["V2"]["slots"]["start_time"]["validation_error"])

        # Case 2: Failure without old valid value
        snaps_no_old = {}
        for mode_name, flag_v2, flag_norm in [("Legacy", False, False), ("G2.1", True, False), ("V2", True, True)]:
            dm = DialogueManager(llm=MagicMock())
            self._setup_stage2_task("pipeline_inspection", dm=dm)
            res = {"slot_candidates": res_candidates, "list_mutations": [], "unresolved": []}
            dm.extractor.extract_updates = MagicMock(return_value=res)
            with patch("src.dialogue_manager.is_task_patch_v2_enabled", return_value=flag_v2), \
                 patch("src.dialogue_manager.is_normalization_contract_v2_enabled", return_value=flag_norm):
                dm.process("开始时间非法时间表达")
            snaps_no_old[mode_name] = get_effect_snapshot(dm, ["start_time"])

        self.assertEqual(snaps_no_old["Legacy"], snaps_no_old["G2.1"])
        self.assertEqual(snaps_no_old["Legacy"], snaps_no_old["V2"])
        self.assertIsNone(snaps_no_old["V2"]["slots"]["start_time"]["value"])
        self.assertEqual(snaps_no_old["V2"]["slots"]["start_time"]["status"], "invalid")
        self.assertEqual(snaps_no_old["V2"]["slots"]["start_time"]["candidate_value"], "invalid_datetime")

    def test_v2_coord_success_parity(self):
        coord_val = {"lat": 20.0, "lon": 110.0}
        res_candidates = [
            {
                "canonical_key": "start_point",
                "normalized_value": coord_val,
                "raw_value": "北纬20度东经110度",
                "confidence": 1.0,
                "resolution_method": "canonical_exact",
            }
        ]
        snapshots = {}
        for mode_name, flag_v2, flag_norm in [("Legacy", False, False), ("G2.1", True, False), ("V2", True, True)]:
            dm = DialogueManager(llm=MagicMock())
            self._setup_stage2_task("pipeline_inspection", dm=dm)
            res = {"slot_candidates": res_candidates, "list_mutations": [], "unresolved": []}
            dm.extractor.extract_updates = MagicMock(return_value=res)
            with patch("src.dialogue_manager.is_task_patch_v2_enabled", return_value=flag_v2), \
                 patch("src.dialogue_manager.is_normalization_contract_v2_enabled", return_value=flag_norm):
                dm.process("起始点北纬20度东经110度")
            snapshots[mode_name] = get_effect_snapshot(dm, ["start_point"])

        self.assertEqual(snapshots["Legacy"], snapshots["G2.1"])
        self.assertEqual(snapshots["Legacy"], snapshots["V2"])
        self.assertEqual(snapshots["V2"]["slots"]["start_point"]["value"], coord_val)
        self.assertEqual(snapshots["V2"]["slots"]["start_point"]["status"], "valid")

    def test_v2_coord_failure_parity(self):
        res_candidates = [
            {
                "canonical_key": "start_point",
                "normalized_value": "invalid_coord",
                "raw_value": "非法坐标表达",
                "confidence": 1.0,
                "resolution_method": "canonical_exact",
            }
        ]
        # Case 1: Failure with old valid value
        snaps_old_valid = {}
        for mode_name, flag_v2, flag_norm in [("Legacy", False, False), ("G2.1", True, False), ("V2", True, True)]:
            dm = DialogueManager(llm=MagicMock())
            self._setup_stage2_task("pipeline_inspection", dm=dm)
            sp_slot = Slot("start_point")
            sp_slot.value = {"lat": 10.0, "lon": 100.0}
            sp_slot.status = "valid"
            ttk_slot = dm.slot_store.slots["task_type_key"]
            tt_slot = dm.slot_store.slots["task_type"]
            dm.slot_store.commit_transaction({"task_type": tt_slot, "task_type_key": ttk_slot, "start_point": sp_slot}, [])

            res = {"slot_candidates": res_candidates, "list_mutations": [], "unresolved": []}
            dm.extractor.extract_updates = MagicMock(return_value=res)
            with patch("src.dialogue_manager.is_task_patch_v2_enabled", return_value=flag_v2), \
                 patch("src.dialogue_manager.is_normalization_contract_v2_enabled", return_value=flag_norm):
                dm.process("起始点非法坐标表达")
            snaps_old_valid[mode_name] = get_effect_snapshot(dm, ["start_point"])

        self.assertEqual(snaps_old_valid["Legacy"], snaps_old_valid["G2.1"])
        self.assertEqual(snaps_old_valid["Legacy"], snaps_old_valid["V2"])
        self.assertEqual(snaps_old_valid["V2"]["slots"]["start_point"]["value"], {"lat": 10.0, "lon": 100.0})
        self.assertEqual(snaps_old_valid["V2"]["slots"]["start_point"]["status"], "conflict")
        self.assertEqual(snaps_old_valid["V2"]["slots"]["start_point"]["candidate_value"], "invalid_coord")

        # Case 2: Failure without old valid value
        snaps_no_old = {}
        for mode_name, flag_v2, flag_norm in [("Legacy", False, False), ("G2.1", True, False), ("V2", True, True)]:
            dm = DialogueManager(llm=MagicMock())
            self._setup_stage2_task("pipeline_inspection", dm=dm)
            res = {"slot_candidates": res_candidates, "list_mutations": [], "unresolved": []}
            dm.extractor.extract_updates = MagicMock(return_value=res)
            with patch("src.dialogue_manager.is_task_patch_v2_enabled", return_value=flag_v2), \
                 patch("src.dialogue_manager.is_normalization_contract_v2_enabled", return_value=flag_norm):
                dm.process("起始点非法坐标表达")
            snaps_no_old[mode_name] = get_effect_snapshot(dm, ["start_point"])

        self.assertEqual(snaps_no_old["Legacy"], snaps_no_old["G2.1"])
        self.assertEqual(snaps_no_old["Legacy"], snaps_no_old["V2"])
        self.assertIsNone(snaps_no_old["V2"]["slots"]["start_point"]["value"])
        self.assertEqual(snaps_no_old["V2"]["slots"]["start_point"]["status"], "invalid")

    def test_v2_enum_success_parity(self):
        res_candidates = [
            {
                "canonical_key": "cable_type",
                "normalized_value": "海底油气管道",
                "raw_value": "海底油气管道",
                "confidence": 1.0,
                "resolution_method": "canonical_exact",
            }
        ]
        snapshots = {}
        for mode_name, flag_v2, flag_norm in [("Legacy", False, False), ("G2.1", True, False), ("V2", True, True)]:
            dm = DialogueManager(llm=MagicMock())
            self._setup_stage2_task("pipeline_inspection", dm=dm)
            res = {"slot_candidates": res_candidates, "list_mutations": [], "unresolved": []}
            dm.extractor.extract_updates = MagicMock(return_value=res)
            with patch("src.dialogue_manager.is_task_patch_v2_enabled", return_value=flag_v2), \
                 patch("src.dialogue_manager.is_normalization_contract_v2_enabled", return_value=flag_norm):
                dm.process("管缆类型海底油气管道")
            snapshots[mode_name] = get_effect_snapshot(dm, ["cable_type"])

        self.assertEqual(snapshots["Legacy"], snapshots["G2.1"])
        self.assertEqual(snapshots["Legacy"], snapshots["V2"])
        self.assertEqual(snapshots["V2"]["slots"]["cable_type"]["value"], "海底油气管道")
        self.assertEqual(snapshots["V2"]["slots"]["cable_type"]["status"], "valid")

    def test_v2_enum_failure_parity(self):
        res_candidates = [
            {
                "canonical_key": "support_vessel",
                "normalized_value": "invalid_enum",
                "raw_value": "魔幻飞船",
                "confidence": 1.0,
                "resolution_method": "canonical_exact",
            }
        ]
        # Case 1: Failure with old valid value
        snaps_old_valid = {}
        for mode_name, flag_v2, flag_norm in [("Legacy", False, False), ("G2.1", True, False), ("V2", True, True)]:
            dm = DialogueManager(llm=MagicMock())
            self._setup_stage2_task("pipeline_inspection", dm=dm)
            sv_slot = Slot("support_vessel")
            sv_slot.value = "VSL-001"
            sv_slot.status = "valid"
            ttk_slot = dm.slot_store.slots["task_type_key"]
            tt_slot = dm.slot_store.slots["task_type"]
            dm.slot_store.commit_transaction({"task_type": tt_slot, "task_type_key": ttk_slot, "support_vessel": sv_slot}, [])

            res = {"slot_candidates": res_candidates, "list_mutations": [], "unresolved": []}
            dm.extractor.extract_updates = MagicMock(return_value=res)
            with patch("src.dialogue_manager.is_task_patch_v2_enabled", return_value=flag_v2), \
                 patch("src.dialogue_manager.is_normalization_contract_v2_enabled", return_value=flag_norm):
                dm.process("支持船魔幻飞船")
            snaps_old_valid[mode_name] = get_effect_snapshot(dm, ["support_vessel"])

        self.assertEqual(snaps_old_valid["Legacy"], snaps_old_valid["G2.1"])
        self.assertEqual(snaps_old_valid["Legacy"], snaps_old_valid["V2"])
        self.assertEqual(snaps_old_valid["V2"]["slots"]["support_vessel"]["value"], "VSL-001")
        self.assertEqual(snaps_old_valid["V2"]["slots"]["support_vessel"]["status"], "conflict")

        # Case 2: Failure without old valid value
        snaps_no_old = {}
        for mode_name, flag_v2, flag_norm in [("Legacy", False, False), ("G2.1", True, False), ("V2", True, True)]:
            dm = DialogueManager(llm=MagicMock())
            self._setup_stage2_task("pipeline_inspection", dm=dm)
            res = {"slot_candidates": res_candidates, "list_mutations": [], "unresolved": []}
            dm.extractor.extract_updates = MagicMock(return_value=res)
            with patch("src.dialogue_manager.is_task_patch_v2_enabled", return_value=flag_v2), \
                 patch("src.dialogue_manager.is_normalization_contract_v2_enabled", return_value=flag_norm):
                dm.process("支持船魔幻飞船")
            snaps_no_old[mode_name] = get_effect_snapshot(dm, ["support_vessel"])

        self.assertEqual(snaps_no_old["Legacy"], snaps_no_old["G2.1"])
        self.assertEqual(snaps_no_old["Legacy"], snaps_no_old["V2"])
        self.assertIsNone(snaps_no_old["V2"]["slots"]["support_vessel"]["value"])
        self.assertEqual(snaps_no_old["V2"]["slots"]["support_vessel"]["status"], "invalid")

    def test_v2_direct_list_runtime_na_documentation(self):
        # 证明 schema 中所有 list 槽位（如 payload）在真实架构中全由 ListMutationPatch 驱动
        fields = self.dm.builder.get_schema("pipeline_inspection", "normal")
        list_fields = [
            f["key"] for f in fields
            if f.get("type") == "list"
        ]
        self.assertEqual(list_fields, ["payload"])
        # N/A 结论：不存在直接 SlotPatch 独立操作 list 槽位的 Runtime 路径，SSOT 为 ListMutationPatch

    def test_v2_payload_remove_parity(self):
        list_muts = [
            {
                "field": "payload",
                "operation": "remove",
                "items": ["LED水下照明灯"],
                "target_items": [],
                "raw_text": "移除LED水下照明灯",
                "confidence": 1.0,
                "source": "user_input",
            }
        ]
        snapshots = {}
        for mode_name, flag_v2, flag_norm in [("Legacy", False, False), ("G2.1", True, False), ("V2", True, True)]:
            dm = DialogueManager(llm=MagicMock())
            self._setup_stage2_task("pipeline_inspection", dm=dm)
            p_slot = Slot("payload", value_type="list")
            p_slot.value = ["高清水下摄像机", "LED水下照明灯"]
            p_slot.status = "valid"
            ttk_slot = dm.slot_store.slots["task_type_key"]
            tt_slot = dm.slot_store.slots["task_type"]
            dm.slot_store.commit_transaction({"task_type": tt_slot, "task_type_key": ttk_slot, "payload": p_slot}, [])

            res = {"slot_candidates": [], "list_mutations": list_muts, "unresolved": []}
            dm.extractor.extract_updates = MagicMock(return_value=res)
            with patch("src.dialogue_manager.is_task_patch_v2_enabled", return_value=flag_v2), \
                 patch("src.dialogue_manager.is_normalization_contract_v2_enabled", return_value=flag_norm):
                dm.process("移除LED水下照明灯")
            snapshots[mode_name] = get_effect_snapshot(dm, ["payload"])

        self.assertEqual(snapshots["Legacy"], snapshots["G2.1"])
        self.assertEqual(snapshots["Legacy"], snapshots["V2"])
        self.assertEqual(snapshots["V2"]["slots"]["payload"]["value"], ["高清水下摄像机"])
        self.assertEqual(snapshots["V2"]["slots"]["payload"]["status"], "valid")

    def test_v2_payload_replace_parity(self):
        list_muts = [
            {
                "field": "payload",
                "operation": "replace",
                "items": ["成像声呐"],
                "target_items": ["高清水下摄像机"],
                "raw_text": "将高清水下摄像机替换为成像声呐",
                "confidence": 1.0,
                "source": "user_input",
            }
        ]
        snapshots = {}
        for mode_name, flag_v2, flag_norm in [("Legacy", False, False), ("G2.1", True, False), ("V2", True, True)]:
            dm = DialogueManager(llm=MagicMock())
            self._setup_stage2_task("pipeline_inspection", dm=dm)
            p_slot = Slot("payload", value_type="list")
            p_slot.value = ["高清水下摄像机"]
            p_slot.status = "valid"
            ttk_slot = dm.slot_store.slots["task_type_key"]
            tt_slot = dm.slot_store.slots["task_type"]
            dm.slot_store.commit_transaction({"task_type": tt_slot, "task_type_key": ttk_slot, "payload": p_slot}, [])

            res = {"slot_candidates": [], "list_mutations": list_muts, "unresolved": []}
            dm.extractor.extract_updates = MagicMock(return_value=res)
            with patch("src.dialogue_manager.is_task_patch_v2_enabled", return_value=flag_v2), \
                 patch("src.dialogue_manager.is_normalization_contract_v2_enabled", return_value=flag_norm):
                dm.process("将高清水下摄像机替换为成像声呐")
            snapshots[mode_name] = get_effect_snapshot(dm, ["payload"])

        self.assertEqual(snapshots["Legacy"], snapshots["G2.1"])
        self.assertEqual(snapshots["Legacy"], snapshots["V2"])
        self.assertEqual(snapshots["V2"]["slots"]["payload"]["value"], ["成像声呐"])
        self.assertEqual(snapshots["V2"]["slots"]["payload"]["status"], "valid")

    def test_v2_payload_clear_parity(self):
        list_muts = [
            {
                "field": "payload",
                "operation": "clear",
                "items": [],
                "target_items": [],
                "raw_text": "清空所有工具",
                "confidence": 1.0,
                "source": "user_input",
            }
        ]
        snapshots = {}
        for mode_name, flag_v2, flag_norm in [("Legacy", False, False), ("G2.1", True, False), ("V2", True, True)]:
            dm = DialogueManager(llm=MagicMock())
            self._setup_stage2_task("pipeline_inspection", dm=dm)
            p_slot = Slot("payload", value_type="list")
            p_slot.value = ["高清水下摄像机", "成像声呐"]
            p_slot.status = "valid"
            ttk_slot = dm.slot_store.slots["task_type_key"]
            tt_slot = dm.slot_store.slots["task_type"]
            dm.slot_store.commit_transaction({"task_type": tt_slot, "task_type_key": ttk_slot, "payload": p_slot}, [])

            res = {"slot_candidates": [], "list_mutations": list_muts, "unresolved": []}
            dm.extractor.extract_updates = MagicMock(return_value=res)
            with patch("src.dialogue_manager.is_task_patch_v2_enabled", return_value=flag_v2), \
                 patch("src.dialogue_manager.is_normalization_contract_v2_enabled", return_value=flag_norm):
                dm.process("清空所有工具")
            snapshots[mode_name] = get_effect_snapshot(dm, ["payload"])

        self.assertEqual(snapshots["Legacy"], snapshots["G2.1"])
        self.assertEqual(snapshots["Legacy"], snapshots["V2"])
        self.assertEqual(snapshots["V2"]["slots"]["payload"]["value"], [])
        self.assertEqual(snapshots["V2"]["slots"]["payload"]["status"], "missing")

    def test_v2_publish_adjacent_governance_invariants(self):
        # 验证在 V2 启用时 Hard Constraint 阻断、Soft Warning 区分与 Fail-Closed 正常运作
        dm = DialogueManager(llm=MagicMock())
        self._setup_stage2_task("pipeline_inspection", dm=dm)
        wd_slot = Slot("water_depth")
        wd_slot.value = 500
        wd_slot.status = "valid"
        eq_slot = Slot("equipment_type")
        eq_slot.value = "HYSY-601-ROV"
        eq_slot.status = "valid"
        ttk_slot = dm.slot_store.slots["task_type_key"]
        tt_slot = dm.slot_store.slots["task_type"]
        dm.slot_store.commit_transaction({"task_type": tt_slot, "task_type_key": ttk_slot, "water_depth": wd_slot, "equipment_type": eq_slot}, [])

        res = {
            "slot_candidates": [
                {
                    "canonical_key": "water_depth",
                    "normalized_value": 5000,
                    "raw_value": "5000米",
                    "confidence": 1.0,
                    "resolution_method": "canonical_exact",
                }
            ],
            "list_mutations": [],
            "unresolved": [],
        }
        dm.extractor.extract_updates = MagicMock(return_value=res)

        dm.intent_router.route = MagicMock(
            return_value=IntentRouteResult(
                interaction_type="WRITE",
                confidence=1.0,
                reason="confirm_test",
            )
        )

        with patch("src.dialogue_manager.is_task_patch_v2_enabled", return_value=True), \
             patch("src.dialogue_manager.is_normalization_contract_v2_enabled", return_value=True):
            dm.process("水深改为5000米")

        self.assertEqual(dm.phase, "blocked_hard")


if __name__ == "__main__":
    unittest.main()
