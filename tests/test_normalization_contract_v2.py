"""tests/test_normalization_contract_v2.py — Normalization Outcome Contract (G2.2-A) Unit Tests
"""

import copy
import unittest
from unittest.mock import patch, MagicMock

from src.knowledge_retriever import KnowledgeBase
from src.output_builder import OutputBuilder
from src.task_patch import (
    ListMutationPatch,
    SlotPatch,
    TaskPatch,
    TaskPatchValidationError,
)
from src.normalizer import FieldNormalizer
from src.normalization_contract import (
    NormalizedTaskPatch,
    NormalizationContractError,
    SlotNormalizationOutcome,
    normalize_task_patch,
)


class TestL1OutcomeSchemaAndInvariants(unittest.TestCase):
    """L1 Outcome & Container Invariants 校验测试"""

    def test_success_outcome_contract(self):
        outcome = SlotNormalizationOutcome(
            key="water_depth",
            success=True,
            normalized_value=300.0,
            candidate_value="300米",
            raw_value="大概三百米",
            confidence=0.95,
            source="user_input",
            resolution_method="llm_semantic",
            error_code=None,
            error_message=None,
        )
        self.assertEqual(outcome.key, "water_depth")
        self.assertTrue(outcome.success)
        self.assertEqual(outcome.normalized_value, 300.0)
        self.assertEqual(outcome.candidate_value, "300米")
        self.assertEqual(outcome.raw_value, "大概三百米")
        self.assertEqual(outcome.confidence, 0.95)
        self.assertEqual(outcome.source, "user_input")
        self.assertEqual(outcome.resolution_method, "llm_semantic")
        self.assertIsNone(outcome.error_code)
        self.assertIsNone(outcome.error_message)

    def test_failure_outcome_contract(self):
        outcome = SlotNormalizationOutcome(
            key="water_depth",
            success=False,
            normalized_value=None,
            candidate_value="300abc",
            raw_value="差不多很深",
            confidence=0.90,
            source="user_input",
            resolution_method="llm_semantic",
            error_code="invalid_number",
            error_message="无法将 '300abc' 规范化为合法的 number 类型",
        )
        self.assertEqual(outcome.key, "water_depth")
        self.assertFalse(outcome.success)
        self.assertIsNone(outcome.normalized_value)
        self.assertEqual(outcome.candidate_value, "300abc")
        self.assertEqual(outcome.raw_value, "差不多很深")
        self.assertEqual(outcome.error_code, "invalid_number")
        self.assertIn("invalid_number", outcome.error_code)

    def test_success_rejects_error_code(self):
        with self.assertRaises(NormalizationContractError):
            SlotNormalizationOutcome(
                key="water_depth",
                success=True,
                normalized_value=300.0,
                candidate_value="300米",
                raw_value="300米",
                confidence=0.9,
                source="user_input",
                error_code="invalid_number",
                error_message=None,
            )

    def test_success_rejects_error_message(self):
        with self.assertRaises(NormalizationContractError):
            SlotNormalizationOutcome(
                key="water_depth",
                success=True,
                normalized_value=300.0,
                candidate_value="300米",
                raw_value="300米",
                confidence=0.9,
                source="user_input",
                error_code=None,
                error_message="some error",
            )

    def test_failure_requires_error_code(self):
        with self.assertRaises(NormalizationContractError):
            SlotNormalizationOutcome(
                key="water_depth",
                success=False,
                normalized_value=None,
                candidate_value="300abc",
                raw_value="300abc",
                confidence=0.9,
                source="user_input",
                error_code=None,
                error_message="error message",
            )

    def test_failure_requires_error_message(self):
        with self.assertRaises(NormalizationContractError):
            SlotNormalizationOutcome(
                key="water_depth",
                success=False,
                normalized_value=None,
                candidate_value="300abc",
                raw_value="300abc",
                confidence=0.9,
                source="user_input",
                error_code="invalid_number",
                error_message=None,
            )

    def test_failure_requires_normalized_none(self):
        with self.assertRaises(NormalizationContractError):
            SlotNormalizationOutcome(
                key="water_depth",
                success=False,
                normalized_value=300.0,
                candidate_value="300abc",
                raw_value="300abc",
                confidence=0.9,
                source="user_input",
                error_code="invalid_number",
                error_message="error message",
            )

    def test_success_requires_normalized_value(self):
        with self.assertRaises(NormalizationContractError):
            SlotNormalizationOutcome(
                key="water_depth",
                success=True,
                normalized_value=None,
                candidate_value="300米",
                raw_value="300米",
                confidence=0.9,
                source="user_input",
                error_code=None,
                error_message=None,
            )

    def test_reject_empty_key(self):
        with self.assertRaises(NormalizationContractError):
            SlotNormalizationOutcome(
                key="",
                success=True,
                normalized_value=300.0,
                candidate_value="300米",
                raw_value="300米",
                confidence=0.9,
                source="user_input",
            )

    def test_reject_invalid_confidence(self):
        with self.assertRaises(TaskPatchValidationError):
            SlotNormalizationOutcome(
                key="water_depth",
                success=True,
                normalized_value=300.0,
                candidate_value="300米",
                raw_value="300米",
                confidence=1.5,
                source="user_input",
            )

    def test_reject_invalid_source(self):
        with self.assertRaises(NormalizationContractError):
            SlotNormalizationOutcome(
                key="water_depth",
                success=True,
                normalized_value=300.0,
                candidate_value="300米",
                raw_value="300米",
                confidence=0.9,
                source="",
            )

    def test_reject_invalid_resolution_method(self):
        with self.assertRaises(NormalizationContractError):
            SlotNormalizationOutcome(
                key="water_depth",
                success=True,
                normalized_value=300.0,
                candidate_value="300米",
                raw_value="300米",
                confidence=0.9,
                source="user_input",
                resolution_method="",
            )

    def test_normalized_task_patch_is_immutable(self):
        outcome = SlotNormalizationOutcome(
            key="water_depth",
            success=True,
            normalized_value=300.0,
            candidate_value="300米",
            raw_value="300米",
            confidence=0.9,
            source="user_input",
        )
        patch = NormalizedTaskPatch(
            schema_version=1,
            slot_outcomes=(outcome,),
            passthrough_slot_updates=(),
            list_mutations=(),
            unresolved=(),
        )
        with self.assertRaises(AttributeError):
            patch.schema_version = 2  # type: ignore

    def test_reject_duplicate_outcome_keys(self):
        o1 = SlotNormalizationOutcome(
            key="water_depth",
            success=True,
            normalized_value=300.0,
            candidate_value="300米",
            raw_value="300米",
            confidence=0.9,
            source="user_input",
        )
        o2 = SlotNormalizationOutcome(
            key="water_depth",
            success=True,
            normalized_value=500.0,
            candidate_value="500米",
            raw_value="500米",
            confidence=0.9,
            source="user_input",
        )
        with self.assertRaises(NormalizationContractError):
            NormalizedTaskPatch(
                schema_version=1,
                slot_outcomes=(o1, o2),
                passthrough_slot_updates=(),
                list_mutations=(),
                unresolved=(),
            )

    def test_reject_duplicate_passthrough_keys(self):
        p1 = SlotPatch(key="equipment_type", candidate_value="ROV", raw_value="ROV", confidence=0.9, source="user_input")
        p2 = SlotPatch(key="equipment_type", candidate_value="AUV", raw_value="AUV", confidence=0.9, source="user_input")
        with self.assertRaises(NormalizationContractError):
            NormalizedTaskPatch(
                schema_version=1,
                slot_outcomes=(),
                passthrough_slot_updates=(p1, p2),
                list_mutations=(),
                unresolved=(),
            )

    def test_reject_key_in_outcome_and_passthrough(self):
        o1 = SlotNormalizationOutcome(
            key="equipment_type",
            success=True,
            normalized_value="ROV",
            candidate_value="ROV",
            raw_value="ROV",
            confidence=0.9,
            source="user_input",
        )
        p1 = SlotPatch(key="equipment_type", candidate_value="ROV", raw_value="ROV", confidence=0.9, source="user_input")
        with self.assertRaises(NormalizationContractError):
            NormalizedTaskPatch(
                schema_version=1,
                slot_outcomes=(o1,),
                passthrough_slot_updates=(p1,),
                list_mutations=(),
                unresolved=(),
            )


