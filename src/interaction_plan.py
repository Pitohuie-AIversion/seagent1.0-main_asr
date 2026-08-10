"""
src/interaction_plan.py - 结构化交互计划定义与后端确定性校验

InteractionPlan 是 SEAgent 内部对用户表达意图的底层强类型建模。
它解耦了“句子中出现了什么关键词”，提供可校验、确定性、包含 Subject / Relation / SourcePolicy 的计划表示。
"""

from __future__ import annotations

import math
import logging
from dataclasses import dataclass
from typing import Any, Literal

logger = logging.getLogger(__name__)

OperationType = Literal["READ", "WRITE", "CONTROL", "CLARIFY"]
DialogueModeType = Literal["task_collection", "knowledge_qa", "emergency_intervention"]
SubjectType = Literal[
    "general_concept",
    "system_rule",
    "task",
    "device",
    "device_class",
    "device_family",
    "payload",
    "environment",
    "realtime_state",
    "unknown",
]
RelationType = Literal[
    "definition",
    "describe",
    "list",
    "compare",
    "supports",
    "belongs_to",
    "capabilities",
    "limitations",
    "status",
    "missing_fields",
    "filled_fields",
    "procedure",
    "unknown",
]
SourcePolicyType = Literal[
    "project_kb",
    "session_state",
    "realtime_state",
    "general_domain",
    "hybrid",
    "none",
]

