"""
tests/test_grounded_recommendation.py

针对 DialogueManager._build_grounded_recommendation 的单元测试。

覆盖场景：
1. plan.relation != "recommend" → 返回 None，不拦截
2. subject_type 无对应 target_key → 返回 None，不拦截
3. allowed_values 为空（字段未解析） → 返回 None，不拦截
4. LLM subject_text 精确匹配 allowed_values → 使用 subject_text 推荐
5. LLM subject_text 不匹配、唯一候选 → 直接推荐唯一候选（不报错）
6. LLM subject_text 不匹配、多个候选 → 列出全部候选并建议第一个（不报错）
7. LLM subject_text 为 None、多个候选 → 列出全部候选并建议第一个
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
    # Case 6: LLM subject_text 不匹配、多个候选 → 列出全部，建议第一个，不报错
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
        # 第一个为建议值
        self.assertIn("观察级ROV", result)
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


if __name__ == "__main__":
    unittest.main()