class TestL2NormalizeTaskPatchContract(unittest.TestCase):
    """L2 normalize_task_patch Functional Contract Tests"""

    def setUp(self):
        self.dummy_resolver = lambda fdef, state: None

    def test_number_success(self):
        patch_input = TaskPatch(
            schema_version=1,
            slot_updates=(
                SlotPatch(
                    key="water_depth",
                    candidate_value="300米",
                    raw_value="大概三百米",
                    confidence=0.95,
                    source="user_input",
                    resolution_method="llm_semantic",
                ),
            ),
            list_mutations=(),
            unresolved=(),
        )
        field_defs = [{"key": "water_depth", "type": "number"}]
        res = normalize_task_patch(
            patch_input,
            field_defs,
            {},
            self.dummy_resolver,
            passthrough_keys=set(),
        )
        self.assertEqual(len(res.slot_outcomes), 1)
        o = res.slot_outcomes[0]
        self.assertTrue(o.success)
        self.assertEqual(o.normalized_value, 300.0)
        self.assertEqual(o.candidate_value, "300米")
        self.assertEqual(o.raw_value, "大概三百米")
        self.assertIsNone(o.error_code)

    def test_number_failure(self):
        patch_input = TaskPatch(
            schema_version=1,
            slot_updates=(
                SlotPatch(
                    key="water_depth",
                    candidate_value="300abc",
                    raw_value="差不多很深",
                    confidence=0.90,
                    source="user_input",
                ),
            ),
            list_mutations=(),
            unresolved=(),
        )
        field_defs = [{"key": "water_depth", "type": "number"}]
        res = normalize_task_patch(
            patch_input,
            field_defs,
            {},
            self.dummy_resolver,
            passthrough_keys=set(),
        )
        self.assertEqual(len(res.slot_outcomes), 1)
        o = res.slot_outcomes[0]
        self.assertFalse(o.success)
        self.assertIsNone(o.normalized_value)
        self.assertEqual(o.candidate_value, "300abc")
        self.assertEqual(o.raw_value, "差不多很深")
        self.assertEqual(o.error_code, "invalid_number")
        self.assertIn("无法将 '300abc' 规范化", o.error_message)

    def test_datetime_success(self):
        patch_input = TaskPatch(
            schema_version=1,
            slot_updates=(
                SlotPatch(
                    key="start_time",
                    candidate_value="2026-08-10 09:00:00",
                    raw_value="下周一早上九点",
                    confidence=0.9,
                    source="user_input",
                ),
            ),
            list_mutations=(),
            unresolved=(),
        )
        field_defs = [{"key": "start_time", "type": "datetime"}]
        res = normalize_task_patch(
            patch_input,
            field_defs,
            {},
            self.dummy_resolver,
            passthrough_keys=set(),
        )
        o = res.slot_outcomes[0]
        self.assertTrue(o.success)
        self.assertEqual(o.normalized_value, "2026-08-10T09:00:00")

    def test_datetime_failure(self):
        patch_input = TaskPatch(
            schema_version=1,
            slot_updates=(
                SlotPatch(
                    key="start_time",
                    candidate_value="随便什么时候",
                    raw_value="随便什么时候",
                    confidence=0.9,
                    source="user_input",
                ),
            ),
            list_mutations=(),
            unresolved=(),
        )
        field_defs = [{"key": "start_time", "type": "datetime"}]
        res = normalize_task_patch(
            patch_input,
            field_defs,
            {},
            self.dummy_resolver,
            passthrough_keys=set(),
        )
        o = res.slot_outcomes[0]
        self.assertFalse(o.success)
        self.assertEqual(o.error_code, "invalid_datetime")

    def test_coord_success(self):
        patch_input = TaskPatch(
            schema_version=1,
            slot_updates=(
                SlotPatch(
                    key="start_point",
                    candidate_value="北纬20度，东经110度",
                    raw_value="北纬20度，东经110度",
                    confidence=0.9,
                    source="user_input",
                ),
            ),
            list_mutations=(),
            unresolved=(),
        )
        field_defs = [{"key": "start_point", "type": "coord"}]
        res = normalize_task_patch(
            patch_input,
            field_defs,
            {},
            self.dummy_resolver,
            passthrough_keys=set(),
        )
        o = res.slot_outcomes[0]
        self.assertTrue(o.success)
        self.assertEqual(o.normalized_value, {"lat": 20.0, "lon": 110.0})

    def test_coord_failure(self):
        patch_input = TaskPatch(
            schema_version=1,
            slot_updates=(
                SlotPatch(
                    key="start_point",
                    candidate_value="非法坐标点",
                    raw_value="非法坐标点",
                    confidence=0.9,
                    source="user_input",
                ),
            ),
            list_mutations=(),
            unresolved=(),
        )
        field_defs = [{"key": "start_point", "type": "coord"}]
        res = normalize_task_patch(
            patch_input,
            field_defs,
            {},
            self.dummy_resolver,
            passthrough_keys=set(),
        )
        o = res.slot_outcomes[0]
        self.assertFalse(o.success)
        self.assertEqual(o.error_code, "invalid_coord")

    def test_enum_success(self):
        patch_input = TaskPatch(
            schema_version=1,
            slot_updates=(
                SlotPatch(
                    key="cable_type",
                    candidate_value="电力电缆",
                    raw_value="电力缆",
                    confidence=0.9,
                    source="user_input",
                ),
            ),
            list_mutations=(),
            unresolved=(),
        )
        field_defs = [{"key": "cable_type", "type": "string"}]
        resolver = lambda fdef, state: ["电力电缆", "光纤通信缆"]
        res = normalize_task_patch(
            patch_input,
            field_defs,
            {},
            resolver,
            passthrough_keys=set(),
        )
        o = res.slot_outcomes[0]
        self.assertTrue(o.success)
        self.assertEqual(o.normalized_value, "电力电缆")

    def test_enum_failure(self):
        patch_input = TaskPatch(
            schema_version=1,
            slot_updates=(
                SlotPatch(
                    key="cable_type",
                    candidate_value="未知特别电缆",
                    raw_value="未知特别电缆",
                    confidence=0.9,
                    source="user_input",
                ),
            ),
            list_mutations=(),
            unresolved=(),
        )
        field_defs = [{"key": "cable_type", "type": "string"}]
        resolver = lambda fdef, state: ["电力电缆", "光纤通信缆"]
        res = normalize_task_patch(
            patch_input,
            field_defs,
            {},
            resolver,
            passthrough_keys=set(),
        )
        o = res.slot_outcomes[0]
        self.assertFalse(o.success)
        self.assertEqual(o.error_code, "invalid_enum")

    def test_list_success(self):
        patch_input = TaskPatch(
            schema_version=1,
            slot_updates=(
                SlotPatch(
                    key="payload",
                    candidate_value=["摄像机", "声呐"],
                    raw_value="摄像机和声呐",
                    confidence=0.9,
                    source="user_input",
                ),
            ),
            list_mutations=(),
            unresolved=(),
        )
        field_defs = [{"key": "payload", "type": "list"}]
        resolver = lambda fdef, state: ["摄像机", "声呐", "机械手"]
        res = normalize_task_patch(
            patch_input,
            field_defs,
            {},
            resolver,
            passthrough_keys=set(),
        )
        o = res.slot_outcomes[0]
        self.assertTrue(o.success)
        self.assertEqual(o.normalized_value, ["摄像机", "声呐"])

    def test_list_failure_all_or_nothing(self):
        patch_input = TaskPatch(
            schema_version=1,
            slot_updates=(
                SlotPatch(
                    key="payload",
                    candidate_value=["摄像机", "魔法激光"],
                    raw_value="摄像机和魔法激光",
                    confidence=0.9,
                    source="user_input",
                ),
            ),
            list_mutations=(),
            unresolved=(),
        )
        field_defs = [{"key": "payload", "type": "list"}]
        resolver = lambda fdef, state: ["摄像机", "声呐", "机械手"]
        res = normalize_task_patch(
            patch_input,
            field_defs,
            {},
            resolver,
            passthrough_keys=set(),
        )
        o = res.slot_outcomes[0]
        self.assertFalse(o.success)
        self.assertEqual(o.error_code, "invalid_list")

    def test_raw_success(self):
        patch_input = TaskPatch(
            schema_version=1,
            slot_updates=(
                SlotPatch(
                    key="support_vessel",
                    candidate_value="  海洋石油 201  ",
                    raw_value="海洋石油201",
                    confidence=0.9,
                    source="user_input",
                ),
            ),
            list_mutations=(),
            unresolved=(),
        )
        field_defs = [{"key": "support_vessel", "type": "raw"}]
        res = normalize_task_patch(
            patch_input,
            field_defs,
            {},
            self.dummy_resolver,
            passthrough_keys=set(),
        )
        o = res.slot_outcomes[0]
        self.assertTrue(o.success)
        self.assertEqual(o.normalized_value, "海洋石油 201")

    def test_success_preserves_provenance(self):
        patch_input = TaskPatch(
            schema_version=1,
            slot_updates=(
                SlotPatch(
                    key="water_depth",
                    candidate_value="300米",
                    raw_value="大概三百米左右",
                    confidence=0.92,
                    source="user_input",
                    resolution_method="llm_semantic",
                ),
            ),
            list_mutations=(),
            unresolved=(),
        )
        field_defs = [{"key": "water_depth", "type": "number"}]
        res = normalize_task_patch(
            patch_input,
            field_defs,
            {},
            self.dummy_resolver,
            passthrough_keys=set(),
        )
        o = res.slot_outcomes[0]
        self.assertEqual(o.key, "water_depth")
        self.assertEqual(o.candidate_value, "300米")
        self.assertEqual(o.raw_value, "大概三百米左右")
        self.assertEqual(o.confidence, 0.92)
        self.assertEqual(o.source, "user_input")
        self.assertEqual(o.resolution_method, "llm_semantic")

    def test_failure_preserves_provenance(self):
        patch_input = TaskPatch(
            schema_version=1,
            slot_updates=(
                SlotPatch(
                    key="water_depth",
                    candidate_value="300abc",
                    raw_value="差不多很深",
                    confidence=0.88,
                    source="user_input",
                    resolution_method="llm_semantic",
                ),
            ),
            list_mutations=(),
            unresolved=(),
        )
        field_defs = [{"key": "water_depth", "type": "number"}]
        res = normalize_task_patch(
            patch_input,
            field_defs,
            {},
            self.dummy_resolver,
            passthrough_keys=set(),
        )
        o = res.slot_outcomes[0]
        self.assertEqual(o.candidate_value, "300abc")
        self.assertEqual(o.raw_value, "差不多很深")
        self.assertEqual(o.confidence, 0.88)
        self.assertEqual(o.source, "user_input")
        self.assertEqual(o.resolution_method, "llm_semantic")

    def test_schema_ordering_and_temp_state_chain(self):
        """测试按 field_definitions 顺序遍历，且后续 allowed_values_resolver 依赖前面已规范化的 temp_state。"""
        # field_b 的 allowed values 只有在 state['field_a'] == 'OptionA' 时才包含 'OptionB'
        def dynamic_resolver(fdef, state):
            if fdef["key"] == "field_a":
                return ["OptionA"]
            if fdef["key"] == "field_b":
                if state.get("field_a") == "OptionA":
                    return ["OptionB"]
                return []
            return None

        # 构造 TaskPatch，slot_updates 的顺序为 [field_b, field_a]
        patch_input = TaskPatch(
            schema_version=1,
            slot_updates=(
                SlotPatch(key="field_b", candidate_value="OptionB", raw_value="B", confidence=0.9, source="user_input"),
                SlotPatch(key="field_a", candidate_value="OptionA", raw_value="A", confidence=0.9, source="user_input"),
            ),
            list_mutations=(),
            unresolved=(),
        )
        # field_definitions 定义的顺序为 [field_a, field_b]
        field_defs = [
            {"key": "field_a", "type": "string"},
            {"key": "field_b", "type": "string"},
        ]

        res = normalize_task_patch(
            patch_input,
            field_defs,
            {},
            dynamic_resolver,
            passthrough_keys=set(),
        )
        self.assertEqual(len(res.slot_outcomes), 2)
        outcomes_by_key = {o.key: o for o in res.slot_outcomes}
        # 因为按照 field_defs 顺序跑，field_a 先 normalize 成功并存入 temp_state
        # 跑 field_b 时 resolver 能读到 temp_state['field_a'] == 'OptionA'，所以 field_b 也成功！
        self.assertTrue(outcomes_by_key["field_a"].success)
        self.assertTrue(outcomes_by_key["field_b"].success)
        self.assertEqual(outcomes_by_key["field_b"].normalized_value, "OptionB")

    def test_unknown_field_fail_closed(self):
        patch_input = TaskPatch(
            schema_version=1,
            slot_updates=(
                SlotPatch(key="unknown_slot", candidate_value="val", raw_value="val", confidence=0.9, source="user_input"),
            ),
            list_mutations=(),
            unresolved=(),
        )
        field_defs = [{"key": "water_depth", "type": "number"}]
        with self.assertRaises(NormalizationContractError):
            normalize_task_patch(
                patch_input,
                field_defs,
                {},
                self.dummy_resolver,
                passthrough_keys=set(),
            )

    def test_explicit_passthrough_and_no_normalizer_call(self):
        patch_input = TaskPatch(
            schema_version=1,
            slot_updates=(
                SlotPatch(key="water_depth", candidate_value="300米", raw_value="300米", confidence=0.9, source="user_input"),
                SlotPatch(key="equipment_type", candidate_value="ROV", raw_value="ROV", confidence=0.9, source="user_input"),
            ),
            list_mutations=(),
            unresolved=(),
        )
        field_defs = [{"key": "water_depth", "type": "number"}]

        with patch.object(FieldNormalizer, "normalize", wraps=FieldNormalizer().normalize) as spy_normalize:
            res = normalize_task_patch(
                patch_input,
                field_defs,
                {},
                self.dummy_resolver,
                passthrough_keys={"equipment_type"},
            )

        # water_depth 进入 slot_outcomes
        self.assertEqual(len(res.slot_outcomes), 1)
        self.assertEqual(res.slot_outcomes[0].key, "water_depth")

        # equipment_type 原样进入 passthrough_slot_updates
        self.assertEqual(len(res.passthrough_slot_updates), 1)
        self.assertEqual(res.passthrough_slot_updates[0].key, "equipment_type")
        self.assertEqual(res.passthrough_slot_updates[0].candidate_value, "ROV")

        # 验证 spy_normalize 仅对 water_depth 调用过，未对 equipment_type 调用过
        called_args = spy_normalize.call_args_list
        self.assertEqual(len(called_args), 1)
        self.assertEqual(called_args[0][0][0], "300米")  # raw_value / candidate_value for water_depth

    def test_duplicate_slot_patch_key_fail_closed(self):
        patch_input = TaskPatch(
            schema_version=1,
            slot_updates=(
                SlotPatch(key="water_depth", candidate_value="300米", raw_value="300米", confidence=0.9, source="user_input"),
                SlotPatch(key="water_depth", candidate_value="500米", raw_value="500米", confidence=0.9, source="user_input"),
            ),
            list_mutations=(),
            unresolved=(),
        )
        field_defs = [{"key": "water_depth", "type": "number"}]
        with self.assertRaises(NormalizationContractError):
            normalize_task_patch(
                patch_input,
                field_defs,
                {},
                self.dummy_resolver,
                passthrough_keys=set(),
            )

    def test_duplicate_schema_key_fail_closed(self):
        patch_input = TaskPatch(
            schema_version=1,
            slot_updates=(
                SlotPatch(key="water_depth", candidate_value="300米", raw_value="300米", confidence=0.9, source="user_input"),
            ),
            list_mutations=(),
            unresolved=(),
        )
        field_defs = [
            {"key": "water_depth", "type": "number"},
            {"key": "water_depth", "type": "number"},
        ]
        with self.assertRaises(NormalizationContractError):
            normalize_task_patch(
                patch_input,
                field_defs,
                {},
                self.dummy_resolver,
                passthrough_keys=set(),
            )

    def test_normalize_task_patch_no_side_effects(self):
        patch_input = TaskPatch(
            schema_version=1,
            slot_updates=(
                SlotPatch(key="water_depth", candidate_value="300米", raw_value="300米", confidence=0.9, source="user_input"),
            ),
            list_mutations=(
                ListMutationPatch(
                    field="payload",
                    operation="add",
                    items=("摄像机",),
                    target_items=(),
                    raw_text="加摄像机",
                    confidence=0.9,
                    source="user_input",
                ),
            ),
            unresolved=("未解解析点",),
        )
        field_defs = [{"key": "water_depth", "type": "number"}]
        current_state = {"water_depth": 100.0, "other": "val"}

        patch_copy = copy.deepcopy(patch_input)
        field_defs_copy = copy.deepcopy(field_defs)
        state_copy = copy.deepcopy(current_state)

        res = normalize_task_patch(
            patch_input,
            field_defs,
            current_state,
            self.dummy_resolver,
            passthrough_keys=set(),
        )

        self.assertEqual(patch_input, patch_copy)
        self.assertEqual(field_defs, field_defs_copy)
        self.assertEqual(current_state, state_copy)

        self.assertEqual(res.list_mutations, patch_input.list_mutations)
        self.assertEqual(res.unresolved, patch_input.unresolved)


