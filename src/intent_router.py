"""
src/intent_router.py - 结构化交互路由器

IntentRouter 只判断用户本轮输入是 WRITE 还是 QUERY。
QUERY 时保留 query_intent，用于兼容 DialogueManager 现有查询回复链路。
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from typing import Any, Literal

from .llm_client import LLMClient

logger = logging.getLogger(__name__)

InteractionType = Literal["WRITE", "QUERY"]

VALID_INTERACTION_TYPES = {"WRITE", "QUERY"}
VALID_QUERY_INTENTS = {
    "TASK_STATUS",
    "TOOL_QUERY",
    "DEVICE_CAPABILITY",
    "DEVICE_STATUS",
    "ENVIRONMENT_QUERY",
    "KNOWLEDGE_QA",
    "GENERAL_CHAT",
    "UNKNOWN",
}

INTENT_ROUTER_SYSTEM = """\
你负责判断用户本轮输入是在提交任务信息，还是在索取信息。

【第一层类型】

WRITE：
用户正在提交、补充、选择或修改任务信息。
包括提交任务目标、任务参数、设备、工具、时间、坐标，
以及回答系统当前正在追问的 expected_slots。

QUERY：
用户正在索取信息、询问状态、询问能力、询问原因、
询问范围、请求建议，或进行普通聊天。
QUERY 不允许修改任务状态。

【QUERY 子类型】

仅当 interaction_type 为 QUERY 时，判断 query_intent：

- TASK_STATUS：询问当前任务进度、已有参数或缺失参数。
- TOOL_QUERY：询问工具、载荷、机械臂、传感器等信息。
- DEVICE_CAPABILITY：询问设备参数、能力、最大水深或是否适合作业。
- DEVICE_STATUS：询问设备当前实时状态。
- ENVIRONMENT_QUERY：询问水深、海况、底质或环境实时状态。
- KNOWLEDGE_QA：询问任务类型、参数定义、作业规则和业务知识。
- GENERAL_CHAT：问候、感谢、系统介绍和普通交流。
- UNKNOWN：明确属于查询，但无法确定具体查询类型。

【判断原则】

1. 用户提交准备写入任务状态的明确值，判断为 WRITE。
2. 用户回答 expected_slots，判断为 WRITE。
3. 用户询问信息或建议，判断为 QUERY。
4. “水深改成500米，可以吗？”属于 WRITE，因为用户提交了明确修改值。
5. “水深改成多少合适？”属于 QUERY，因为用户没有提交具体值。
6. “如果改成500米会怎样？”属于 QUERY，因为这是条件性询问，不是实际修改。
7. 普通问候和系统身份询问属于 QUERY / GENERAL_CHAT。
8. 不提取字段，不规范化字段值，不判断 CREATE 或 UPDATE。
9. 不判断确认、取消、继续等控制动作。

【输出要求】

只能输出严格 JSON，不得输出其他文字。

WRITE 示例：
{
  "interaction_type": "WRITE",
  "query_intent": null,
  "confidence": 0.97,
  "reason": "用户提交了准备写入任务状态的水深参数"
}

