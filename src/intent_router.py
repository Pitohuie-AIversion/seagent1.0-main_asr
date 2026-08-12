"""模型驱动的交互路由协议适配器。

IntentRouter 只负责把统一会话上下文交给模型，并验证模型返回的
InteractionPlan。它不读取用户原句重新猜测 READ/WRITE/CONTROL/CLARIFY。
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from typing import Any, Literal

from .interaction_plan import (
    InteractionPlan,
    build_clarify_fallback_plan,
    validate_interaction_plan,
)
from .llm_client import LLMClient
from .model_profile import ModelRole, _is_unsupported_role_keyword_error

logger = logging.getLogger(__name__)

InteractionType = Literal["WRITE", "QUERY"]
DialogueMode = Literal[
    "task_collection",
    "knowledge_qa",
    "emergency_intervention",
]

VALID_INTERACTION_TYPES = {"WRITE", "QUERY"}
VALID_DIALOGUE_MODES = {
    "task_collection",
    "knowledge_qa",
    "emergency_intervention",
}
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
VALID_EMERGENCY_ACTIONS = {"stop", "pause", "abort", "cancel"}


INTENT_ROUTER_SYSTEM = """\
你是 SEAgent 的 TurnPlanner。请结合最新输入、历史消息和当前任务状态，判断本轮
用户真正想做的事情，并只输出一个 schema_version=1 的 JSON object。

operation 只能是：
- READ：只回答问题或聊天，不修改任务；
- WRITE：创建任务、补充或修改任务信息；即使同时要求解释，也选择 WRITE；
- CONTROL：请求停止、暂停、终止或取消运行控制；
- CLARIFY：上下文仍不足以安全判断。

不要依赖固定句式。需要理解省略、指代、对上一轮建议的接受、任务中途闲聊，以及
同一句中的问答和修改。已有任务或 expected_slots 不代表本轮一定要写入；反过来，
自然表达没有出现字段名也不代表不能写入。

必须输出全部字段：
{
  "schema_version": 1,
  "operation": "READ|WRITE|CONTROL|CLARIFY",
  "dialogue_mode": "task_collection|knowledge_qa|emergency_intervention",
  "query_intent": "TASK_STATUS|TOOL_QUERY|DEVICE_CAPABILITY|DEVICE_STATUS|ENVIRONMENT_QUERY|KNOWLEDGE_QA|GENERAL_CHAT|CLARIFICATION|null",
  "subject_type": "general_concept|system_rule|task|device|device_class|device_family|payload|environment|realtime_state|unknown",
  "subject_text": "string|null",
  "relation": "definition|describe|list|compare|supports|belongs_to|capabilities|limitations|status|missing_fields|filled_fields|procedure|unknown",
  "source_policy": "project_kb|session_state|realtime_state|general_domain|hybrid|none",
  "needs_clarification": false,
  "clarification_reason": "string|null",
  "emergency_action": "stop|pause|abort|cancel|null",
  "confidence": 0.0,
  "reason_code": "short_machine_readable_code"
}

