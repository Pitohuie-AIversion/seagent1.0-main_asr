"""
tests/test_interaction_plan.py - InteractionPlan 与 validate_interaction_plan 单元测试
"""

import math
import unittest
from src.interaction_plan import (
    InteractionPlan,
    build_clarify_fallback_plan,
    validate_interaction_plan,
)
from src.intent_router import IntentRouteResult


class TestInteractionPlanSchemaAndValidation(unittest.TestCase):
    def test_plan_instantiation_and_immutability(self):
        plan = InteractionPlan(
            schema_version=1,
            operation="READ",
            dialogue_mode="knowledge_qa",
            query_intent="DEVICE_CAPABILITY",
            subject_type="device",
            subject_text="金牛座",
            relation="describe",
            source_policy="project_kb",
            needs_clarification=False,
            clarification_reason=None,
            emergency_action=None,
            confidence=0.95,
            reason_code="DEVICE_DESCRIBE",
        )
        self.assertEqual(plan.operation, "READ")
        self.assertEqual(plan.subject_text, "金牛座")
        with self.assertRaises(Exception):
            plan.operation = "WRITE"  # type: ignore (frozen dataclass)

    def test_to_dict_and_to_intent_route_result(self):
        plan = InteractionPlan(
            schema_version=1,
            operation="WRITE",
            dialogue_mode="task_collection",
            query_intent=None,
            subject_type="task",
            subject_text="当前任务",
            relation="filled_fields",
            source_policy="session_state",
            needs_clarification=False,
            clarification_reason=None,
            emergency_action=None,
            confidence=0.9,
            reason_code="WRITE_PARAM",
        )
        d = plan.to_dict()
        self.assertEqual(d["operation"], "WRITE")
        self.assertEqual(d["dialogue_mode"], "task_collection")

        route_res = plan.to_intent_route_result()
        self.assertIsInstance(route_res, IntentRouteResult)
        self.assertEqual(route_res.interaction_type, "WRITE")
        self.assertEqual(route_res.dialogue_mode, "task_collection")
        self.assertIsNotNone(route_res.interaction_plan)
        self.assertEqual(route_res.interaction_plan.reason_code, "WRITE_PARAM")

    def test_valid_plan_validation(self):
        raw_read = {
            "schema_version": 1,
            "operation": "READ",
            "dialogue_mode": "knowledge_qa",
            "query_intent": "DEVICE_CAPABILITY",
            "subject_type": "device",
            "subject_text": "金牛座",
            "relation": "belongs_to",
            "source_policy": "project_kb",
            "needs_clarification": False,
            "clarification_reason": None,
            "emergency_action": None,
            "confidence": 0.95,
            "reason_code": "TEST_VALID_READ",
        }
        plan = validate_interaction_plan(raw_read)
        self.assertEqual(plan.operation, "READ")
        self.assertEqual(plan.relation, "belongs_to")

    def test_protocol_exceptions_fallback_to_clarify(self):
        # 1. 非 dict 或 InteractionPlan 结构
        plan1 = validate_interaction_plan("not a plan")
        self.assertEqual(plan1.operation, "CLARIFY")
        self.assertTrue(plan1.needs_clarification)

        # 2. READ 的异常 confidence 退化为 0，不阻断无副作用回答
        raw_nan = {
            "operation": "READ",
            "dialogue_mode": "knowledge_qa",
            "confidence": float("nan"),
            "source_policy": "project_kb",
        }
        plan_nan = validate_interaction_plan(raw_nan)
        self.assertEqual(plan_nan.operation, "READ")
        self.assertEqual(plan_nan.confidence, 0.0)

        # 3. READ 的 Inf 同样作为非关键元数据降级
        raw_inf = {
            "operation": "READ",
            "dialogue_mode": "knowledge_qa",
            "confidence": float("inf"),
            "source_policy": "project_kb",
        }
        plan_inf = validate_interaction_plan(raw_inf)
        self.assertEqual(plan_inf.operation, "READ")
        self.assertEqual(plan_inf.confidence, 0.0)

        # 4. 置信度过低 (< 0.6)
        raw_low = {
            "operation": "WRITE",
            "dialogue_mode": "task_collection",
            "confidence": 0.4,
            "source_policy": "session_state",
        }
        plan_low = validate_interaction_plan(raw_low)
        self.assertEqual(plan_low.operation, "CLARIFY")

        # 5. 非法枚举值
        raw_bad_enum = {
            "operation": "SUPER_WRITE",
            "dialogue_mode": "task_collection",
            "confidence": 0.9,
            "source_policy": "session_state",
        }
        plan_enum = validate_interaction_plan(raw_bad_enum)
        self.assertEqual(plan_enum.operation, "CLARIFY")

        # 6. CONTROL 缺失合法 emergency_action
        raw_control_no_act = {
            "operation": "CONTROL",
            "dialogue_mode": "emergency_intervention",
            "emergency_action": "destroy_robot",  # 非法动作
            "confidence": 0.95,
            "source_policy": "session_state",
        }
        plan_ctrl = validate_interaction_plan(raw_control_no_act)
        self.assertEqual(plan_ctrl.operation, "CLARIFY")

        # 7. READ 携带多余控制元数据时丢弃副作用字段
        raw_read_with_action = {
            "operation": "READ",
            "dialogue_mode": "knowledge_qa",
            "emergency_action": "stop",
            "confidence": 0.95,
            "source_policy": "project_kb",
        }
        plan_read_act = validate_interaction_plan(raw_read_with_action)
        self.assertEqual(plan_read_act.operation, "READ")
        self.assertIsNone(plan_read_act.emergency_action)

        # 8. dialogue_mode 是冗余字段，WRITE 模式由 operation 推导
        raw_write_conflict = {
            "operation": "WRITE",
            "dialogue_mode": "knowledge_qa",
            "confidence": 0.9,
            "source_policy": "session_state",
        }
        plan_w_conf = validate_interaction_plan(raw_write_conflict)
        self.assertEqual(plan_w_conf.operation, "WRITE")
        self.assertEqual(plan_w_conf.dialogue_mode, "task_collection")

    def test_write_plan_is_validated_independently_of_wording(self):
        raw_write = {
            "schema_version": 1,
            "operation": "WRITE",
            "dialogue_mode": "task_collection",
            "confidence": 0.95,
            "reason_code": "MODEL_WRITE",
            "source_policy": "session_state",
        }
        natural_variants = [
            "那就照你刚才说的做",
            "深度别太大，三百米吧",
            "第二台挺合适，就它了",
            "水深改成五百米，顺便说说风险",
        ]
        for message in natural_variants:
            validated = validate_interaction_plan(raw_write, user_message=message)
            self.assertEqual(validated.operation, "WRITE")


if __name__ == "__main__":
    unittest.main()
