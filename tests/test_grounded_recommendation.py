"""
tests/test_grounded_recommendation.py

针对 DialogueManager._build_grounded_recommendation 的单元测试。

覆盖场景：
1. plan.relation != "recommend" → 返回 None，不拦截
2. subject_type 无对应 target_key → 返回 None，不拦截
3. allowed_values 为空（字段未解析） → 返回 None，不拦截
4. LLM subject_text 精确匹配 allowed_values → 使用 subject_text 推荐
5. LLM subject_text 不匹配、唯一候选 → 直接推荐唯一候选（不报错）
6. LLM subject_text 不匹配、多个候选 → 语义解析失败时列出并询问偏好
7. LLM subject_text 为 None、多个候选 → 列出候选但不按顺序猜测
"""
from __future__ import annotations

import types
import unittest
from unittest.mock import MagicMock, patch


def _make_plan(relation, subject_type, subject_text):
    """构造最小 InteractionPlan mock。"""
    plan = MagicMock()
    plan.operation = "READ"
    plan.relation = relation
    plan.subject_type = subject_type
    plan.subject_text = subject_text
    return plan


def _make_route(plan):
    route = MagicMock()
    route.interaction_plan = plan
    return route


class TestBuildGroundedRecommendation(unittest.TestCase):
    """单元测试：直接调用 DialogueManager._build_grounded_recommendation。"""

    def _make_manager(self, missing_field_def=None, task_state=None):
        """构造最小 DialogueManager 实例，绕过 __init__ 中的重型依赖。"""
        from src.dialogue_manager import DialogueManager

        mgr = object.__new__(DialogueManager)
        # 注入所需属性
        mgr.task_state = task_state or {}
        mgr.conversation_history = []
        mgr.extractor = MagicMock()
        mgr.extractor.resolve_allowed_candidate.return_value = None
        mgr._last_missing = []
        if missing_field_def is not None:
            mgr._missing_field_definition = lambda key: (
                missing_field_def if key == missing_field_def.get("key") else None
            )
        else:
            mgr._missing_field_definition = lambda key: None
        return mgr

    # ------------------------------------------------------------------
    # Case 1: plan.relation != "recommend" → None
    # ------------------------------------------------------------------
    def test_non_recommend_relation_returns_none(self):
        from src.dialogue_manager import DialogueManager

        mgr = self._make_manager()
        plan = _make_plan(relation="list", subject_type="device_class", subject_text="AUV")
        route = _make_route(plan)
        result = DialogueManager._build_grounded_recommendation(mgr, route)
        self.assertIsNone(result, "非 recommend 关系应返回 None")

    # ------------------------------------------------------------------
    # Case 2: subject_type 无 target_key → None
    # ------------------------------------------------------------------
    def test_unknown_subject_type_returns_none(self):
        from src.dialogue_manager import DialogueManager

        mgr = self._make_manager()
        plan = _make_plan(relation="recommend", subject_type="unknown", subject_text="某值")
        route = _make_route(plan)
        result = DialogueManager._build_grounded_recommendation(mgr, route)
        self.assertIsNone(result, "未知 subject_type 无对应 target_key，应返回 None")

    # ------------------------------------------------------------------
    # Case 3: allowed_values 为空 → None
    # ------------------------------------------------------------------
    def test_empty_allowed_values_returns_none(self):
        from src.dialogue_manager import DialogueManager

        field_def = {"key": "equipment_class", "allowed_values": []}
        mgr = self._make_manager(missing_field_def=field_def)
        plan = _make_plan(relation="recommend", subject_type="device_class", subject_text="某值")
        route = _make_route(plan)
        result = DialogueManager._build_grounded_recommendation(mgr, route)
        self.assertIsNone(result, "allowed_values 为空应返回 None，不拦截")

    # ------------------------------------------------------------------
    # Case 4: LLM subject_text 精确匹配 → 使用该值推荐
    # ------------------------------------------------------------------
    def test_exact_match_uses_subject_text(self):
        from src.dialogue_manager import DialogueManager

        field_def = {"key": "equipment_class", "allowed_values": ["观察级ROV", "AUV"]}
        mgr = self._make_manager(missing_field_def=field_def)
        plan = _make_plan(relation="recommend", subject_type="device_class", subject_text="AUV")
        route = _make_route(plan)
        result = DialogueManager._build_grounded_recommendation(mgr, route)
        self.assertIsNotNone(result)
        self.assertIn("AUV", result)
        self.assertNotIn("无法", result)
        self.assertNotIn("选项有", result)

    # ------------------------------------------------------------------
    # Case 5: LLM subject_text 不匹配、唯一候选 → 推荐该候选，不报错
    # ------------------------------------------------------------------
    def test_mismatch_single_candidate_recommends_it(self):
        from src.dialogue_manager import DialogueManager

        field_def = {"key": "equipment_class", "allowed_values": ["工作级ROV"]}
        mgr = self._make_manager(missing_field_def=field_def)
        # LLM 生成了幻觉名称"重量级ROV"
        plan = _make_plan(relation="recommend", subject_type="device_class", subject_text="重量级ROV")
        route = _make_route(plan)
        result = DialogueManager._build_grounded_recommendation(mgr, route)
        self.assertIsNotNone(result)
        # 必须推荐配置中的合法值
        self.assertIn("工作级ROV", result)
        # 绝不出现原来的错误消息
        self.assertNotIn("无法从当前任务", result)
        self.assertNotIn("重量级ROV", result)

    # ------------------------------------------------------------------
    # Case 6: LLM subject_text 不匹配、多个候选 → 列出全部并询问偏好
    # ------------------------------------------------------------------
    def test_mismatch_multiple_candidates_lists_all(self):
        from src.dialogue_manager import DialogueManager

        field_def = {"key": "equipment_class", "allowed_values": ["观察级ROV", "AUV"]}
        mgr = self._make_manager(missing_field_def=field_def)
        # LLM 生成了不存在的名称
        plan = _make_plan(relation="recommend", subject_type="device_class", subject_text="重量级ROV")
        route = _make_route(plan)
        result = DialogueManager._build_grounded_recommendation(mgr, route)
        self.assertIsNotNone(result)
        # 两个候选都应出现
        self.assertIn("观察级ROV", result)
        self.assertIn("AUV", result)
        self.assertNotIn("建议选择【观察级ROV】", result)
        self.assertIn("请补充偏好", result)
        # 不出现错误消息
        self.assertNotIn("无法从当前任务", result)
        self.assertNotIn("重量级ROV", result)

    # ------------------------------------------------------------------
    # Case 7: subject_text 为 None、多个候选 → 同样正常列出
    # ------------------------------------------------------------------
    def test_none_subject_text_multiple_candidates_lists_all(self):
        from src.dialogue_manager import DialogueManager

        field_def = {"key": "equipment_class", "allowed_values": ["观察级ROV", "AUV"]}
        mgr = self._make_manager(missing_field_def=field_def)
        plan = _make_plan(relation="recommend", subject_type="device_class", subject_text=None)
        route = _make_route(plan)
        result = DialogueManager._build_grounded_recommendation(mgr, route)
        self.assertIsNotNone(result)
        self.assertIn("观察级ROV", result)
        self.assertIn("AUV", result)
        self.assertNotIn("建议选择【观察级ROV】", result)
        self.assertNotIn("无法从当前任务", result)

    # ------------------------------------------------------------------
    # Case 8: 有任务名时，回复中包含任务前缀
    # ------------------------------------------------------------------
    def test_task_prefix_included_when_task_name_exists(self):
        from src.dialogue_manager import DialogueManager

        field_def = {"key": "equipment_class", "allowed_values": ["观察级ROV"]}
        mgr = self._make_manager(
            missing_field_def=field_def,
            task_state={"task_type": "管缆巡检"},
        )
        plan = _make_plan(relation="recommend", subject_type="device_class", subject_text=None)
        route = _make_route(plan)
        result = DialogueManager._build_grounded_recommendation(mgr, route)
        self.assertIsNotNone(result)
        self.assertIn("管缆巡检", result)


