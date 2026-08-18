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
from tests.interaction_plan_support import (
    ScriptedLLM,
    extraction_result,
    make_plan,
    slot_candidate,
)


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
        llm = ScriptedLLM(plans=[make_plan("WRITE")])
        dm = DialogueManager(llm=llm)
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
        self.assertEqual(len(llm.classify_calls), 1)
        self.assertEqual(len(llm.extract_calls), 0)


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

    def test_task_type_lock_uses_current_schema_for_sibling_updates(self):
        """V2 锁定切类时，旧 Schema 合法字段可更新，目标任务专属值不得通过。"""
        self._setup_stage2_task("pipeline_inspection")
        tid_slot = Slot(
            slot_name="task_id",
            value="PI-20260813-001",
            status="valid",
            source="auto_reserved",
        )
        depth_slot = Slot(
            slot_name="water_depth",
            value=100.0,
            value_type="number",
            status="valid",
        )
        seeded_slots = self.dm.slot_store.clone_slots()
        seeded_slots["task_id"] = tid_slot
        seeded_slots["water_depth"] = depth_slot
        self.dm.slot_store.commit_transaction(seeded_slots, [])
        self.dm._rebuild_cache(commit_derived=False)
        self.dm.extractor.extract_updates = MagicMock(
            return_value={
                "slot_candidates": [
                    {
                        "canonical_key": "task_type_key",
                        "normalized_value": "pipeline_burial",
                        "raw_value": "管缆埋设",
                        "confidence": 1.0,
                        "resolution_method": "canonical_exact",
                    },
                    {
                        "canonical_key": "water_depth",
                        "normalized_value": 321.0,
                        "raw_value": "321米",
                        "confidence": 1.0,
                        "resolution_method": "type_normalization",
                    },
                    {
                        "canonical_key": "payload",
                        "normalized_value": ["高压水射流喷冲埋设模块"],
                        "raw_value": "高压水射流喷冲埋设模块",
                        "confidence": 1.0,
                        "resolution_method": "canonical_exact",
                    },
                ],
                "list_mutations": [],
                "unresolved": [],
            }
        )

        with patch("src.dialogue_manager.is_task_patch_v2_enabled", return_value=True), \
             patch("src.dialogue_manager.is_normalization_contract_v2_enabled", return_value=True):
            self.dm.process("改成管缆埋设，水深321米并带高压水射流喷冲埋设模块")

        self.assertEqual(
            self.dm.slot_store.slots["task_type_key"].value,
            "pipeline_inspection",
        )
        self.assertIn(
            "任务编号已锁定",
            self.dm.slot_store.slots["task_type_key"].validation_error,
        )
        self.assertEqual(self.dm.slot_store.slots["water_depth"].value, 321.0)
        self.assertEqual(self.dm.slot_store.slots["water_depth"].status, "valid")
        self.assertNotEqual(self.dm.slot_store.slots["payload"].status, "valid")

    def test_task_switch_payload_mutation_uses_target_schema_in_legacy_and_v2(self):
        """未锁定切换任务时，Payload ListMutation 必须按目标任务 Schema 解析。"""
        extraction_res = {
            "slot_candidates": [
                {
                    "canonical_key": "task_type_key",
                    "normalized_value": "pipeline_burial",
                    "raw_value": "管缆埋设",
                    "confidence": 1.0,
                    "resolution_method": "canonical_exact",
                }
            ],
            "list_mutations": [
                {
                    "field": "payload",
                    "operation": "add",
                    "items": ["高压水射流喷冲埋设模块"],
                    "target_items": [],
                    "raw_text": "添加高压水射流喷冲埋设模块",
                    "confidence": 1.0,
                    "source": "user_input",
                }
            ],
            "unresolved": [],
        }

        for name, task_patch_flag, normalization_flag in (
            ("legacy", False, False),
            ("v2", True, True),
        ):
            with self.subTest(runtime=name):
                dm = DialogueManager(llm=MagicMock())
                self._setup_stage2_task("pipeline_inspection", dm=dm)
                dm.extractor.extract_updates = MagicMock(
                    return_value=copy.deepcopy(extraction_res)
                )

                with patch(
                    "src.dialogue_manager.is_task_patch_v2_enabled",
                    return_value=task_patch_flag,
                ), patch(
                    "src.dialogue_manager.is_normalization_contract_v2_enabled",
                    return_value=normalization_flag,
                ):
                    dm.process("改成管缆埋设并添加高压水射流喷冲埋设模块")

                self.assertEqual(
                    dm.slot_store.slots["task_type_key"].value,
                    "pipeline_burial",
                )
                self.assertEqual(dm.slot_store.slots["payload"].status, "valid")
                self.assertEqual(
                    dm.slot_store.slots["payload"].value,
                    ["高压水射流喷冲埋设模块"],
                )

    def test_task_switch_target_only_field_reextracted_in_legacy_and_v2(self):
        """切类时必须把同一句话按目标 Schema 重抽，不能提前丢失专属字段。"""
        selector_pass = extraction_result(
            slot_candidate(
                "task_type_key",
                "tree_valve_operation",
                raw_key="任务类型",
                raw_value="采油树控制面板插入",
            )
        )
        target_pass = extraction_result(
            slot_candidate(
                "task_type",
                "采油树控制面板插入",
                raw_key="任务类型",
            ),
            slot_candidate(
                "task_type_key",
                "tree_valve_operation",
                raw_key="任务类型键",
            ),
            slot_candidate(
                "wellhead_id",
                "WH-17",
                raw_key="井口编号",
            ),
        )

        for name, task_patch_flag, normalization_flag in (
            ("legacy", False, False),
            ("v2", True, True),
        ):
            with self.subTest(runtime=name):
                llm = ScriptedLLM(
                    extractions=[selector_pass, target_pass],
                    default_extraction=target_pass,
                )
                dm = DialogueManager(llm=llm)
                self._setup_stage2_task("pipeline_inspection", dm=dm)

                with patch(
                    "src.dialogue_manager.is_task_patch_v2_enabled",
                    return_value=task_patch_flag,
                ), patch(
                    "src.dialogue_manager.is_normalization_contract_v2_enabled",
                    return_value=normalization_flag,
                ):
                    dm.process("改成采油树控制面板插入，井口编号 WH-17")

                self.assertEqual(
                    dm.slot_store.slots["task_type_key"].value,
                    "tree_valve_operation",
                )
                self.assertEqual(
                    dm.slot_store.slots["task_type"].value,
                    "采油树控制面板插入",
                )
                self.assertEqual(dm.slot_store.slots["wellhead_id"].value, "WH-17")
                self.assertEqual(dm.slot_store.slots["wellhead_id"].status, "valid")
                self.assertEqual(dm.task_state.get("wellhead_id"), "WH-17")
                self.assertEqual(len(llm.extract_calls), 2)
                self.assertTrue(
                    any(
                        "wellhead_id" in str(message.get("content", ""))
                        for message in llm.extract_calls[-1]
                        if message.get("role") == "system"
                    )
                )

    def test_task_switch_keeps_first_pass_shared_field_when_target_pass_omits_it(self):
        """目标二抽漏掉共享字段时，首抽值仍须按目标 Schema 复验并提交。"""
        selector_pass = extraction_result(
            slot_candidate(
                "task_type_key",
                "tree_valve_operation",
                raw_key="任务类型",
            ),
            slot_candidate(
                "water_depth",
                321.0,
                raw_key="水深",
                raw_value="321米",
            ),
        )
        target_pass = extraction_result(
            slot_candidate("task_type", "采油树控制面板插入"),
            slot_candidate("task_type_key", "tree_valve_operation"),
            slot_candidate("wellhead_id", "WH-17"),
        )

        for name, task_patch_flag, normalization_flag in (
            ("legacy", False, False),
            ("v2", True, True),
        ):
            with self.subTest(runtime=name):
                llm = ScriptedLLM(
                    extractions=[selector_pass, target_pass],
                    default_extraction=target_pass,
                )
                dm = DialogueManager(llm=llm)
                self._setup_stage2_task("pipeline_inspection", dm=dm)

                with patch(
                    "src.dialogue_manager.is_task_patch_v2_enabled",
                    return_value=task_patch_flag,
                ), patch(
                    "src.dialogue_manager.is_normalization_contract_v2_enabled",
                    return_value=normalization_flag,
                ):
                    dm.process("改成采油树控制面板插入，水深321米，井口WH-17")

                self.assertEqual(dm.task_state.get("task_type_key"), "tree_valve_operation")
                self.assertEqual(dm.task_state.get("water_depth"), 321.0)
                self.assertEqual(dm.task_state.get("wellhead_id"), "WH-17")

    def test_task_switch_target_pass_overrides_first_pass_shared_field(self):
        """两遍都抽到同一共享字段时，以目标 Schema 二抽结果为准。"""
        selector_pass = extraction_result(
            slot_candidate("task_type_key", "tree_valve_operation"),
            slot_candidate("water_depth", 321.0, raw_value="321米"),
        )
        target_pass = extraction_result(
            slot_candidate("task_type", "采油树控制面板插入"),
            slot_candidate("task_type_key", "tree_valve_operation"),
            slot_candidate("water_depth", 456.0, raw_value="456米"),
        )

        for name, task_patch_flag, normalization_flag in (
            ("legacy", False, False),
            ("v2", True, True),
        ):
            with self.subTest(runtime=name):
                llm = ScriptedLLM(
                    extractions=[selector_pass, target_pass],
                    default_extraction=target_pass,
                )
                dm = DialogueManager(llm=llm)
                self._setup_stage2_task("pipeline_inspection", dm=dm)
                with patch(
                    "src.dialogue_manager.is_task_patch_v2_enabled",
                    return_value=task_patch_flag,
                ), patch(
                    "src.dialogue_manager.is_normalization_contract_v2_enabled",
                    return_value=normalization_flag,
                ):
                    dm.process("改成采油树任务，水深456米")

                self.assertEqual(dm.task_state.get("water_depth"), 456.0)

    def test_task_switch_payload_mutation_ignores_old_robot_and_payload(self):
        """旧任务设备/载荷不得缩窄目标任务的动态 payload 合法域。"""
        selector_pass = extraction_result(
            slot_candidate("task_type_key", "tree_valve_operation"),
        )
        target_pass = extraction_result(
            slot_candidate("task_type", "采油树控制面板插入"),
            slot_candidate("task_type_key", "tree_valve_operation"),
            list_mutations=[
                {
                    "field": "payload",
                    "operation": "add",
                    "items": ["多功能液压机械臂"],
                    "target_items": [],
                    "raw_text": "添加多功能液压机械臂",
                    "confidence": 1.0,
                    "source": "user_input",
                }
            ],
        )

        for name, task_patch_flag, normalization_flag in (
            ("legacy", False, False),
            ("v2", True, True),
        ):
            with self.subTest(runtime=name):
                llm = ScriptedLLM(
                    extractions=[selector_pass, target_pass],
                    default_extraction=target_pass,
                )
                dm = DialogueManager(llm=llm)
                self._setup_stage2_task("pipeline_inspection", dm=dm)
                slots = dm.slot_store.clone_slots()
                for key, value in (
                    ("equipment_class", "observation_rov"),
                    ("equipment_family", "观察级深海机器人"),
                    ("equipment_type", "观察级深海机器人 75HP"),
                    ("equipment_unit_id", "OBSROV-75-001"),
                ):
                    slots[key] = Slot(key, value=value, status="valid")
                slots["payload"] = Slot(
                    "payload",
                    value_type="list",
                    value=["电磁检测传感器"],
                    status="valid",
                )
                dm.slot_store.commit_transaction(slots, [])
                dm._rebuild_cache(commit_derived=False)

                with patch(
                    "src.dialogue_manager.is_task_patch_v2_enabled",
                    return_value=task_patch_flag,
                ), patch(
                    "src.dialogue_manager.is_normalization_contract_v2_enabled",
                    return_value=normalization_flag,
                ):
                    dm.process("改成采油树控制面板插入并添加多功能液压机械臂")

                self.assertEqual(dm.task_state.get("task_type_key"), "tree_valve_operation")
                self.assertEqual(dm.task_state.get("payload"), ["多功能液压机械臂"])
                self.assertNotIn("电磁检测传感器", dm.task_state.get("payload", []))

    def test_task_switch_payload_candidate_ignores_old_robot_and_payload(self):
        """普通 payload candidate 也必须按清洁的目标任务状态规范化。"""
        selector_pass = extraction_result(
            slot_candidate("task_type_key", "tree_valve_operation"),
        )
        target_pass = extraction_result(
            slot_candidate("task_type", "采油树控制面板插入"),
            slot_candidate("task_type_key", "tree_valve_operation"),
            slot_candidate("payload", ["多功能液压机械臂"]),
        )

        for name, task_patch_flag, normalization_flag in (
            ("legacy", False, False),
            ("v2", True, True),
        ):
            with self.subTest(runtime=name):
                llm = ScriptedLLM(
                    extractions=[selector_pass, target_pass],
                    default_extraction=target_pass,
                )
                dm = DialogueManager(llm=llm)
                self._setup_stage2_task("pipeline_inspection", dm=dm)
                slots = dm.slot_store.clone_slots()
                slots["equipment_type"] = Slot(
                    "equipment_type",
                    value="观察级深海机器人 75HP",
                    status="valid",
                )
                slots["payload"] = Slot(
                    "payload",
                    value_type="list",
                    value=["电磁检测传感器"],
                    status="valid",
                )
                dm.slot_store.commit_transaction(slots, [])
                dm._rebuild_cache(commit_derived=False)

                with patch(
                    "src.dialogue_manager.is_task_patch_v2_enabled",
                    return_value=task_patch_flag,
                ), patch(
                    "src.dialogue_manager.is_normalization_contract_v2_enabled",
                    return_value=normalization_flag,
                ):
                    dm.process("改成采油树插入并携带多功能液压机械臂")

                self.assertEqual(dm.task_state.get("task_type_key"), "tree_valve_operation")
                self.assertEqual(dm.task_state.get("payload"), ["多功能液压机械臂"])

    def test_task_switch_target_robot_uses_same_turn_depth_not_stale_depth(self):
        """目标机器人候选必须按本轮新水深生成，不能被旧水深提前过滤。"""
        selector_pass = extraction_result(
            slot_candidate("task_type_key", "pipeline_burial"),
            slot_candidate("water_depth", 400.0, raw_value="400米"),
        )
        target_pass = extraction_result(
            slot_candidate("task_type", "管缆埋设"),
            slot_candidate("task_type_key", "pipeline_burial"),
            slot_candidate("water_depth", 400.0, raw_value="400米"),
            slot_candidate(
                "equipment_type",
                "履带式海底重载作业机器人 1600HP",
            ),
        )

        for name, task_patch_flag, normalization_flag in (
            ("legacy", False, False),
            ("v2", True, True),
        ):
            with self.subTest(runtime=name):
                llm = ScriptedLLM(
                    extractions=[selector_pass, target_pass],
                    default_extraction=target_pass,
                )
                dm = DialogueManager(llm=llm)
                self._setup_stage2_task("pipeline_inspection", dm=dm)
                slots = dm.slot_store.clone_slots()
                slots["water_depth"] = Slot(
                    "water_depth",
                    value_type="number",
                    value=600.0,
                    status="valid",
                )
                dm.slot_store.commit_transaction(slots, [])
                dm._rebuild_cache(commit_derived=False)

                with patch(
                    "src.dialogue_manager.is_task_patch_v2_enabled",
                    return_value=task_patch_flag,
                ), patch(
                    "src.dialogue_manager.is_normalization_contract_v2_enabled",
                    return_value=normalization_flag,
                ):
                    dm.process("改成管缆埋设，水深400米，使用履带式1600HP")

                self.assertEqual(dm.task_state.get("task_type_key"), "pipeline_burial")
                self.assertEqual(dm.task_state.get("water_depth"), 400.0)
                self.assertEqual(
                    dm.task_state.get("equipment_type"),
                    "履带式海底重载作业机器人 1600HP",
                )

    def test_target_pass_conflicting_concrete_task_type_is_rejected_atomically(self):
        """目标二抽同键给出插入/拔出时必须拒绝，不能 last-wins。"""
        selector_pass = extraction_result(
            slot_candidate("task_type_key", "tree_valve_operation"),
        )
        target_pass = extraction_result(
            slot_candidate("task_type", "采油树控制面板插入"),
            slot_candidate("task_type", "采油树控制面板拔出"),
            slot_candidate("task_type_key", "tree_valve_operation"),
            slot_candidate("wellhead_id", "WH-17"),
        )

        for name, task_patch_flag, normalization_flag in (
            ("legacy", False, False),
            ("v2", True, True),
        ):
            with self.subTest(runtime=name):
                llm = ScriptedLLM(
                    extractions=[selector_pass, target_pass],
                    default_extraction=target_pass,
                )
                dm = DialogueManager(llm=llm)
                self._setup_stage2_task("pipeline_inspection", dm=dm)
                before = dm.slot_store.get_task_state()
                with patch(
                    "src.dialogue_manager.is_task_patch_v2_enabled",
                    return_value=task_patch_flag,
                ), patch(
                    "src.dialogue_manager.is_normalization_contract_v2_enabled",
                    return_value=normalization_flag,
                ):
                    dm.process("改成采油树插入或拔出，井口WH-17")

                self.assertEqual(dm.slot_store.get_task_state(), before)
                self.assertIn(
                    "同轮具体任务类型互相冲突",
                    dm.slot_store.slots["task_type_key"].validation_error,
                )

    def test_cross_pass_conflicting_concrete_task_type_is_rejected_atomically(self):
        """首抽与目标二抽的具体操作冲突时也必须拒绝，不能让二抽覆盖。"""
        selector_pass = extraction_result(
            slot_candidate("task_type", "采油树控制面板插入"),
            slot_candidate("task_type_key", "tree_valve_operation"),
        )
        target_pass = extraction_result(
            slot_candidate("task_type", "采油树控制面板拔出"),
            slot_candidate("task_type_key", "tree_valve_operation"),
            slot_candidate("wellhead_id", "WH-17"),
        )

        for name, task_patch_flag, normalization_flag in (
            ("legacy", False, False),
            ("v2", True, True),
        ):
            with self.subTest(runtime=name):
                llm = ScriptedLLM(
                    extractions=[selector_pass, target_pass],
                    default_extraction=target_pass,
                )
                dm = DialogueManager(llm=llm)
                self._setup_stage2_task("pipeline_inspection", dm=dm)
                before = dm.slot_store.get_task_state()
                with patch(
                    "src.dialogue_manager.is_task_patch_v2_enabled",
                    return_value=task_patch_flag,
                ), patch(
                    "src.dialogue_manager.is_normalization_contract_v2_enabled",
                    return_value=normalization_flag,
                ):
                    dm.process("改成采油树插入，井口WH-17")

                self.assertEqual(dm.slot_store.get_task_state(), before)
                self.assertIn(
                    "同轮具体任务类型互相冲突",
                    dm.slot_store.slots["task_type_key"].validation_error,
                )

    def test_malformed_target_task_selector_fails_closed_without_exception(self):
        """模型返回 list/dict 任务 selector 时必须结构化拒绝，不能 TypeError。"""
        selector_pass = extraction_result(
            slot_candidate("task_type_key", "tree_valve_operation"),
        )
        malformed_values = (["tree_valve_operation"], {"key": "tree_valve_operation"})
        for malformed in malformed_values:
            for name, task_patch_flag, normalization_flag in (
                ("legacy", False, False),
                ("v2", True, True),
            ):
                with self.subTest(runtime=name, malformed=malformed):
                    target_pass = extraction_result(
                        slot_candidate("task_type_key", malformed),
                        slot_candidate("wellhead_id", "WH-17"),
                    )
                    llm = ScriptedLLM(
                        extractions=[selector_pass, target_pass],
                        default_extraction=target_pass,
                    )
                    dm = DialogueManager(llm=llm)
                    self._setup_stage2_task("pipeline_inspection", dm=dm)
                    before = dm.slot_store.get_task_state()
                    with patch(
                        "src.dialogue_manager.is_task_patch_v2_enabled",
                        return_value=task_patch_flag,
                    ), patch(
                        "src.dialogue_manager.is_normalization_contract_v2_enabled",
                        return_value=normalization_flag,
                    ):
                        dm.process("改成采油树任务，井口WH-17")

                    self.assertEqual(dm.slot_store.get_task_state(), before)
                    self.assertIn(
                        "非空字符串",
                        dm.slot_store.slots["task_type_key"].validation_error,
                    )

    def test_same_turn_explicit_robot_revalidates_payload_in_legacy_and_v2(self):
        """同轮机器人更新后必须按最终 Variant 复验普通 payload candidate。"""
        extraction = extraction_result(
            slot_candidate("equipment_unit_id", "AUV-324cc-001"),
            slot_candidate("payload", ["电磁检测传感器"]),
        )

        for name, task_patch_flag, normalization_flag in (
            ("legacy", False, False),
            ("v2", True, True),
        ):
            with self.subTest(runtime=name):
                dm = DialogueManager(llm=MagicMock())
                self._setup_stage2_task("pipeline_inspection", dm=dm)
                dm.extractor.extract_updates = MagicMock(
                    return_value=copy.deepcopy(extraction)
                )
                with patch(
                    "src.dialogue_manager.is_task_patch_v2_enabled",
                    return_value=task_patch_flag,
                ), patch(
                    "src.dialogue_manager.is_normalization_contract_v2_enabled",
                    return_value=normalization_flag,
                ):
                    dm.process("改用AUV-324cc-001并携带电磁检测传感器")

                self.assertEqual(
                    dm.task_state.get("equipment_unit_id"),
                    "AUV-324cc-001",
                )
                self.assertNotEqual(dm.slot_store.slots["payload"].status, "valid")
                self.assertNotIn("payload", dm.task_state)

    def test_same_turn_parent_robot_selector_collapses_before_payload_validation(self):
        """Class/Family 唯一下级须先在沙箱收敛，再校验依赖 Variant 的载荷。"""
        for selector_key, selector_value in (
            ("equipment_class", "auv"),
            ("equipment_family", "水下无人自主航行器"),
        ):
            extraction = extraction_result(
                slot_candidate(selector_key, selector_value),
                slot_candidate("payload", ["侧扫声呐"]),
            )
            for name, task_patch_flag, normalization_flag in (
                ("legacy", False, False),
                ("v2", True, True),
            ):
                with self.subTest(runtime=name, selector=selector_key):
                    dm = DialogueManager(llm=MagicMock())
                    self._setup_stage2_task("pipeline_inspection", dm=dm)
                    dm.extractor.extract_updates = MagicMock(
                        return_value=copy.deepcopy(extraction)
                    )
                    with patch(
                        "src.dialogue_manager.is_task_patch_v2_enabled",
                        return_value=task_patch_flag,
                    ), patch(
                        "src.dialogue_manager.is_normalization_contract_v2_enabled",
                        return_value=normalization_flag,
                    ):
                        dm.process("改用AUV并携带侧扫声呐")

                    self.assertEqual(
                        dm.task_state.get("equipment_type"),
                        "水下无人自主航行器 324CC",
                    )
                    self.assertEqual(
                        dm.task_state.get("equipment_unit_id"),
                        "AUV-324cc-001",
                    )
                    self.assertEqual(
                        dm.task_state.get("payload"),
                        ["侧扫声呐"],
                    )

    def test_unit_only_replaces_old_complete_cascade_before_payload_validation(self):
        """Unit-only 更新不能被旧 Variant 上下文限制或错绑同尾号机器。"""
        for unit_selector in ("AUV-324cc-001", "AUV001"):
            extraction = extraction_result(
                slot_candidate("equipment_unit_id", unit_selector),
                slot_candidate("payload", ["侧扫声呐"]),
            )
            for name, task_patch_flag, normalization_flag in (
                ("legacy", False, False),
                ("v2", True, True),
            ):
                with self.subTest(runtime=name, unit=unit_selector):
                    dm = DialogueManager(llm=MagicMock())
                    self._setup_stage2_task("pipeline_inspection", dm=dm)
                    dm._handle_equipment_updates_in_transaction(
                        {"equipment_unit_id": "OBSROV-75-001"},
                        dm.slot_store.slots,
                        allow_overwrite=True,
                    )
                    dm.extractor.extract_updates = MagicMock(
                        return_value=copy.deepcopy(extraction)
                    )
                    with patch(
                        "src.dialogue_manager.is_task_patch_v2_enabled",
                        return_value=task_patch_flag,
                    ), patch(
                        "src.dialogue_manager.is_normalization_contract_v2_enabled",
                        return_value=normalization_flag,
                    ):
                        dm.process("改用AUV一号机并携带侧扫声呐")

                    self.assertEqual(dm.task_state.get("equipment_class"), "auv")
                    self.assertEqual(
                        dm.task_state.get("equipment_family"),
                        "水下无人自主航行器",
                    )
                    self.assertEqual(
                        dm.task_state.get("equipment_type"),
                        "水下无人自主航行器 324CC",
                    )
                    self.assertEqual(
                        dm.task_state.get("equipment_unit_id"),
                        "AUV-324cc-001",
                    )
                    self.assertEqual(
                        dm.task_state.get("payload"),
                        ["侧扫声呐"],
                    )

    def test_same_turn_robot_payload_list_mutation_uses_post_update_variant(self):
        """Payload ListMutation 必须使用同轮目标 Variant，合法项接受、非法项拒绝。"""
        for payload_name, should_succeed in (
            ("侧扫声呐", True),
            ("电磁检测传感器", False),
        ):
            extraction = extraction_result(
                slot_candidate("equipment_unit_id", "AUV-324cc-001"),
                list_mutations=[
                    {
                        "field": "payload",
                        "operation": "add",
                        "items": [payload_name],
                        "target_items": [],
                        "raw_text": f"添加{payload_name}",
                        "confidence": 1.0,
                        "source": "user_input",
                    }
                ],
            )
            for name, task_patch_flag, normalization_flag in (
                ("legacy", False, False),
                ("v2", True, True),
            ):
                with self.subTest(
                    runtime=name,
                    payload=payload_name,
                    should_succeed=should_succeed,
                ):
                    dm = DialogueManager(llm=MagicMock())
                    self._setup_stage2_task("pipeline_inspection", dm=dm)
                    dm.extractor.extract_updates = MagicMock(
                        return_value=copy.deepcopy(extraction)
                    )
                    with patch(
                        "src.dialogue_manager.is_task_patch_v2_enabled",
                        return_value=task_patch_flag,
                    ), patch(
                        "src.dialogue_manager.is_normalization_contract_v2_enabled",
                        return_value=normalization_flag,
                    ):
                        dm.process(f"改用AUV-324cc-001并添加{payload_name}")

                    self.assertEqual(
                        dm.task_state.get("equipment_unit_id"),
                        "AUV-324cc-001",
                    )
                    if should_succeed:
                        self.assertEqual(dm.task_state.get("payload"), [payload_name])
                    else:
                        self.assertNotEqual(
                            dm.slot_store.slots["payload"].status,
                            "valid",
                        )
                        self.assertNotIn("payload", dm.task_state)

    def test_task_switch_target_oilfield_is_linked_after_old_task_slots_clear(self):
        """清除旧任务专属槽位不能误删目标二抽后产生的油田实体。"""
        selector_pass = extraction_result(
            slot_candidate("task_type_key", "tree_valve_operation"),
        )
        target_pass = extraction_result(
            slot_candidate("task_type", "采油树控制面板插入"),
            slot_candidate("task_type_key", "tree_valve_operation"),
            slot_candidate("oilfield_name", "陵水17-2"),
        )

        for name, task_patch_flag, normalization_flag in (
            ("legacy", False, False),
            ("v2", True, True),
        ):
            with self.subTest(runtime=name):
                llm = ScriptedLLM(
                    extractions=[selector_pass, target_pass],
                    default_extraction=target_pass,
                )
                dm = DialogueManager(llm=llm)
                self._setup_stage2_task("pipeline_inspection", dm=dm)
                with patch(
                    "src.dialogue_manager.is_task_patch_v2_enabled",
                    return_value=task_patch_flag,
                ), patch(
                    "src.dialogue_manager.is_normalization_contract_v2_enabled",
                    return_value=normalization_flag,
                ):
                    dm.process("改成采油树控制面板插入，油田是陵水17-2")

                self.assertEqual(dm.task_state.get("task_type_key"), "tree_valve_operation")
                self.assertEqual(dm.task_state.get("oilfield_name"), "陵水17-2气田")
                self.assertTrue(dm.task_state.get("oilfield_entity_id"))

    def test_task_switch_clears_old_oilfield_lineage_and_constraints(self):
        """离开采油树任务后旧油田实体不得残留或注入 C028/C029。"""
        selector_pass = extraction_result(
            slot_candidate("task_type_key", "pipeline_inspection"),
        )
        target_pass = extraction_result(
            slot_candidate("task_type", "管缆巡检"),
            slot_candidate("task_type_key", "pipeline_inspection"),
            slot_candidate("water_depth", 2000.0, raw_value="2000米"),
        )
        oilfield_values = {
            "oilfield_name": "陵水17-2气田",
            "raw_oilfield_name": "陵水17-2",
            "oilfield_match_status": "accepted",
            "oilfield_match_confidence": 1.0,
            "oilfield_match_evidence": ["test"],
            "oilfield_entity_id": "lingshui_17_2",
            "pending_oilfield_name": "旧候选",
            "pending_oilfield_candidates": [{"id": "old"}],
        }

        for name, task_patch_flag, normalization_flag in (
            ("legacy", False, False),
            ("v2", True, True),
        ):
            with self.subTest(runtime=name):
                llm = ScriptedLLM(
                    extractions=[selector_pass, target_pass],
                    default_extraction=target_pass,
                )
                dm = DialogueManager(llm=llm)
                self._setup_stage2_task("tree_valve_operation", dm=dm)
                slots = dm.slot_store.clone_slots()
                for key, value in oilfield_values.items():
                    slots[key] = Slot(key, value=value, status="valid")
                dm.slot_store.commit_transaction(slots, [])
                dm._rebuild_cache(commit_derived=False)

                with patch(
                    "src.dialogue_manager.is_task_patch_v2_enabled",
                    return_value=task_patch_flag,
                ), patch(
                    "src.dialogue_manager.is_normalization_contract_v2_enabled",
                    return_value=normalization_flag,
                ):
                    dm.process("改成管缆巡检，水深2000米")

                self.assertEqual(dm.task_state.get("task_type_key"), "pipeline_inspection")
                for key in oilfield_values:
                    slot = dm.slot_store.slots.get(key)
                    self.assertTrue(
                        slot is None
                        or (
                            slot.status == "missing"
                            and slot.value in (None, [])
                            and key not in dm.task_state
                        ),
                        key,
                    )
                self.assertEqual(dm._merge_oilfield_context_violations([]), [])

    def test_locked_task_switch_rejects_target_only_oilfield_without_pollution(self):
        """任务编号锁定类别后，目标任务专属 linker 字段不得污染当前任务。"""
        extraction_res = extraction_result(
            slot_candidate(
                "task_type_key",
                "tree_valve_operation",
                raw_key="任务类型",
                raw_value="采油树控制面板插入",
            ),
            slot_candidate(
                "oilfield_name",
                "陵水17-2",
                raw_key="油田名称",
            ),
            slot_candidate(
                "water_depth",
                321.0,
                raw_key="水深",
                raw_value="321米",
            ),
        )
        oilfield_keys = (
            "oilfield_name",
            "raw_oilfield_name",
            "oilfield_match_status",
            "oilfield_entity_id",
            "pending_oilfield_name",
        )

        for name, task_patch_flag, normalization_flag in (
            ("legacy", False, False),
            ("v2", True, True),
        ):
            with self.subTest(runtime=name):
                dm = DialogueManager(llm=MagicMock())
                self._setup_stage2_task("pipeline_inspection", dm=dm)
                slots = dm.slot_store.clone_slots()
                slots["task_id"] = Slot(
                    "task_id",
                    value="PI-20260813-001",
                    status="valid",
                    source="auto_reserved",
                )
                dm.slot_store.commit_transaction(slots, [])
                dm._rebuild_cache(commit_derived=False)
                dm.extractor.extract_updates = MagicMock(
                    return_value=copy.deepcopy(extraction_res)
                )

                with patch(
                    "src.dialogue_manager.is_task_patch_v2_enabled",
                    return_value=task_patch_flag,
                ), patch(
                    "src.dialogue_manager.is_normalization_contract_v2_enabled",
                    return_value=normalization_flag,
                ):
                    dm.process("改成采油树任务，在陵水17-2，水深321米")

                self.assertEqual(
                    dm.slot_store.slots["task_type_key"].value,
                    "pipeline_inspection",
                )
                self.assertIn(
                    "任务编号已锁定",
                    dm.slot_store.slots["task_type_key"].validation_error,
                )
                self.assertEqual(dm.slot_store.slots["water_depth"].value, 321.0)
                self.assertEqual(dm.slot_store.slots["water_depth"].status, "valid")
                for key in oilfield_keys:
                    slot = dm.slot_store.slots.get(key)
                    self.assertTrue(
                        slot is None
                        or (slot.value is None and slot.status == "missing"),
                        key,
                    )

    def test_list_mutation_requires_field_membership_in_effective_schema(self):
        """ListMutation 不得把普通模式字段写入不含该字段的紧急 Schema。"""
        for name, task_patch_flag, normalization_flag in (
            ("legacy", False, False),
            ("v2", True, True),
        ):
            with self.subTest(runtime=name):
                dm = DialogueManager(llm=MagicMock())
                dm.mode = "emergency"
                self._setup_stage2_task("pipeline_inspection", dm=dm)
                dm.extractor.extract_updates = MagicMock(
                    return_value={
                        "slot_candidates": [],
                        "list_mutations": [
                            {
                                "field": "payload",
                                "operation": "add",
                                "items": ["高清水下摄像机"],
                                "target_items": [],
                                "raw_text": "添加高清水下摄像机",
                                "confidence": 1.0,
                                "source": "user_input",
                            }
                        ],
                        "unresolved": [],
                    }
                )

                with patch(
                    "src.dialogue_manager.is_task_patch_v2_enabled",
                    return_value=task_patch_flag,
                ), patch(
                    "src.dialogue_manager.is_normalization_contract_v2_enabled",
                    return_value=normalization_flag,
                ):
                    dm.process("添加高清水下摄像机")

                payload = dm.slot_store.slots.get("payload")
                self.assertTrue(
                    payload is None
                    or (payload.value in (None, []) and payload.status == "missing")
                )
                self.assertTrue(
                    any("列表字段 payload" in item for item in dm.slot_store.unresolved)
                )

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

        eq_keys = ["equipment_class", "equipment_family", "equipment_type", "equipment_name", "equipment_unit_id"]
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
                    "normalized_value": "气田",
                    "raw_value": "气田",
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
                dm.process("作业在气田")

            snapshots[mode_name] = get_effect_snapshot(dm, oil_keys)

        self.assertEqual(snapshots["Legacy"], snapshots["G2.1"])
        self.assertEqual(snapshots["Legacy"], snapshots["V2"])
        self.assertEqual(snapshots["V2"]["slots"]["pending_oilfield_name"]["value"], "气田")
        self.assertEqual(snapshots["V2"]["slots"]["pending_oilfield_name"]["status"], "valid")
        self.assertIsNone(snapshots["V2"]["slots"]["oilfield_name"]["value"])
        self.assertEqual(snapshots["V2"]["slots"]["oilfield_name"]["status"], "missing")

    def test_v2_oilfield_removed_key_not_reapplied(self):
        self._setup_stage2_task("tree_valve_operation")

        extraction_res = {
            "slot_candidates": [
                {
                    "canonical_key": "oilfield_name",
                    "normalized_value": "气田",
                    "raw_value": "气田",
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
            self.dm.process("作业在气田")

        of_slot = self.dm.slot_store.slots.get("oilfield_name")
        self.assertIsNotNone(of_slot)
        self.assertIsNone(of_slot.value)
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
                    "normalized_value": "气田",
                    "raw_value": "气田",
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
            self.dm.process("作业在气田")

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
        self._setup_stage2_task("tree_valve_operation", dm=dm)
        wd_slot = Slot("water_depth")
        wd_slot.value = 500
        wd_slot.status = "valid"
        eq_slot = Slot("equipment_type")
        eq_slot.value = "通用工作级深海机器人 250HP"
        eq_slot.status = "valid"
        eq_slot.source = "user_input"
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
