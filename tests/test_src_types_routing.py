"""
单元测试：src/types/routing_types.py 共享路由类型
验证 IntentRouteResult 验证逻辑、常量复用、循环依赖消除后的双向导入正常。
"""

import pytest

from src.types import (
    InteractionType,
    DialogueMode,
    VALID_INTERACTION_TYPES,
    VALID_DIALOGUE_MODES,
    VALID_EMERGENCY_ACTIONS,
    VALID_QUERY_INTENTS,
    IntentRoutingError,
    IntentRouteResult,
)


# ── 常量正确性 ──────────────────────────────────────────────────────────────

class TestRoutingConstants:
    def test_valid_interaction_types(self):
        assert VALID_INTERACTION_TYPES == {"WRITE", "QUERY"}

    def test_valid_dialogue_modes(self):
        assert VALID_DIALOGUE_MODES == {"task_collection", "knowledge_qa", "emergency_intervention"}

    def test_valid_emergency_actions(self):
        assert VALID_EMERGENCY_ACTIONS == {"stop", "pause", "abort", "cancel"}

    def test_valid_query_intents_contains(self):
        # 只验证几个关键成员，避免写死整个集合的顺序
        for key in ("TASK_STATUS", "KNOWLEDGE_QA", "DEVICE_STATUS", "CLARIFICATION", "UNKNOWN"):
            assert key in VALID_QUERY_INTENTS

    def test_intent_routing_error_is_exception(self):
        assert issubclass(IntentRoutingError, Exception)
        with pytest.raises(IntentRoutingError):
            raise IntentRoutingError("boom")


# ── IntentRouteResult 基本构造 ─────────────────────────────────────────────

class TestIntentRouteResultBasic:
    def test_minimal_write(self):
        r = IntentRouteResult(
            interaction_type="WRITE",
            confidence=0.9,
            reason="OK",
        )
        assert r.interaction_type == "WRITE"
        assert r.dialogue_mode == "task_collection"
        assert r.confidence == 0.9

    def test_minimal_query(self):
        r = IntentRouteResult(
            interaction_type="QUERY",
            confidence=0.8,
            reason="QA",
            dialogue_mode="knowledge_qa",
            query_intent="KNOWLEDGE_QA",
        )
        assert r.interaction_type == "QUERY"
        assert r.dialogue_mode == "knowledge_qa"
        assert r.query_intent == "KNOWLEDGE_QA"

    def test_confidence_float_coerced(self):
        r = IntentRouteResult(
            interaction_type="WRITE",
            confidence=1,  # int 1 → float 1.0
            reason="coerce",
        )
        assert type(r.confidence) is float
        assert r.confidence == 1.0


# ── 非法字段校验 ────────────────────────────────────────────────────────────

class TestIntentRouteResultValidation:
    def test_invalid_interaction_type_rejected(self):
        with pytest.raises(ValueError):
            IntentRouteResult(
                interaction_type="READ",
                confidence=0.5,
                reason="bad type",
            )

    def test_invalid_dialogue_mode_rejected(self):
        with pytest.raises(ValueError):
            IntentRouteResult(
                interaction_type="WRITE",
                confidence=0.5,
                reason="bad mode",
                dialogue_mode="bogus_mode",
            )

    def test_confidence_bool_rejected(self):
        with pytest.raises(ValueError):
            IntentRouteResult(
                interaction_type="WRITE",
                confidence=True,  # bool 不应视为数字
                reason="bool confidence",
            )

    def test_confidence_out_of_range_rejected(self):
        with pytest.raises(ValueError):
            IntentRouteResult(
                interaction_type="WRITE",
                confidence=1.5,
                reason="over 1",
            )
        with pytest.raises(ValueError):
            IntentRouteResult(
                interaction_type="WRITE",
                confidence=-0.1,
                reason="under 0",
            )

    def test_confidence_nan_inf_rejected(self):
        import math
        with pytest.raises(ValueError):
            IntentRouteResult(
                interaction_type="WRITE",
                confidence=float("nan"),
                reason="nan",
            )
        with pytest.raises(ValueError):
            IntentRouteResult(
                interaction_type="WRITE",
                confidence=float("inf"),
                reason="inf",
            )


# ── 对话模式派生与降级 ─────────────────────────────────────────────────────

