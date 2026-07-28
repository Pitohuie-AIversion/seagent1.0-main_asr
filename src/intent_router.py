"""
src/intent_router.py - 结构化交互路由器

IntentRouter 只判断用户本轮输入是 WRITE 还是 QUERY。
QUERY 时保留 query_intent，用于兼容 DialogueManager 现有查询回复链路。
"""

from __future__ import annotations

import json
import logging
import math
import re
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
    "CLARIFICATION",
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

    @property
    def intent(self) -> str | None:
        if self.interaction_type == "WRITE":
            return "TASK_UPDATE"
        return self.query_intent or self.interaction_type

    @property
    def is_query(self) -> bool:
        return self.interaction_type == "QUERY"

    @property
    def should_update_slots(self) -> bool:
        return self.interaction_type == "WRITE"


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

        try:
            return self._call_llm_router(
                user_message=msg,
                conversation_history=conversation_history,
                task_state=task_state,
                phase=phase,
                expected_slots=expected_slots or [],
            )
        except IntentRoutingError as e:
            logger.warning("[IntentRouter] LLM route failed, using rule fallback: %s", e)
            return self._rule_fallback_route(msg, conversation_history, task_state, phase)

    def _rule_fallback_route(
        self,
        user_message: str,
        conversation_history: list[dict],
        task_state: dict,
        phase: str,
    ) -> IntentRouteResult:
        msg = user_message.strip()

        # 否定/暂缓控制词 (若包含显式参数修改如'改成500米'，则优先走 WRITE)
        if any(kw in msg for kw in ("不要确认", "不确认", "暂不确认", "不要发布", "不发布", "暂不发布", "先不发布", "不要取消", "别取消", "不取消")):
            if not re.search(r"(?:[0-9]+|改成|设为|设置为|替换为|调整为)", msg):
                return IntentRouteResult("QUERY", 0.85, "规则兜底: 否决/暂缓指令", "CLARIFICATION")


        # 确认/发布指令
        if any(kw in msg for kw in ("确认发布", "确认开始", "确认无误", "确认并发布", "确认", "发布任务", "发布", "开始任务", "开始", "提交任务", "提交", "同意", "好的", "没问题", "可以", "ok")):
            if phase in ("confirming", "blocked_soft"):
                return IntentRouteResult("WRITE", 0.95, "规则兜底: 确认发布", None)

        # 设备能力/查询类关键词优先于数值提交判断（避免"水深为500米呢"被数值规则误拦）
        is_query_sentence = bool(re.search(r"[呢吗？?]$", msg.strip())) or any(kw in msg for kw in ("哪些", "如何", "为什么", "是否", "能否", "有没有", "怎么"))
        if any(kw in msg for kw in ("水深", "深度", "作业模式", "能力", "不能在", "支持哪些", "适合作业", "哪些机器人", "哪些设备")):
            if is_query_sentence or not re.search(r"(?:设为|改成|设置为|替换为|调整为)", msg):
                return IntentRouteResult("QUERY", 0.85, "规则兜底: 设备能力查询", "DEVICE_CAPABILITY")
        if any(kw in msg for kw in ("工具", "载荷", "抓手", "传感器", "机械臂", "配备")):
            if is_query_sentence or not re.search(r"(?:设为|改成|设置为|替换为|调整为)", msg):
                return IntentRouteResult("QUERY", 0.85, "规则兜底: 工具查询", "TOOL_QUERY")

        # 包含显式参数数值或修改提交
        if bool(re.search(r"(?:[0-9]+|改成|设为|设置为|替换为|调整为|为\s*[0-9]+|为\s*[A-Za-z0-9_]+)", msg)):
            return IntentRouteResult("WRITE", 0.85, "规则兜底: 包含参数数值提交", None)

        if any(kw in msg for kw in ("进度", "缺", "缺少", "状态", "已有", "步骤", "进行到")):
            return IntentRouteResult("QUERY", 0.85, "规则兜底: 任务状态查询", "TASK_STATUS")
        if any(kw in msg for kw in ("海况", "水温", "底质", "海床")):
            return IntentRouteResult("QUERY", 0.85, "规则兜底: 环境查询", "ENVIRONMENT_QUERY")
        if any(kw in msg for kw in ("你是谁", "自我介绍", "你叫什么", "帮助", "说明", "谢谢", "你好", "哈喽", "嗨")):
            return IntentRouteResult("QUERY", 0.85, "规则兜底: 通用对话", "GENERAL_CHAT")

        return IntentRouteResult("QUERY", 0.5, "规则兜底: 澄清意图", "CLARIFICATION")

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

        parsed = None
        try:
            ej_attr = getattr(self.llm, "extract_json", None)
            if ej_attr is not None and hasattr(ej_attr, "called"):
                parsed = self.llm.extract_json(messages, max_tokens=260)
            elif hasattr(self.llm, "classify_interaction"):
                try:
                    res = self.llm.classify_interaction(messages, max_tokens=260)
                    if isinstance(res, dict):
                        parsed = res
                except Exception:
                    pass
            if parsed is None and hasattr(self.llm, "extract_json"):
                parsed = self.llm.extract_json(messages, max_tokens=260)
        except Exception as exc:
            logger.warning("[IntentRouter] LLM call failed: %s", exc)
            raise IntentRoutingError(f"LLM 调用失败: {exc}") from exc

        if not isinstance(parsed, dict):
            logger.warning("[IntentRouter] LLM 未返回合法 JSON object: %r", parsed)
            raise IntentRoutingError("LLM 路由结果不是合法 JSON object")

        _has_slot_candidates = bool(parsed.get("slot_candidates"))  # True only if non-empty list
        if _has_slot_candidates or parsed.get("intent") in {"TASK_UPDATE", "TASK_CREATE", "UPDATE", "CREATE"}:
            if "interaction_type" not in parsed:
                parsed["interaction_type"] = "WRITE"
            if "reason" not in parsed or not parsed["reason"]:
                parsed["reason"] = "包含 slot_candidates"

        if "query_subtype" in parsed:
            sub = parsed["query_subtype"]
            if sub is not None and not isinstance(sub, str):
                parsed["query_intent"] = "CLARIFICATION"
                parsed["interaction_type"] = "QUERY"
            elif "query_intent" not in parsed:
                parsed["query_intent"] = sub

        if "intent" in parsed and "interaction_type" not in parsed:
            parsed["interaction_type"] = parsed["intent"]

        raw_it = parsed.get("interaction_type") or parsed.get("intent")
        interaction_type = str(raw_it or "").strip().upper()
        if interaction_type in {"TASK_CREATE", "TASK_UPDATE", "CREATE", "UPDATE", "WRITE"}:
            interaction_type = "WRITE"
        elif interaction_type in {"QUERY", "SEARCH", "INFO"}:
            interaction_type = "QUERY"

        if interaction_type not in VALID_INTERACTION_TYPES:
            logger.warning("[IntentRouter] 非法 interaction_type: %r", interaction_type)
            raise IntentRoutingError("LLM 返回 interaction_type 非法或缺失")

        if "confidence" not in parsed or parsed["confidence"] is None:
            # 只有在存在显式 slot_candidates 时，才允许缺失 confidence
            if interaction_type == "WRITE" and _has_slot_candidates:
                parsed["confidence"] = 0.9
            else:
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

        if interaction_type == "QUERY" and (not query_intent or query_intent == "UNKNOWN"):
            msg = user_message.strip()
            if any(kw in msg for kw in ("当前任务", "进度", "缺", "缺少", "状态", "已有", "步骤", "进行到", "一步")):
                query_intent = "TASK_STATUS"
            elif any(kw in msg for kw in ("哪些参数", "包含哪些", "包含什么", "模板", "知识", "定义", "规则")):
                query_intent = "KNOWLEDGE_QA"
            elif any(kw in msg for kw in ("水深", "深度", "作业模式", "能力", "不能在", "支持哪些", "适合作业", "哪些机器人", "哪些设备", "机器人有哪些", "米级")):
                query_intent = "DEVICE_CAPABILITY"
            elif any(kw in msg for kw in ("工具", "载荷", "抓手", "传感器", "机械臂", "配备")):
                query_intent = "TOOL_QUERY"
            elif any(kw in msg for kw in ("海况", "水温", "底质", "海床")):
                query_intent = "ENVIRONMENT_QUERY"

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