class TestL3PrimitiveParityMatrix(unittest.TestCase):
    """L3 Primitive Parity Tests (FieldNormalizer vs NormalizationContract)"""

    def setUp(self):
        self.fn = FieldNormalizer()
        self.dummy_resolver = lambda fdef, state: None

    def _check_parity(self, candidate_val, field_type, allowed=None):
        legacy_val = self.fn.normalize(candidate_val, allowed, field_type)

        field_defs = [{"key": "test_field", "type": field_type}]
        resolver = lambda fdef, state: allowed

        patch_input = TaskPatch(
            schema_version=1,
            slot_updates=(
                SlotPatch(
                    key="test_field",
                    candidate_value=candidate_val,
                    raw_value=str(candidate_val),
                    confidence=0.9,
                    source="user_input",
                ),
            ),
            list_mutations=(),
            unresolved=(),
        )

        res = normalize_task_patch(
            patch_input,
            field_defs,
            {},
            resolver,
            passthrough_keys=set(),
        )
        o = res.slot_outcomes[0]

        if legacy_val is not None:
            self.assertTrue(o.success, f"Legacy succeeded with {legacy_val!r} but V2 outcome failed for {candidate_val!r}")
            self.assertEqual(o.normalized_value, legacy_val)
            self.assertIsNone(o.error_code)
        else:
            self.assertFalse(o.success, f"Legacy failed with None but V2 outcome succeeded with {o.normalized_value!r} for {candidate_val!r}")
            self.assertIsNone(o.normalized_value)
            self.assertIsNotNone(o.error_code)

    def test_number_parity(self):
        self._check_parity("300米", "number")
        self._check_parity("12.3456", "number")
        self._check_parity("300abc", "number")

    def test_datetime_parity(self):
        self._check_parity("2026-08-10 09:00:00", "datetime")
        self._check_parity("2026-08-10T09:00:00", "datetime")
        self._check_parity("随便什么时候", "datetime")

    def test_coord_parity(self):
        self._check_parity("北纬20度，东经110度", "coord")
        self._check_parity("invalid coord str", "coord")

    def test_string_tasktype_parity(self):
        allowed = ["管缆巡检", "水下搜寻"]
        self._check_parity("管缆巡检", "tasktype", allowed)
        self._check_parity("巡检", "tasktype", allowed)  # matching fails if not exact
        self._check_parity("火星勘探", "tasktype", allowed)

    def test_list_parity(self):
        allowed = ["摄像机", "声呐", "机械手"]
        self._check_parity(["摄像机", "声呐"], "list", allowed)
        self._check_parity(["摄像机", "未知设备"], "list", allowed)

    def test_raw_parity(self):
        self._check_parity("  测试船 1 号  ", "raw")