class TestIntentRouteResultDerivations:
    def test_task_collection_forces_write_no_intent(self):
        r = IntentRouteResult(
            interaction_type="QUERY",  # 会被改为 WRITE
            confidence=0.9,
            reason="task mode",
            dialogue_mode="task_collection",
            query_intent="KNOWLEDGE_QA",  # 会被清空
        )
        assert r.interaction_type == "WRITE"
        assert r.query_intent is None

    def test_emergency_forces_query_no_intent(self):
        r = IntentRouteResult(
            interaction_type="WRITE",  # 会被改为 QUERY
            confidence=0.9,
            reason="emergency",
            dialogue_mode="emergency_intervention",
            emergency_action="stop",
            query_intent="TASK_STATUS",  # 会被清空
        )
        assert r.interaction_type == "QUERY"
        assert r.query_intent is None
        assert r.emergency_action == "stop"

    def test_knowledge_qa_defaults_intent(self):
        r = IntentRouteResult(
            interaction_type="WRITE",  # 会被改为 QUERY
            confidence=0.9,
            reason="qa",
            dialogue_mode="knowledge_qa",
            query_intent="NOT_A_VALID_INTENT",  # 会被纠正为 KNOWLEDGE_QA
        )
        assert r.interaction_type == "QUERY"
        assert r.query_intent == "KNOWLEDGE_QA"

    def test_emergency_contradiction_safe_downgrade(self):
        # 对话模式非 emergency_intervention，但 action=stop → 矛盾
        r = IntentRouteResult(
            interaction_type="WRITE",
            confidence=0.9,
            reason="contradiction",
            dialogue_mode="knowledge_qa",
            emergency_action="stop",  # 触发矛盾
        )
        # 安全降级到 QA + CLARIFY
        assert r.dialogue_mode == "knowledge_qa"
        assert r.interaction_type == "QUERY"
        assert r.query_intent == "CLARIFICATION"
        assert r.emergency_action is None


# ── 属性派生 ────────────────────────────────────────────────────────────────

class TestIntentRouteResultProperties:
    def test_intent_task(self):
        r = IntentRouteResult(
            interaction_type="WRITE", confidence=0.9, reason="T"
        )
        assert r.intent == "TASK_UPDATE"

    def test_intent_emergency(self):
        r = IntentRouteResult(
            interaction_type="QUERY",
            confidence=0.9,
            reason="E",
            dialogue_mode="emergency_intervention",
            emergency_action="abort",
        )
        assert r.intent == "EMERGENCY_INTERVENTION"

    def test_intent_query(self):
        r = IntentRouteResult(
            interaction_type="QUERY",
            confidence=0.9,
            reason="Q",
            dialogue_mode="knowledge_qa",
            query_intent="DEVICE_STATUS",
        )
        assert r.intent == "DEVICE_STATUS"

    def test_is_query(self):
        qa = IntentRouteResult(
            interaction_type="QUERY", confidence=0.9, reason="", dialogue_mode="knowledge_qa"
        )
        task = IntentRouteResult(
            interaction_type="WRITE", confidence=0.9, reason="", dialogue_mode="task_collection"
        )
        assert qa.is_query is True
        assert task.is_query is False

    def test_should_update_slots(self):
        qa = IntentRouteResult(
            interaction_type="QUERY", confidence=0.9, reason="", dialogue_mode="knowledge_qa"
        )
        task = IntentRouteResult(
            interaction_type="WRITE", confidence=0.9, reason="", dialogue_mode="task_collection"
        )
        assert qa.should_update_slots is False
        assert task.should_update_slots is True


# ── 序列化 ──────────────────────────────────────────────────────────────────

class TestIntentRouteResultToDict:
    def test_to_dict_keys(self):
        r = IntentRouteResult(
            interaction_type="WRITE", confidence=0.9, reason="dict test"
        )
        d = r.to_dict()
        for expected in (
            "interaction_type", "confidence", "reason",
            "query_intent", "dialogue_mode", "source",
            "emergency_action",
        ):
            assert expected in d

    def test_to_dict_without_plan(self):
        r = IntentRouteResult(
            interaction_type="WRITE", confidence=0.9, reason="no plan"
        )
        d = r.to_dict()
        assert "interaction_plan" not in d


# ── 循环依赖消除验证 ─────────────────────────────────────────────────────────

class TestNoCircularImports:
    def test_intent_router_reimports_from_types(self):
        """types 中定义的类可被 intent_router 重新导出，不触发循环。"""
        from src.intent_router import IntentRouteResult as IRR_Router, IntentRoutingError as IRE_Router
        # 必须是同一对象
        assert IRR_Router is IntentRouteResult
        assert IRE_Router is IntentRoutingError

    def test_interaction_plan_can_import(self):
        """interaction_plan 从 types 导入 IntentRouteResult，而不再从 intent_router 延迟导入。"""
        from src.interaction_plan import InteractionPlan, IntentRouteResult as IRR_IP
        assert IRR_IP is IntentRouteResult
        # InteractionPlan.to_intent_route_result() 能正确构造 IntentRouteResult
        plan = InteractionPlan(
            schema_version=1,
            operation="CLARIFY",
            dialogue_mode="knowledge_qa",
            query_intent="CLARIFICATION",
            subject_type="unknown",
            subject_text=None,
            relation="unknown",
            source_policy="none",
            needs_clarification=True,
            clarification_reason="test clarify",
            emergency_action=None,
            confidence=0.5,
            reason_code="TEST_REASON",
        )
        result = plan.to_intent_route_result()
        assert isinstance(result, IntentRouteResult)
        assert result.interaction_type == "QUERY"
        assert "[TEST_REASON]" in result.reason
        assert result.interaction_plan is plan