VALID_OPERATIONS = {"READ", "WRITE", "CONTROL", "CLARIFY"}
VALID_DIALOGUE_MODES = {"task_collection", "knowledge_qa", "emergency_intervention"}
VALID_SUBJECT_TYPES = {
    "general_concept",
    "system_rule",
    "task",
    "device",
    "device_class",
    "device_family",
    "payload",
    "environment",
    "realtime_state",
    "unknown",
}
VALID_RELATIONS = {
    "definition",
    "describe",
    "list",
    "compare",
    "supports",
    "belongs_to",
    "capabilities",
    "limitations",
    "status",
    "missing_fields",
    "filled_fields",
    "procedure",
    "unknown",
}
VALID_SOURCE_POLICIES = {
    "project_kb",
    "session_state",
    "realtime_state",
    "general_domain",
    "hybrid",
    "none",
}
VALID_EMERGENCY_ACTIONS = {"stop", "pause", "abort", "cancel"}


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
            "emergency_action": self.emergency_action,
            "confidence": self.confidence,
            "reason_code": self.reason_code,
        }

    def to_intent_route_result(self) -> Any:
        """适配转换至现有的 IntentRouteResult，保障 DialogueManager / Router 接口兼容。"""
        from .intent_router import IntentRouteResult

        interaction_type = "WRITE" if self.operation == "WRITE" else "QUERY"
        return IntentRouteResult(
            interaction_type=interaction_type,
            confidence=self.confidence,
            reason=f"[{self.reason_code}] {self.clarification_reason or 'InteractionPlan 导出的路由结果'}",
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
    """产生标准的安全降级 CLARIFY InteractionPlan。"""
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


def validate_interaction_plan(
    plan_candidate: Any,
    user_message: str = "",
    context: dict | None = None,
) -> InteractionPlan:
    """
    后端确定性校验器。

    校验项包括：
    1. 字段存在性与数据类型（避免 LLM 输出缺失/类型错误）
    2. 数值有限性 (Confidence 无 NaN / Inf / 越界)
    3. 枚举合法性 (operation, dialogue_mode, subject_type, relation, source_policy)
    4. 逻辑一致性：
       - CONTROL 必须有合法的 emergency_action ('stop', 'pause', 'abort', 'cancel')
       - READ 严禁包含可执行紧急控制动作
       - WRITE 必须匹配 task_collection，不可与 knowledge_qa 冲突
       - CLARIFY 必须为 knowledge_qa 且 needs_clarification = True
    5. 置信度校验（< 0.6 安全降级）

    所有不合法的 Plan 统一 Fail-Closed 降级至 CLARIFY。
    """
    if isinstance(plan_candidate, InteractionPlan):
        data = plan_candidate.to_dict()
    elif isinstance(plan_candidate, dict):
        data = dict(plan_candidate)
    else:
        logger.warning("[validate_interaction_plan] 候选 Plan 类型非法: %r", type(plan_candidate))
        return build_clarify_fallback_plan("候选 Plan 非 dict 或 InteractionPlan 结构")

    # 1. 基础 schema 字段检查
    try:
        conf = data.get("confidence")
        if conf is None or isinstance(conf, bool) or not isinstance(conf, (int, float)):
            return build_clarify_fallback_plan("confidence 缺失或类型非法")
        conf_float = float(conf)
        if not math.isfinite(conf_float) or not (0.0 <= conf_float <= 1.0):
            return build_clarify_fallback_plan(f"confidence 数值非法: {conf_float}")
        if conf_float < 0.6:
            return build_clarify_fallback_plan(f"置信度过低({conf_float:.2f})")

        op = data.get("operation")
        if not isinstance(op, str) or op not in VALID_OPERATIONS:
            return build_clarify_fallback_plan(f"operation 非法: {op!r}")

        dm = data.get("dialogue_mode")
        if not isinstance(dm, str) or dm not in VALID_DIALOGUE_MODES:
            return build_clarify_fallback_plan(f"dialogue_mode 非法: {dm!r}")

        st = data.get("subject_type")
        if st is not None and (not isinstance(st, str) or st not in VALID_SUBJECT_TYPES):
            return build_clarify_fallback_plan(f"subject_type 非法: {st!r}")

        rel = data.get("relation")
        if rel is not None and (not isinstance(rel, str) or rel not in VALID_RELATIONS):
            return build_clarify_fallback_plan(f"relation 非法: {rel!r}")

        sp = data.get("source_policy")
        if not isinstance(sp, str) or sp not in VALID_SOURCE_POLICIES:
            return build_clarify_fallback_plan(f"source_policy 非法: {sp!r}")

        act = data.get("emergency_action")
        if act is not None and (not isinstance(act, str) or act not in VALID_EMERGENCY_ACTIONS):
            return build_clarify_fallback_plan(f"emergency_action 非法: {act!r}")

        # 2. 逻辑约束校验
        if op == "CONTROL":
            if dm != "emergency_intervention" or act not in VALID_EMERGENCY_ACTIONS:
                return build_clarify_fallback_plan("CONTROL 模式缺少合法 emergency_action 或 dialogue_mode 矛盾")

        elif op == "READ":
            if dm != "knowledge_qa":
                return build_clarify_fallback_plan(f"READ 模式的 dialogue_mode 必须为 knowledge_qa，实际为 {dm!r}")
            if act is not None:
                return build_clarify_fallback_plan("READ 模式不可包含可执行控制动作")

        elif op == "WRITE":
            if dm != "task_collection":
                return build_clarify_fallback_plan(f"WRITE 模式的 dialogue_mode 必须为 task_collection，实际为 {dm!r}")
            if act is not None:
                return build_clarify_fallback_plan("WRITE 模式不可包含紧急控制动作")

        elif op == "CLARIFY":
            if dm != "knowledge_qa":
                return build_clarify_fallback_plan(f"CLARIFY 模式的 dialogue_mode 必须为 knowledge_qa，实际为 {dm!r}")

        needs_clarify = bool(data.get("needs_clarification", False))
        if op == "CLARIFY":
            needs_clarify = True

        reason_code = str(data.get("reason_code") or "OK").strip()
        clarification_reason = data.get("clarification_reason")
        if clarification_reason is not None and not isinstance(clarification_reason, str):
            clarification_reason = str(clarification_reason)

        query_intent = data.get("query_intent")
        if query_intent is not None and not isinstance(query_intent, str):
            query_intent = str(query_intent)

        subject_text = data.get("subject_text")
        if subject_text is not None and not isinstance(subject_text, str):
            subject_text = str(subject_text)

        schema_ver = int(data.get("schema_version", 1))

        return InteractionPlan(
            schema_version=schema_ver,
            operation=op,  # type: ignore
            dialogue_mode=dm,  # type: ignore
            query_intent=query_intent,
            subject_type=st,  # type: ignore
            subject_text=subject_text,
            relation=rel,  # type: ignore
            source_policy=sp,  # type: ignore
            needs_clarification=needs_clarify,
            clarification_reason=clarification_reason,
            emergency_action=act if op == "CONTROL" else None,
            confidence=conf_float,
            reason_code=reason_code,
        )
    except Exception as exc:
        logger.warning("[validate_interaction_plan] 校验过程抛出异常: %s", exc)
        return build_clarify_fallback_plan(f"校验异常: {exc}")