class TestL4RealSchemaAndPassthroughBoundary(unittest.TestCase):
    """L4 Real Schema & Passthrough Boundary Integration Tests"""

    def setUp(self):
        self.kb = KnowledgeBase()
        self.builder = OutputBuilder(self.kb)
        self.real_pipeline_schema = self.builder.get_schema("pipeline_inspection", "normal")
        self.equipment_passthrough_keys = {
            "equipment_class",
            "equipment_family",
            "equipment_specification",
            "equipment_type",
            "equipment_name",
            "equipment_unit_id",
        }

    def test_schema_field_explicit_passthrough_takes_precedence(self):
        """验证即使字段属于 schema field_definitions，一旦显式声明为 passthrough_keys 则优先 Passthrough，不进入 normalizer。"""
        field_defs = [{"key": "equipment_type", "type": "string"}]
        patch_input = TaskPatch(
            schema_version=1,
            slot_updates=(
                SlotPatch(key="equipment_type", candidate_value="ROV", raw_value="ROV", confidence=0.9, source="user_input"),
            ),
            list_mutations=(),
            unresolved=(),
        )

        with patch.object(FieldNormalizer, "normalize", wraps=FieldNormalizer().normalize) as spy_normalize:
            res = normalize_task_patch(
                patch_input,
                field_defs,
                {},
                lambda f, s: None,
                passthrough_keys={"equipment_type"},
            )

        self.assertEqual(len(res.slot_outcomes), 0)
        self.assertEqual(len(res.passthrough_slot_updates), 1)
        self.assertEqual(res.passthrough_slot_updates[0].key, "equipment_type")
        self.assertEqual(res.passthrough_slot_updates[0].candidate_value, "ROV")
        spy_normalize.assert_not_called()

    def test_real_schema_equipment_type_is_passthrough(self):
        """验证使用真实 task_schemas.yaml 导出的 pipeline_inspection schema 时，equipment_type 正确 Passthrough。"""
        patch_input = TaskPatch(
            schema_version=1,
            slot_updates=(
                SlotPatch(key="water_depth", candidate_value="300米", raw_value="300米", confidence=0.9, source="user_input"),
                SlotPatch(key="equipment_type", candidate_value="观测级ROV", raw_value="观测级ROV", confidence=0.9, source="user_input"),
            ),
            list_mutations=(),
            unresolved=(),
        )

        resolver = lambda fdef, state: self.builder._resolve_allowed(fdef, "pipeline_inspection", state)

        res = normalize_task_patch(
            patch_input,
            self.real_pipeline_schema,
            {},
            resolver,
            passthrough_keys=self.equipment_passthrough_keys,
        )

        outcome_keys = {o.key for o in res.slot_outcomes}
        passthrough_keys = {p.key for p in res.passthrough_slot_updates}

        self.assertIn("water_depth", outcome_keys)
        self.assertNotIn("equipment_type", outcome_keys)
        self.assertIn("equipment_type", passthrough_keys)
        self.assertNotIn("water_depth", passthrough_keys)
        self.assertEqual(outcome_keys & passthrough_keys, set())

    def test_real_schema_equipment_specification_is_passthrough(self):
        """验证真实 schema 下 equipment_specification 复杂 object 结构可被 Passthrough，不触发 unsupported_field_type。"""
        spec_dict = {"variant_id": "var_001", "type": "ROV", "value": 250}
        patch_input = TaskPatch(
            schema_version=1,
            slot_updates=(
                SlotPatch(
                    key="equipment_specification",
                    candidate_value=spec_dict,
                    raw_value="ROV 250规格",
                    confidence=0.95,
                    source="user_input",
                ),
            ),
            list_mutations=(),
            unresolved=(),
        )

        resolver = lambda fdef, state: self.builder._resolve_allowed(fdef, "pipeline_inspection", state)

        res = normalize_task_patch(
            patch_input,
            self.real_pipeline_schema,
            {},
            resolver,
            passthrough_keys=self.equipment_passthrough_keys,
        )

        self.assertEqual(len(res.slot_outcomes), 0)
        self.assertEqual(len(res.passthrough_slot_updates), 1)
        p = res.passthrough_slot_updates[0]
        self.assertEqual(p.key, "equipment_specification")
        self.assertEqual(p.candidate_value, spec_dict)
        self.assertEqual(p.raw_value, "ROV 250规格")

    def test_real_schema_passthrough_does_not_call_normalizer(self):
        """验证在真实 schema 下，water_depth 触发 FieldNormalizer，而 equipment_type 与 equipment_specification 不触发。"""
        spec_dict = {"variant_id": "var_001", "type": "ROV", "value": 250}
        patch_input = TaskPatch(
            schema_version=1,
            slot_updates=(
                SlotPatch(key="water_depth", candidate_value="300米", raw_value="300米", confidence=0.9, source="user_input"),
                SlotPatch(key="equipment_type", candidate_value="观测级ROV", raw_value="观测级ROV", confidence=0.9, source="user_input"),
                SlotPatch(key="equipment_specification", candidate_value=spec_dict, raw_value="ROV 250", confidence=0.9, source="user_input"),
            ),
            list_mutations=(),
            unresolved=(),
        )

        resolver = lambda fdef, state: self.builder._resolve_allowed(fdef, "pipeline_inspection", state)

        with patch.object(FieldNormalizer, "normalize", wraps=FieldNormalizer().normalize) as spy_normalize:
            res = normalize_task_patch(
                patch_input,
                self.real_pipeline_schema,
                {},
                resolver,
                passthrough_keys=self.equipment_passthrough_keys,
            )

        self.assertEqual(len(res.slot_outcomes), 1)
        self.assertEqual(res.slot_outcomes[0].key, "water_depth")
        self.assertEqual(len(res.passthrough_slot_updates), 2)

        called_first_args = [call[0][0] for call in spy_normalize.call_args_list]
        self.assertIn("300米", called_first_args)
        self.assertNotIn("观测级ROV", called_first_args)
        self.assertNotIn(spec_dict, called_first_args)

    def test_stage1_task_type_key_explicit_passthrough(self):
        """验证 Stage1 产生的 task_type_key 可作为显式 passthrough_keys，与 task_type 分立。"""
        patch_input = TaskPatch(
            schema_version=1,
            slot_updates=(
                SlotPatch(key="task_type", candidate_value="管缆巡检", raw_value="管缆巡检", confidence=0.9, source="user_input"),
                SlotPatch(key="task_type_key", candidate_value="pipeline_inspection", raw_value="pipeline_inspection", confidence=0.9, source="user_input"),
            ),
            list_mutations=(),
            unresolved=(),
        )

        resolver = lambda fdef, state: self.builder._resolve_allowed(fdef, "pipeline_inspection", state)

        res = normalize_task_patch(
            patch_input,
            self.real_pipeline_schema,
            {},
            resolver,
            passthrough_keys={"task_type_key"},
        )

        self.assertEqual(len(res.slot_outcomes), 1)
        self.assertEqual(res.slot_outcomes[0].key, "task_type")
        self.assertTrue(res.slot_outcomes[0].success)

        self.assertEqual(len(res.passthrough_slot_updates), 1)
        self.assertEqual(res.passthrough_slot_updates[0].key, "task_type_key")
        self.assertEqual(res.passthrough_slot_updates[0].candidate_value, "pipeline_inspection")

    def test_real_schema_support_vessel_uses_string_contract(self):
        """验证在真实 schema 下，support_vessel 按照 type=string 与 vessel_ids 校验。"""
        patch_input_succ = TaskPatch(
            schema_version=1,
            slot_updates=(
                SlotPatch(key="support_vessel", candidate_value="海洋石油681", raw_value="海洋石油681", confidence=0.9, source="user_input"),
            ),
            list_mutations=(),
            unresolved=(),
        )
        patch_input_fail = TaskPatch(
            schema_version=1,
            slot_updates=(
                SlotPatch(key="support_vessel", candidate_value="海贼王号", raw_value="海贼王号", confidence=0.9, source="user_input"),
            ),
            list_mutations=(),
            unresolved=(),
        )

        resolver = lambda fdef, state: self.builder._resolve_allowed(fdef, "pipeline_inspection", state)

        res_succ = normalize_task_patch(
            patch_input_succ,
            self.real_pipeline_schema,
            {},
            resolver,
            passthrough_keys=self.equipment_passthrough_keys,
        )
        self.assertTrue(res_succ.slot_outcomes[0].success)
        self.assertEqual(res_succ.slot_outcomes[0].normalized_value, "海洋石油681")

        res_fail = normalize_task_patch(
            patch_input_fail,
            self.real_pipeline_schema,
            {},
            resolver,
            passthrough_keys=self.equipment_passthrough_keys,
        )
        self.assertFalse(res_fail.slot_outcomes[0].success)
        self.assertEqual(res_fail.slot_outcomes[0].error_code, "invalid_enum")

    def test_unknown_field_still_fails_closed_with_passthrough_support(self):
        """验证非 schema 字段且不在 passthrough_keys 白名单中时，依然产生 NormalizationContractError。"""
        patch_input = TaskPatch(
            schema_version=1,
            slot_updates=(
                SlotPatch(key="unauthorized_field", candidate_value="val", raw_value="val", confidence=0.9, source="user_input"),
            ),
            list_mutations=(),
            unresolved=(),
        )
        resolver = lambda fdef, state: self.builder._resolve_allowed(fdef, "pipeline_inspection", state)

        with self.assertRaises(NormalizationContractError) as ctx:
            normalize_task_patch(
                patch_input,
                self.real_pipeline_schema,
                {},
                resolver,
                passthrough_keys=self.equipment_passthrough_keys,
            )
        self.assertIn("既不在 schema field_definitions 中，也不在 passthrough_keys 白名单中", str(ctx.exception))

    def test_resolver_exception_has_field_context(self):
        """验证当 allowed_values_resolver 抛出异常时，抓取并包装为带有字段 key 上下文的 NormalizationContractError，并保留 __cause__。"""
        field_defs = [{"key": "water_depth", "type": "number"}]
        patch_input = TaskPatch(
            schema_version=1,
            slot_updates=(
                SlotPatch(key="water_depth", candidate_value="300米", raw_value="300米", confidence=0.9, source="user_input"),
            ),
            list_mutations=(),
            unresolved=(),
        )

        def faulty_resolver(fdef, state):
            raise ValueError("Database connection failed")

        with self.assertRaises(NormalizationContractError) as ctx:
            normalize_task_patch(
                patch_input,
                field_defs,
                {},
                faulty_resolver,
                passthrough_keys=set(),
            )

        self.assertIn("allowed_values_resolver failed for field 'water_depth'", str(ctx.exception))
        self.assertIsInstance(ctx.exception.__cause__, ValueError)
        self.assertEqual(str(ctx.exception.__cause__), "Database connection failed")


if __name__ == "__main__":
    unittest.main()