class TestScopeConfirmedRecommendation(unittest.TestCase):
    """回归测试：_scope_confirmed_recommendation 的候选保留行为。

    修复 Bug：当 valid_provenance=False 时，extractor 已抽取的 robot cascade
    candidates（equipment_unit_id、equipment_type 等）不应被清空，应由后续
    _handle_equipment_updates_in_transaction 正常处理。
    """

    def _make_manager(self, conversation_history=None):
        from src.dialogue_manager import DialogueManager

        mgr = object.__new__(DialogueManager)
        mgr.task_state = {}
        mgr.conversation_history = conversation_history or []
        mgr._last_missing = []

        def _missing_field_definition(key):
            # equipment_type 有两个候选
            if key == "equipment_type":
                return {
                    "key": "equipment_type",
                    "allowed_values": ["轻型工作级深海机器人 150HP", "观察级深海机器人 75HP"],
                }
            return None

        mgr._missing_field_definition = _missing_field_definition
        return mgr

    def _make_write_recommend_plan(self, subject_type="device", subject_text="轻型工作级深海机器人 150HP"):
        plan = MagicMock()
        plan.operation = "WRITE"
        plan.relation = "recommend"
        plan.subject_type = subject_type
        plan.subject_text = subject_text
        plan.confidence = 0.9
        return plan

    # ------------------------------------------------------------------
    # Bug 修复回归测试：valid_provenance=False 时不清空 robot cascade candidates
    # ------------------------------------------------------------------
    def test_invalid_provenance_preserves_extractor_robot_cascade_candidates(self):
        """当 valid_provenance=False 时，extractor 抽取的 robot cascade candidates 必须保留。

        场景：用户输入 "001"，TurnPlanner 路由为 WRITE+recommend+device，
        但 subject_text 不在上一轮 assistant 消息中（valid_provenance=False）。
        extractor 已从 "001" 中正确抽取出 equipment_unit_id 和 equipment_type，
        这些 candidates 必须保留以供后续写入。
        """
        from src.dialogue_manager import DialogueManager

        # 上一轮 assistant 消息：推荐了具体机器人
        history = [
            {"role": "assistant", "content": "建议使用轻型工作级深海机器人 150HP 执行本任务。"}
        ]
        mgr = self._make_manager(conversation_history=history)

        # extractor 已从 "001" 抽出了 unit_id 和 type 候选
        extraction_result = {
            "slot_candidates": [
                {
                    "raw_key": "设备编号",
                    "canonical_key": "equipment_unit_id",
                    "raw_value": "001",
                    "normalized_value": "LROV-150-001",
                    "confidence": 0.95,
                },
                {
                    "raw_key": "设备型号",
                    "canonical_key": "equipment_type",
                    "raw_value": "001",
                    "normalized_value": "轻型工作级深海机器人 150HP",
                    "confidence": 0.9,
                },
                {
                    "raw_key": "其他字段",
                    "canonical_key": "support_vessel",
                    "raw_value": "海工01",
                    "normalized_value": "海工01",
                    "confidence": 0.8,
                },
            ],
            "list_mutations": [],
            "unresolved": [],
        }

        # plan.subject_text = "LROV-150-001" 不在 allowed_values ["轻型工作级深海机器人 150HP", ...]
        # 且 "LROV-150-001" 不在 previous_assistant → valid_provenance = False
        plan = self._make_write_recommend_plan(
            subject_type="device",
            subject_text="LROV-150-001",  # 不在 allowed_values，provenance 必然 False
        )

        result = DialogueManager._scope_confirmed_recommendation(mgr, extraction_result, plan, "001")

        # robot cascade candidates 必须全部保留
        result_keys = [c["canonical_key"] for c in result["slot_candidates"]]
        self.assertIn(
            "equipment_unit_id", result_keys,
            "valid_provenance=False 时，equipment_unit_id candidate 不得被清空"
        )
        self.assertIn(
            "equipment_type", result_keys,
            "valid_provenance=False 时，equipment_type candidate 不得被清空"
        )
        # 非 robot cascade 字段同样保留
        self.assertIn(
            "support_vessel", result_keys,
            "非 robot cascade 字段 support_vessel 不得被清空"
        )
        # unresolved 中有提示信息，但不阻断写入
        self.assertTrue(
            any("无法验证" in u for u in result.get("unresolved", [])),
            "valid_provenance=False 时应记录 unresolved 提示"
        )

    def test_valid_provenance_uses_recommendation_and_strips_other_cascade(self):
        """valid_provenance=True 时，应用推荐协议并清除其他 robot cascade extractor 候选。"""
        from src.dialogue_manager import DialogueManager

        # 上一轮 assistant 消息明确提到了推荐值
        history = [
            {"role": "assistant", "content": "我明确推荐作业设备型号【轻型工作级深海机器人 150HP】。"}
        ]
        mgr = self._make_manager(conversation_history=history)

        # extractor 也抽到了一个 equipment_type（可能不同）
        extraction_result = {
            "slot_candidates": [
                {
                    "raw_key": "设备型号",
                    "canonical_key": "equipment_type",
                    "raw_value": "确认",
                    "normalized_value": "观察级深海机器人 75HP",  # 与推荐值不同
                    "confidence": 0.6,
                },
            ],
            "list_mutations": [],
            "unresolved": [],
        }

        # plan.subject_text = "轻型工作级深海机器人 150HP" 在 allowed_values 且在 previous_assistant
        # → valid_provenance = True
        plan = self._make_write_recommend_plan(
            subject_type="device",
            subject_text="轻型工作级深海机器人 150HP",
        )

        result = DialogueManager._scope_confirmed_recommendation(mgr, extraction_result, plan, "确认")

        result_keys = [c["canonical_key"] for c in result["slot_candidates"]]
        # extractor 抽到的与推荐值不同的 equipment_type 应被清除（已被推荐协议替换）
        result_values = {c["canonical_key"]: c["normalized_value"] for c in result["slot_candidates"]}

        # 推荐协议注入的 candidate 应该存在，且值为推荐值
        self.assertIn("equipment_type", result_keys, "推荐协议应注入 equipment_type candidate")
        self.assertEqual(
            result_values.get("equipment_type"), "轻型工作级深海机器人 150HP",
            "推荐协议注入的值应为 valid_provenance 通过的推荐值，而不是 extractor 候选值"
        )
        # 不应有 unresolved
        self.assertEqual(result.get("unresolved", []), [], "valid_provenance=True 时不应有 unresolved")


if __name__ == "__main__":
    unittest.main()

