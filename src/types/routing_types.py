"""
路由与交互共享类型模块

提取 IntentRouteResult 及共享常量，消除 intent_router 与 interaction_plan 之间的循环依赖。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Literal, TYPE_CHECKING

if TYPE_CHECKING:
    from ..interaction_plan import InteractionPlan

InteractionType = Literal["WRITE", "QUERY"]
DialogueMode = Literal[
    "task_collection",
    "knowledge_qa",
    "emergency_intervention",
]

VALID_INTERACTION_TYPES = {"WRITE", "QUERY"}
VALID_DIALOGUE_MODES: frozenset[str] = frozenset({
    "task_collection",
    "knowledge_qa",
    "emergency_intervention",
})
VALID_EMERGENCY_ACTIONS = {"stop", "pause", "abort", "cancel"}
VALID_QUERY_INTENTS = frozenset({
    "TASK_STATUS",
    "TOOL_QUERY",
    "DEVICE_CAPABILITY",
    "DEVICE_STATUS",
    "ENVIRONMENT_QUERY",
    "KNOWLEDGE_QA",
    "GENERAL_CHAT",
    "CLARIFICATION",
    "UNKNOWN",
})


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
    interaction_plan: "InteractionPlan | None" = None

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
        object.__setattr__(self, "dialogue_mode", dialogue_mode)  # type: ignore[arg-type]
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