QUERY 示例：
{
  "interaction_type": "QUERY",
  "query_intent": "DEVICE_CAPABILITY",
  "confidence": 0.96,
  "reason": "用户正在询问设备的最大作业水深"
}
"""


class IntentRoutingError(Exception):
    """IntentRouter 协议识别失败。"""


@dataclass(frozen=True)
class IntentRouteResult:
    interaction_type: InteractionType
    confidence: float
    reason: str
    query_intent: str | None = None

    def __post_init__(self) -> None:
        interaction_type = str(self.interaction_type).strip().upper()
        if interaction_type not in VALID_INTERACTION_TYPES:
            raise ValueError(f"非法 interaction_type: {self.interaction_type}")

        query_intent = str(self.query_intent).strip().upper() if self.query_intent else None
        if interaction_type == "WRITE" and query_intent is not None:
            raise ValueError("WRITE 路由的 query_intent 必须为 None")
        if interaction_type == "QUERY" and query_intent not in VALID_QUERY_INTENTS:
            raise ValueError("QUERY 路由的 query_intent 必须属于 VALID_QUERY_INTENTS")

        object.__setattr__(self, "interaction_type", interaction_type)
        object.__setattr__(self, "query_intent", query_intent)

    def to_dict(self) -> dict[str, Any]:
        return {
            "interaction_type": self.interaction_type,
            "confidence": self.confidence,
            "reason": self.reason,
            "query_intent": self.query_intent,
        }


class IntentRouter:
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
        msg = (user_message or "").strip()
        if not msg:
            raise IntentRoutingError("用户输入为空")

        return self._call_llm_router(
            user_message=msg,
            conversation_history=conversation_history,
            task_state=task_state,
            phase=phase,
            expected_slots=expected_slots or [],
        )

    def _call_llm_router(
        self,
        user_message: str,
        conversation_history: list[dict],
        task_state: dict,
        phase: str,
        expected_slots: list[str],
    ) -> IntentRouteResult:
        if self.llm is None:
            raise IntentRoutingError("IntentRouter 缺少 LLMClient")

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
            *conversation_history[-4:],
            {
                "role": "user",
                "content": (
                    f"【当前上下文状态】{json.dumps(context, ensure_ascii=False)}\n"
                    f"【最新用户输入】: \"{user_message}\""
                ),
            },
        ]

        if hasattr(self.llm, "classify_interaction"):
            parsed = self.llm.classify_interaction(messages, max_tokens=260)
        else:
            parsed = self.llm.extract_json(messages, max_tokens=260)

        if not isinstance(parsed, dict):
            logger.warning("[IntentRouter] LLM 未返回合法 JSON object: %r", parsed)
            raise IntentRoutingError("LLM 路由结果不是合法 JSON object")

        interaction_type = str(parsed.get("interaction_type", "")).strip().upper()
        if interaction_type not in VALID_INTERACTION_TYPES:
            logger.warning("[IntentRouter] 非法 interaction_type: %r", interaction_type)
            raise IntentRoutingError("LLM 返回 interaction_type 非法或缺失")

        if "confidence" not in parsed or parsed["confidence"] is None:
            logger.warning("[IntentRouter] LLM response missing confidence")
            raise IntentRoutingError("LLM 缺少 confidence 字段")

        confidence = parsed["confidence"]
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            logger.warning("[IntentRouter] 非法 confidence 类型: %r", type(confidence))
            raise IntentRoutingError("LLM confidence 类型非法")

        confidence_float = float(confidence)
        if not math.isfinite(confidence_float) or not 0.0 <= confidence_float <= 1.0:
            logger.warning("[IntentRouter] confidence 越界或非有限值: %r", confidence_float)
            raise IntentRoutingError("LLM confidence 数值越界或非有限值")

        reason = parsed.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            logger.warning("[IntentRouter] LLM response missing reason")
            raise IntentRoutingError("LLM 缺少 reason 字段")

        if confidence_float < 0.6:
            raise IntentRoutingError(f"LLM 识别置信度过低({confidence_float:.2f}): {reason.strip()}")

        raw_query_intent = parsed.get("query_intent")
        query_intent = str(raw_query_intent).strip().upper() if raw_query_intent else None

        if interaction_type == "WRITE":
            if query_intent is not None:
                raise IntentRoutingError("WRITE 路由的 query_intent 必须为 null")
            return IntentRouteResult(
                interaction_type="WRITE",
                confidence=confidence_float,
                reason=reason.strip(),
                query_intent=None,
            )

        if query_intent not in VALID_QUERY_INTENTS:
            logger.warning("[IntentRouter] 非法 query_intent: %r", query_intent)
            raise IntentRoutingError("QUERY 路由的 query_intent 非法或缺失")

        return IntentRouteResult(
            interaction_type="QUERY",
            confidence=confidence_float,
            reason=reason.strip(),
            query_intent=query_intent,
        )
