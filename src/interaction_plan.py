"""结构化交互计划及确定性协议校验。

InteractionPlan 表达模型对本轮输入的语义判断。此模块只验证协议和安全边界，
不读取用户原句重新猜测 READ/WRITE/CLARIFY。
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any, Literal

logger = logging.getLogger(__name__)

OperationType = Literal["READ", "WRITE", "CONTROL", "CLARIFY"]
DialogueModeType = Literal[
    "task_collection",
    "knowledge_qa",
    "emergency_intervention",
]
SubjectType = Literal[
    "general_concept", "system_rule", "task", "device", "device_class",
    "device_family", "payload", "environment", "realtime_state", "unknown",
]
RelationType = Literal[
    "definition", "describe", "list", "compare", "supports", "belongs_to",
    "capabilities", "limitations", "status", "missing_fields", "filled_fields",
    "procedure", "recommend", "unknown",
]
SourcePolicyType = Literal[
    "project_kb", "session_state", "realtime_state", "general_domain",
    "hybrid", "none",
]

VALID_OPERATIONS = {"READ", "WRITE", "CONTROL", "CLARIFY"}
VALID_DIALOGUE_MODES = {
    "task_collection", "knowledge_qa", "emergency_intervention",
}
VALID_SUBJECT_TYPES = {
    "general_concept", "system_rule", "task", "device", "device_class",
    "device_family", "payload", "environment", "realtime_state", "unknown",
}
VALID_RELATIONS = {
    "definition", "describe", "list", "compare", "supports", "belongs_to",
    "capabilities", "limitations", "status", "missing_fields", "filled_fields",
    "procedure", "recommend", "unknown",
}
VALID_SOURCE_POLICIES = {
    "project_kb", "session_state", "realtime_state", "general_domain",
    "hybrid", "none",
}
VALID_EMERGENCY_ACTIONS = {"stop", "pause", "abort", "cancel"}
VALID_PENDING_ACTIONS = {"confirm", "reject"}
VALID_WARNING_ACTIONS = {"acknowledge"}
MIN_PLAN_CONFIDENCE = 0.6
VALID_QUERY_INTENTS = {
    "TASK_STATUS",
    "TOOL_QUERY",
    "DEVICE_CAPABILITY",
    "DEVICE_STATUS",
    "ENVIRONMENT_QUERY",
    "KNOWLEDGE_QA",
    "GENERAL_CHAT",
    "CLARIFICATION",
    "UNKNOWN",
}


@dataclass(frozen=True)
class InteractionPlan:
    schema_version: int
    operation: OperationType
    dialogue_mode: DialogueModeType
    query_intent: str | None
    subject_type: SubjectType | None
    subject_text: str | None
    relation: RelationType | None
    source_policy: SourcePolicyType
    needs_clarification: bool
    clarification_reason: str | None
    emergency_action: str | None
    confidence: float
    reason_code: str
    warning_action: str | None = None
    pending_action: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "operation": self.operation,
            "dialogue_mode": self.dialogue_mode,
            "query_intent": self.query_intent,
            "subject_type": self.subject_type,
            "subject_text": self.subject_text,
            "relation": self.relation,
            "source_policy": self.source_policy,
            "needs_clarification": self.needs_clarification,
            "clarification_reason": self.clarification_reason,
            "warning_action": self.warning_action,
            "emergency_action": self.emergency_action,
            "pending_action": self.pending_action,
            "confidence": self.confidence,
            "reason_code": self.reason_code,
        }

    def to_intent_route_result(self) -> Any:
        """转换为 DialogueManager 仍在使用的兼容路由结果。"""
        from .intent_router import IntentRouteResult

        interaction_type = "WRITE" if self.operation == "WRITE" else "QUERY"
        return IntentRouteResult(
            interaction_type=interaction_type,
            confidence=self.confidence,
            reason=(
                f"[{self.reason_code}] "
                f"{self.clarification_reason or 'InteractionPlan 导出的路由结果'}"
            ),
            query_intent=self.query_intent,
            dialogue_mode=self.dialogue_mode,
            source="interaction_plan",
            emergency_action=self.emergency_action,
            interaction_plan=self,
        )


def build_clarify_fallback_plan(
    reason: str,
    reason_code: str = "VALIDATION_FALLBACK_CLARIFY",
    confidence: float = 0.5,
) -> InteractionPlan:
    """构造无副作用的标准澄清计划。"""
    return InteractionPlan(
        schema_version=1,
        operation="CLARIFY",
        dialogue_mode="knowledge_qa",
        query_intent="CLARIFICATION",
        subject_type="unknown",
        subject_text=None,
        relation="unknown",
        source_policy="none",
        needs_clarification=True,
        clarification_reason=reason,
        emergency_action=None,
        confidence=confidence,
        reason_code=reason_code,
    )


def _optional_string(data: dict[str, Any], key: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{key} 必须为字符串或 null")
    return value


def _enum_or_default(
    data: dict[str, Any],
    key: str,
    allowed: set[str],
    default: str,
) -> str:
    value = data.get(key)
    if value is None:
        return default
    if not isinstance(value, str) or value not in allowed:
        raise ValueError(f"{key} 非法: {value!r}")
    return value


def validate_interaction_plan(
    plan_candidate: Any,
    user_message: str = "",
    context: dict | None = None,
) -> InteractionPlan:
    """验证模型计划的结构与安全一致性。

    ``user_message`` 和 ``context`` 仅为兼容旧调用签名保留。校验器不得依据其中的
    业务词汇改变模型给出的 operation。
    """
    del user_message, context

    if isinstance(plan_candidate, InteractionPlan):
        data = plan_candidate.to_dict()
    elif isinstance(plan_candidate, dict):
        data = dict(plan_candidate)
    else:
        logger.warning(
            "[validate_interaction_plan] 候选 Plan 类型非法: %r",
            type(plan_candidate),
        )
        return build_clarify_fallback_plan("候选 Plan 必须为 JSON object")

    try:
        schema_version = data.get("schema_version", 1)
        if (
            isinstance(schema_version, bool)
            or not isinstance(schema_version, int)
            or schema_version != 1
        ):
            raise ValueError(f"schema_version 非法: {schema_version!r}")

        operation_raw = data.get("operation")
        operation = (
            operation_raw.strip().upper()
            if isinstance(operation_raw, str)
            else None
        )
        if operation not in VALID_OPERATIONS:
            raise ValueError(f"operation 非法: {operation_raw!r}")

        confidence_raw = data.get("confidence", 1.0)
        if (
            isinstance(confidence_raw, bool)
            or not isinstance(confidence_raw, (int, float))
            or not math.isfinite(float(confidence_raw))
            or not 0.0 <= float(confidence_raw) <= 1.0
        ):
            confidence = 0.0
        else:
            confidence = float(confidence_raw)
        if operation in {"WRITE", "CONTROL"} and confidence < MIN_PLAN_CONFIDENCE:
            return build_clarify_fallback_plan(
                f"涉及状态变更的计划置信度过低({confidence:.2f})",
                reason_code="LOW_CONFIDENCE_CLARIFY",
                confidence=confidence,
            )

        # operation 是唯一语义权威。模式和澄清状态由执行器推导，避免模型在
        # 重复字段上产生轻微不一致后让安全的 READ 整轮失败。
        dialogue_mode = {
            "READ": "knowledge_qa",
            "WRITE": "task_collection",
            "CONTROL": "emergency_intervention",
            "CLARIFY": "knowledge_qa",
        }[operation]
        needs_clarification = operation == "CLARIFY"

        subject_type_raw = data.get("subject_type")
        subject_type = (
            subject_type_raw
            if isinstance(subject_type_raw, str)
            and subject_type_raw in VALID_SUBJECT_TYPES
            else "unknown"
        )
        relation_raw = data.get("relation")
        relation = (
            relation_raw
            if isinstance(relation_raw, str) and relation_raw in VALID_RELATIONS
            else "unknown"
        )
        source_policy_raw = data.get("source_policy")
        source_policy = (
            source_policy_raw
            if isinstance(source_policy_raw, str)
            and source_policy_raw in VALID_SOURCE_POLICIES
            else "none"
        )

        subject_text_raw = data.get("subject_text")
        subject_text = subject_text_raw if isinstance(subject_text_raw, str) else None
        clarification_raw = data.get("clarification_reason")
        clarification_reason = (
            clarification_raw if isinstance(clarification_raw, str) else None
        )

        query_raw = data.get("query_intent")
        if operation == "READ":
            if query_raw is None:
                query_intent = "GENERAL_CHAT"
            elif isinstance(query_raw, str) and query_raw in VALID_QUERY_INTENTS:
                query_intent = query_raw
            else:
                query_intent = "KNOWLEDGE_QA"
        elif operation == "CLARIFY":
            query_intent = "CLARIFICATION"
        else:
            query_intent = None

        emergency_raw = data.get("emergency_action")
        if operation == "CONTROL":
            if emergency_raw not in VALID_EMERGENCY_ACTIONS:
                raise ValueError("CONTROL 缺少合法 emergency_action")
            emergency_action = emergency_raw
        else:
            emergency_action = None

        pending_raw = data.get("pending_action")
        pending_action = (
            pending_raw
            if operation == "WRITE" and pending_raw in VALID_PENDING_ACTIONS
            else None
        )
        warning_raw = data.get("warning_action")
        warning_action = (
            warning_raw
            if operation == "WRITE" and warning_raw in VALID_WARNING_ACTIONS
            else None
        )

        reason_raw = data.get("reason_code", "OK")
        reason_code = (
            reason_raw.strip()
            if isinstance(reason_raw, str) and reason_raw.strip()
            else "OK"
        )

        return InteractionPlan(
            schema_version=schema_version,
            operation=operation,
            warning_action=warning_action,
            dialogue_mode=dialogue_mode,
            query_intent=query_intent,
            subject_type=subject_type,
            subject_text=subject_text,
            relation=relation,
            source_policy=source_policy,
            needs_clarification=needs_clarification,
            clarification_reason=clarification_reason,
            emergency_action=emergency_action,
            confidence=confidence,
            reason_code=reason_code,
            pending_action=pending_action,
        )
    except (TypeError, ValueError) as exc:
        logger.warning("[validate_interaction_plan] 协议校验失败: %s", exc)
        return build_clarify_fallback_plan(str(exc))
