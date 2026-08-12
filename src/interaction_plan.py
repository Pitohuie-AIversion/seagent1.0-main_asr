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

        confidence = data.get("confidence")
        if (
            confidence is None
            or isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
        ):
            raise ValueError("confidence 缺失或类型非法")
        confidence = float(confidence)
        if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
            raise ValueError(f"confidence 数值非法: {confidence!r}")
        if confidence < MIN_PLAN_CONFIDENCE:
            return build_clarify_fallback_plan(
                f"置信度过低({confidence:.2f})",
                reason_code="LOW_CONFIDENCE_CLARIFY",
                confidence=confidence,
            )

        operation = data.get("operation")
        if not isinstance(operation, str) or operation not in VALID_OPERATIONS:
            raise ValueError(f"operation 非法: {operation!r}")

        dialogue_mode = data.get("dialogue_mode")
        if (
            not isinstance(dialogue_mode, str)
            or dialogue_mode not in VALID_DIALOGUE_MODES
        ):
            raise ValueError(f"dialogue_mode 非法: {dialogue_mode!r}")

        subject_type = _enum_or_default(
            data, "subject_type", VALID_SUBJECT_TYPES, "unknown"
        )
        relation = _enum_or_default(data, "relation", VALID_RELATIONS, "unknown")
        source_policy = _enum_or_default(
            data, "source_policy", VALID_SOURCE_POLICIES, "none"
        )
        query_intent = _optional_string(data, "query_intent")
        subject_text = _optional_string(data, "subject_text")
        clarification_reason = _optional_string(data, "clarification_reason")

        needs_clarification = data.get("needs_clarification", False)
        if not isinstance(needs_clarification, bool):
            raise TypeError("needs_clarification 必须为布尔值")

        emergency_action = data.get("emergency_action")
        if emergency_action is not None and (
            not isinstance(emergency_action, str)
            or emergency_action not in VALID_EMERGENCY_ACTIONS
        ):
            raise ValueError(f"emergency_action 非法: {emergency_action!r}")

        pending_action = data.get("pending_action")
        if pending_action is not None and (
            not isinstance(pending_action, str)
            or pending_action not in VALID_PENDING_ACTIONS
        ):
            raise ValueError(f"pending_action 非法: {pending_action!r}")
        if pending_action is not None and operation != "WRITE":
            raise ValueError("pending_action 只能用于 WRITE")

        warning_action = data.get("warning_action")
        if warning_action is not None and (
            not isinstance(warning_action, str)
            or warning_action not in VALID_WARNING_ACTIONS
        ):
            raise ValueError(f"warning_action 非法: {warning_action!r}")
        if warning_action is not None and operation != "WRITE":
            raise ValueError("warning_action 只能用于 WRITE")

        if operation == "CONTROL":
            if (
                dialogue_mode != "emergency_intervention"
                or emergency_action not in VALID_EMERGENCY_ACTIONS
            ):
                raise ValueError("CONTROL 缺少合法 emergency_action 或模式矛盾")
        elif operation == "WRITE":
            if dialogue_mode != "task_collection" or emergency_action is not None:
                raise ValueError("WRITE 必须使用 task_collection 且不可携带控制动作")
        elif operation == "READ":
            if dialogue_mode != "knowledge_qa" or emergency_action is not None:
                raise ValueError("READ 必须使用 knowledge_qa 且不可携带控制动作")
        else:
            if dialogue_mode != "knowledge_qa" or emergency_action is not None:
                raise ValueError("CLARIFY 必须为无副作用 knowledge_qa")
            needs_clarification = True

        reason_code = data.get("reason_code", "OK")
        if not isinstance(reason_code, str):
            raise TypeError("reason_code 必须为字符串")

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
            emergency_action=emergency_action if operation == "CONTROL" else None,
            confidence=confidence,
            reason_code=reason_code.strip() or "OK",
            pending_action=pending_action,
        )
    except (TypeError, ValueError) as exc:
        logger.warning("[validate_interaction_plan] 协议校验失败: %s", exc)
        return build_clarify_fallback_plan(str(exc))