一致性要求：READ/CLARIFY 使用 knowledge_qa；WRITE 使用 task_collection；CONTROL
使用 emergency_intervention 且必须给出 emergency_action。只能输出 JSON。
"""


class IntentRoutingError(Exception):
    """模型调用或 InteractionPlan 协议识别失败。"""


@dataclass(frozen=True)
class IntentRouteResult:
    """DialogueManager 兼容路由结果。

    READ、CLARIFY 和 CONTROL 仍映射为旧接口的 QUERY；真正的 operation 保留在
    ``interaction_plan``，CONTROL 通过 emergency_intervention 单独执行。
    """

    interaction_type: InteractionType
    confidence: float
    reason: str
    query_intent: str | None = None
    dialogue_mode: DialogueMode = "task_collection"
    source: str = "interaction_plan"
    emergency_action: str | None = None
    interaction_plan: InteractionPlan | None = None

    def __post_init__(self) -> None:
        interaction_type = str(self.interaction_type).strip().upper()
        dialogue_mode = str(self.dialogue_mode).strip().lower()
        query_intent = (
            str(self.query_intent).strip().upper() if self.query_intent else None
        )

        if interaction_type not in VALID_INTERACTION_TYPES:
            raise ValueError(f"非法 interaction_type: {self.interaction_type}")
        if dialogue_mode not in VALID_DIALOGUE_MODES:
            raise ValueError(f"非法 dialogue_mode: {self.dialogue_mode}")
        if (
            isinstance(self.confidence, bool)
            or not isinstance(self.confidence, (int, float))
            or not math.isfinite(float(self.confidence))
            or not 0.0 <= float(self.confidence) <= 1.0
        ):
            raise ValueError(f"非法 confidence: {self.confidence!r}")

        action = self.emergency_action
        invalid_action = action is not None and action not in VALID_EMERGENCY_ACTIONS
        contradictory_action = (
            action in VALID_EMERGENCY_ACTIONS
            and dialogue_mode != "emergency_intervention"
        )
        missing_action = (
            dialogue_mode == "emergency_intervention"
            and action not in VALID_EMERGENCY_ACTIONS
        )
        if invalid_action or contradictory_action or missing_action:
            # 兼容调用方可能直接构造旧路由结果；协议矛盾只允许安全降级，不能
            # 因异常绕过 DialogueManager 的只读路径。
            dialogue_mode = "knowledge_qa"
            interaction_type = "QUERY"
            query_intent = "CLARIFICATION"
            action = None
            object.__setattr__(self, "emergency_action", None)

        if dialogue_mode == "emergency_intervention":
            interaction_type = "QUERY"
            query_intent = None
        elif dialogue_mode == "task_collection":
            interaction_type = "WRITE"
            query_intent = None
        else:
            interaction_type = "QUERY"
            if query_intent not in VALID_QUERY_INTENTS:
                query_intent = "KNOWLEDGE_QA"

        object.__setattr__(self, "interaction_type", interaction_type)
        object.__setattr__(self, "dialogue_mode", dialogue_mode)
        object.__setattr__(self, "query_intent", query_intent)
        object.__setattr__(self, "confidence", float(self.confidence))

    def to_dict(self) -> dict[str, Any]:
        result = {
            "interaction_type": self.interaction_type,
            "confidence": self.confidence,
            "reason": self.reason,
            "query_intent": self.query_intent,
            "dialogue_mode": self.dialogue_mode,
            "source": self.source,
            "emergency_action": self.emergency_action,
        }
        if self.interaction_plan is not None:
            result["interaction_plan"] = self.interaction_plan.to_dict()
        return result

    @property
    def intent(self) -> str | None:
        if self.dialogue_mode == "task_collection":
            return "TASK_UPDATE"
        if self.dialogue_mode == "emergency_intervention":
            return "EMERGENCY_INTERVENTION"
        return self.query_intent or self.interaction_type

    @property
    def is_query(self) -> bool:
        return self.dialogue_mode != "task_collection"

    @property
    def should_update_slots(self) -> bool:
        return self.dialogue_mode == "task_collection"


class IntentRouter:
    """每轮调用一次模型规划；任何失败均返回无副作用澄清计划。"""

    def __init__(self, llm: LLMClient):
        self.llm = llm

    def route(
        self,
        user_message: str,
        conversation_history: list[dict],
        task_state: dict,
        phase: str = "collecting",
        expected_slots: list[str] | None = None,
    ) -> IntentRouteResult:
        message = (user_message or "").strip()
        if not message:
            raise IntentRoutingError("用户输入为空")

        try:
            return self._call_llm_router(
                user_message=message,
                conversation_history=conversation_history,
                task_state=task_state,
                phase=phase,
                expected_slots=expected_slots or [],
            )
        except IntentRoutingError as exc:
            logger.warning(
                "[IntentRouter] 模型规划失败，安全降级为澄清: %s",
                exc,
            )
            return build_clarify_fallback_plan(
                reason=f"暂时无法可靠理解本轮意图，请用户澄清: {exc}",
                reason_code="LLM_ROUTE_FAILURE_CLARIFY",
                confidence=0.5,
            ).to_intent_route_result()

    def _call_llm_router(
        self,
        user_message: str,
        conversation_history: list[dict],
        task_state: dict,
        phase: str,
        expected_slots: list[str],
    ) -> IntentRouteResult:
        classify = getattr(self.llm, "classify_interaction", None)
        if not callable(classify):
            raise IntentRoutingError("LLMClient 缺少 classify_interaction 协议")

        context = {
            "phase": phase,
            "has_task": bool(task_state.get("task_type_key")),
            "task_type": task_state.get("task_type"),
            "task_type_key": task_state.get("task_type_key"),
            "expected_slots": expected_slots,
            "filled_slots": {
                key: value
                for key, value in (task_state or {}).items()
                if value is not None
                and key
                not in {
                    "raw_oilfield_name",
                    "oilfield_match_evidence",
                    "oilfield_match_candidates",
                    "pending_oilfield_candidates",
                }
            },
        }
        messages = [
            {"role": "system", "content": INTENT_ROUTER_SYSTEM},
            *conversation_history[-8:],
            {
                "role": "user",
                "content": (
                    f"【当前上下文状态】{json.dumps(context, ensure_ascii=False)}\n"
                    f"【最新用户输入】{user_message}"
                ),
            },
        ]

        try:
            try:
                candidate = classify(
                    messages,
                    max_tokens=480,
                    role=ModelRole.ROUTER,
                )
            except TypeError as exc:
                if not _is_unsupported_role_keyword_error(exc):
                    raise
                candidate = classify(messages, max_tokens=480)
        except Exception as exc:
            raise IntentRoutingError(f"LLM 调用失败: {exc}") from exc

        if not isinstance(candidate, dict):
            raise IntentRoutingError("LLM 路由结果不是合法 JSON object")

        plan = validate_interaction_plan(candidate)
        if plan.reason_code == "VALIDATION_FALLBACK_CLARIFY":
            logger.warning(
                "[IntentRouter] InteractionPlan 协议非法: %s",
                plan.clarification_reason,
            )
        return plan.to_intent_route_result()
