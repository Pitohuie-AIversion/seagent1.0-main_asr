"""
src/intent_router.py - 三级结构化交互路由器

IntentRouter 支持三级主线路路由:
1. task_collection (任务收集)
2. knowledge_qa (知识问答)
3. emergency_intervention (紧急干预)
4. uncertain (安全澄清状态)

保持与现有 interaction_type (WRITE / QUERY) 及 query_intent 完全兼容。
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
DialogueMode = Literal[
    "task_collection",
    "knowledge_qa",
    "emergency_intervention",
    "uncertain",
]
EmergencyAction = Literal[
    "stop",
    "pause",
    "cancel",
    "abort",
    "unknown",
]

VALID_INTERACTION_TYPES = {"WRITE", "QUERY"}
VALID_DIALOGUE_MODES = {
    "task_collection",
    "knowledge_qa",
    "emergency_intervention",
    "uncertain",
}
VALID_EMERGENCY_ACTIONS = {
    "stop",
    "pause",
    "cancel",
    "abort",
    "unknown",
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

INTENT_ROUTER_SYSTEM = """\
你负责判断用户本轮输入的意图类型，归类为以下三级线路之一或安全澄清状态：

【三级主线路 (dialogue_mode)】

1. task_collection：
   用户正在提交、补充、选择或修改任务信息。
   包括创建任务、提交任务目标、参数、设备、工具、时间、坐标，以及回答系统正在追问的 expected_slots。

2. knowledge_qa：
   用户正在索取信息、询问设备能力、询问工具载荷、询问环境状态、询问任务规则、询问当前任务进度，或进行普通聊天与系统功能介绍。
   knowledge_qa 属于只读线路，不允许修改任何任务状态或槽位。

   在 dialogue_mode 为 knowledge_qa 时，判断 query_intent 子类型：
   - TASK_STATUS：询问当前任务进度、已有参数或缺失参数。
   - TOOL_QUERY：询问工具、载荷、机械臂、传感器等信息。
   - DEVICE_CAPABILITY：询问设备参数、能力、最大水深或是否适合作业。
   - DEVICE_STATUS：询问设备当前实时状态。
   - ENVIRONMENT_QUERY：询问水深、海况、底质或环境实时状态。
   - KNOWLEDGE_QA：询问任务类型、参数定义、作业规则和业务知识。
   - GENERAL_CHAT：问候、感谢、系统介绍和普通交流。
   - UNKNOWN：明确属于查询，但无法确定具体查询类型。

3. emergency_intervention：
   用户要求立即停止当前任务、暂停任务、终止任务、撤回任务或取消当前操作。

4. uncertain：
   用户输入过于模糊，或置信度较低，无法确定属于上述哪一线路，需要系统向用户寻求针对性澄清。

【输出要求】
只能输出严格 JSON，不得输出其他文字。

task_collection 示例：
{
  "dialogue_mode": "task_collection",
  "confidence": 0.95,
  "reason": "用户提交了准备写入任务状态的水深参数"
}

knowledge_qa 示例：
{
  "dialogue_mode": "knowledge_qa",
  "query_intent": "DEVICE_CAPABILITY",
  "confidence": 0.96,
  "reason": "用户正在询问设备的最大作业水深"
}

emergency_intervention 示例：
{
  "dialogue_mode": "emergency_intervention",
  "emergency_action": "stop",
  "confidence": 0.98,
  "reason": "用户要求立即停止当前任务"
}

uncertain 示例：
{
  "dialogue_mode": "uncertain",
  "query_intent": "CLARIFICATION",
  "confidence": 0.50,
  "reason": "用户意图模糊，需澄清"
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
    dialogue_mode: DialogueMode = "task_collection"
    source: Literal["rule", "llm", "fallback"] = "rule"
    emergency_action: EmergencyAction | None = None

    def __post_init__(self) -> None:
        raw_mode = self.dialogue_mode
        interaction_type = str(self.interaction_type).strip().upper()
        query_intent = (
            str(self.query_intent).strip().upper()
            if self.query_intent
            else None
        )

        if raw_mode == "task_collection" and interaction_type == "QUERY":
            if query_intent == "CLARIFICATION":
                mode = "uncertain"
            else:
                mode = "knowledge_qa"
        else:
            mode = str(raw_mode).strip().lower()

        if mode not in VALID_DIALOGUE_MODES:
            raise ValueError(f"非法 dialogue_mode: {self.dialogue_mode}")
        object.__setattr__(self, "dialogue_mode", mode)

        if self.emergency_action is not None:
            e_act = str(self.emergency_action).strip().lower()
            if e_act not in VALID_EMERGENCY_ACTIONS:
                raise ValueError(f"非法 emergency_action: {self.emergency_action}")
            object.__setattr__(self, "emergency_action", e_act)

        # 映射兼容 interaction_type 与 query_intent
        if mode == "task_collection":
            interaction_type = "WRITE"
            query_intent = None
        elif mode == "knowledge_qa":
            interaction_type = "QUERY"
            query_intent = query_intent or "KNOWLEDGE_QA"
            if query_intent not in VALID_QUERY_INTENTS:
                query_intent = "KNOWLEDGE_QA"
        elif mode == "uncertain":
            interaction_type = "QUERY"
            query_intent = "CLARIFICATION"
        elif mode == "emergency_intervention":
            interaction_type = "QUERY"
            query_intent = None
            if self.emergency_action is None:
                object.__setattr__(self, "emergency_action", "stop")
        else:
            interaction_type = str(self.interaction_type).strip().upper()
            query_intent = (
                str(self.query_intent).strip().upper()
                if self.query_intent
                else None
            )

        if interaction_type not in VALID_INTERACTION_TYPES:
            raise ValueError(f"非法 interaction_type: {interaction_type}")

        if interaction_type == "WRITE" and query_intent is not None:
            raise ValueError("WRITE 路由的 query_intent 必须为 None")
        if (
            interaction_type == "QUERY"
            and query_intent
            and query_intent not in VALID_QUERY_INTENTS
        ):
            raise ValueError("QUERY 路由的 query_intent 必须属于 VALID_QUERY_INTENTS")

        object.__setattr__(self, "interaction_type", interaction_type)
        object.__setattr__(self, "query_intent", query_intent)

    def to_dict(self) -> dict[str, Any]:
        return {
            "dialogue_mode": self.dialogue_mode,
            "interaction_type": self.interaction_type,
            "confidence": self.confidence,
            "reason": self.reason,
            "query_intent": self.query_intent,
            "source": self.source,
            "emergency_action": self.emergency_action,
        }

    @property
    def intent(self) -> str | None:
        if self.dialogue_mode == "emergency_intervention":
            return "EMERGENCY_INTERVENTION"
        if self.dialogue_mode == "task_collection":
            return "TASK_UPDATE"
        return self.query_intent or self.interaction_type

    @property
    def is_query(self) -> bool:
        return (
            self.dialogue_mode in ("knowledge_qa", "uncertain")
            or self.interaction_type == "QUERY"
        )

    @property
    def should_update_slots(self) -> bool:
        return (
            self.dialogue_mode == "task_collection"
            and self.interaction_type == "WRITE"
        )


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

        # 1. 规则优先: 确定性紧急指令识别 (必须在 LLM 路由前执行)
        emergency_res = self._rule_emergency_route(msg)
        if emergency_res is not None:
            return emergency_res

        try:
            return self._call_llm_router(
                user_message=msg,
                conversation_history=conversation_history,
                task_state=task_state,
                phase=phase,
                expected_slots=expected_slots or [],
            )
        except IntentRoutingError as e:
            logger.warning(
                "[IntentRouter] LLM route failed, using rule fallback: %s", e
            )
            return self._rule_fallback_route(
                msg, conversation_history, task_state, phase
            )

    def _rule_emergency_route(self, user_message: str) -> IntentRouteResult | None:
        msg = user_message.strip()

        # 否定句识别: 检查“不要”、“别”、“不”等是否直接修饰紧急动作动词
        negated_emergency = bool(
            re.search(
                r"(?:不要|不|别|无须|不用|禁止|不能|严禁)\s*(?:再)?(?:停止|暂停|终止|撤回|取消|中断)",
                msg,
            )
        )

        # 疑问句与条件句识别: 避免“如何停止当前任务？”或“如果停止任务会怎样？”被误判为执行紧急干预
        is_question_or_condition = (
            bool(re.search(r"[？?]$", msg))
            or any(
                kw in msg
                for kw in (
                    "如何",
                    "怎么",
                    "怎样",
                    "为什么",
                    "能否",
                    "如果",
                    "要是",
                    "假设",
                    "万一",
                    "假使",
                    "若",
                    "会怎样",
                    "会如何",
                    "后果",
                    "怎么办",
                )
            )
        )

        # 明确的紧急干预动作词
        has_emergency_verb = any(
            kw in msg
            for kw in (
                "立即停止",
                "马上停止",
                "停止当前任务",
                "停止任务",
                "暂停当前任务",
                "暂停任务",
                "终止当前任务",
                "终止任务",
                "撤回当前任务",
                "撤回任务",
                "取消当前操作",
                "紧急停止",
                "中断当前任务",
            )
        )

        if negated_emergency or is_question_or_condition:
            return None

        if has_emergency_verb:
            action: EmergencyAction = "stop"
            if "暂停" in msg:
                action = "pause"
            elif "撤回" in msg:
                action = "abort"
            elif "取消" in msg:
                action = "cancel"

            return IntentRouteResult(
                dialogue_mode="emergency_intervention",
                interaction_type="QUERY",
                query_intent=None,
                confidence=0.98,
                reason=f"规则识别: 确定性紧急干预指令({action})",
                source="rule",
                emergency_action=action,
            )

        return None

    def _rule_fallback_route(
        self,
        user_message: str,
        conversation_history: list[dict],
        task_state: dict,
        phase: str,
    ) -> IntentRouteResult:
        msg = user_message.strip()

        # 否定/暂缓控制词 (若包含显式参数修改如'改成500米'，则优先走 task_collection)
        if any(
            kw in msg
            for kw in (
                "不要确认",
                "不确认",
                "暂不确认",
                "不要发布",
                "不发布",
                "暂不发布",
                "先不发布",
                "不要取消",
                "别取消",
                "不取消",
            )
        ):
            if not re.search(r"(?:[0-9]+|改成|设为|设置为|替换为|调整为)", msg):
                return IntentRouteResult(
                    dialogue_mode="uncertain",
                    interaction_type="QUERY",
                    confidence=0.85,
                    reason="规则兜底: 否决/暂缓指令",
                    query_intent="CLARIFICATION",
                    source="fallback",
                )

        # 确认/发布指令
        if any(
            kw in msg
            for kw in (
                "确认发布",
                "确认开始",
                "确认无误",
                "确认并发布",
                "确认",
                "发布任务",
                "发布",
                "开始任务",
                "开始",
                "提交任务",
                "提交",
                "同意",
                "好的",
                "没问题",
                "可以",
                "ok",
            )
        ):
            if phase in ("confirming", "blocked_soft"):
                return IntentRouteResult(
                    dialogue_mode="task_collection",
                    interaction_type="WRITE",
                    confidence=0.95,
                    reason="规则兜底: 确认发布",
                    query_intent=None,
                    source="fallback",
                )

        # 设备能力/查询类关键词优先于数值提交判断
        is_query_sentence = bool(re.search(r"[呢吗？?]$", msg.strip())) or any(
            kw in msg for kw in ("哪些", "如何", "为什么", "是否", "能否", "有没有", "怎么")
        )
        if any(
            kw in msg
            for kw in (
                "水深",
                "深度",
                "作业模式",
                "能力",
                "不能在",
                "支持哪些",
                "适合作业",
                "哪些机器人",
                "哪些设备",
            )
        ):
            if is_query_sentence or not re.search(
                r"(?:设为|改成|设置为|替换为|调整为)", msg
            ):
                return IntentRouteResult(
                    dialogue_mode="knowledge_qa",
                    interaction_type="QUERY",
                    confidence=0.85,
                    reason="规则兜底: 设备能力查询",
                    query_intent="DEVICE_CAPABILITY",
                    source="fallback",
                )
        if any(kw in msg for kw in ("工具", "载荷", "抓手", "传感器", "机械臂", "配备")):
            if is_query_sentence or not re.search(
                r"(?:设为|改成|设置为|替换为|调整为)", msg
            ):
                return IntentRouteResult(
                    dialogue_mode="knowledge_qa",
                    interaction_type="QUERY",
                    confidence=0.85,
                    reason="规则兜底: 工具查询",
                    query_intent="TOOL_QUERY",
                    source="fallback",
                )

        # 包含显式任务创建关键词或参数数值提交
        if any(kw in msg for kw in ("创建", "新建", "巡检", "插入", "拔出")) or bool(
            re.search(
                r"(?:[0-9]+|改成|设为|设置为|替换为|调整为|为\s*[0-9]+|为\s*[A-Za-z0-9_]+)",
                msg,
            )
        ):
            return IntentRouteResult(
                dialogue_mode="task_collection",
                interaction_type="WRITE",
                confidence=0.85,
                reason="规则兜底: 包含任务创建关键词或参数数值",
                query_intent=None,
                source="fallback",
            )

        if any(kw in msg for kw in ("进度", "缺", "缺少", "状态", "已有", "步骤", "进行到")):
            return IntentRouteResult(
                dialogue_mode="knowledge_qa",
                interaction_type="QUERY",
                confidence=0.85,
                reason="规则兜底: 任务状态查询",
                query_intent="TASK_STATUS",
                source="fallback",
            )
        if any(kw in msg for kw in ("海况", "水温", "底质", "海床")):
            return IntentRouteResult(
                dialogue_mode="knowledge_qa",
                interaction_type="QUERY",
                confidence=0.85,
                reason="规则兜底: 环境查询",
                query_intent="ENVIRONMENT_QUERY",
                source="fallback",
            )
        if any(
            kw in msg
            for kw in (
                "你是谁",
                "自我介绍",
                "你叫什么",
                "帮助",
                "说明",
                "谢谢",
                "你好",
                "哈喽",
                "嗨",
            )
        ):
            return IntentRouteResult(
                dialogue_mode="knowledge_qa",
                interaction_type="QUERY",
                confidence=0.85,
                reason="规则兜底: 通用对话",
                query_intent="GENERAL_CHAT",
                source="fallback",
            )

        return IntentRouteResult(
            dialogue_mode="uncertain",
            interaction_type="QUERY",
            confidence=0.5,
            reason="规则兜底: 澄清意图",
            query_intent="CLARIFICATION",
            source="fallback",
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
                    f'【最新用户输入】: "{user_message}"'
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

        _has_slot_candidates = bool(parsed.get("slot_candidates"))
        if _has_slot_candidates or parsed.get("intent") in {"TASK_UPDATE", "TASK_CREATE", "UPDATE", "CREATE"}:
            if "dialogue_mode" not in parsed:
                parsed["dialogue_mode"] = "task_collection"
            if "interaction_type" not in parsed:
                parsed["interaction_type"] = "WRITE"
            if "reason" not in parsed or not parsed["reason"]:
                parsed["reason"] = "包含 slot_candidates"
            if _has_slot_candidates and ("confidence" not in parsed or parsed.get("confidence") is None):
                parsed["confidence"] = 0.9

        raw_mode = parsed.get("dialogue_mode")
        if not raw_mode:
            raw_it = parsed.get("interaction_type") or parsed.get("intent")
            it_str = str(raw_it or "").strip().upper()
            if it_str in ("WRITE", "TASK_CREATE", "TASK_UPDATE", "CREATE", "UPDATE") or _has_slot_candidates:
                raw_mode = "task_collection"
            elif it_str in ("QUERY", "SEARCH", "INFO"):
                raw_mode = "knowledge_qa"
            elif it_str == "EMERGENCY":
                raw_mode = "emergency_intervention"
            else:
                raw_mode = "uncertain"

        dialogue_mode = str(raw_mode).strip().lower()
        if dialogue_mode not in VALID_DIALOGUE_MODES:
            logger.warning("[IntentRouter] 非法 dialogue_mode: %r", dialogue_mode)
            raise IntentRoutingError("LLM 返回 dialogue_mode 非法")

        if "confidence" not in parsed or parsed["confidence"] is None:
            if dialogue_mode == "task_collection" and _has_slot_candidates:
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
            logger.warning(
                "[IntentRouter] confidence 越界或非有限值: %r", confidence_float
            )
            raise IntentRoutingError("LLM confidence 数值越界或非有限值")

        reason = parsed.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            logger.warning("[IntentRouter] LLM response missing reason")
            raise IntentRoutingError("LLM 缺少 reason 字段")

        if confidence_float < 0.6:
            raise IntentRoutingError(
                f"LLM 识别置信度过低({confidence_float:.2f}): {reason.strip()}"
            )

        raw_query_intent = parsed.get("query_intent") or parsed.get("query_subtype")
        query_intent = (
            str(raw_query_intent).strip().upper() if raw_query_intent else None
        )

        if dialogue_mode == "knowledge_qa" and (
            not query_intent or query_intent == "UNKNOWN"
        ):
            msg = user_message.strip()
            if any(
                kw in msg
                for kw in (
                    "当前任务",
                    "进度",
                    "缺",
                    "缺少",
                    "状态",
                    "已有",
                    "步骤",
                    "进行到",
                    "一步",
                )
            ):
                query_intent = "TASK_STATUS"
            elif any(
                kw in msg
                for kw in ("哪些参数", "包含哪些", "包含什么", "模板", "知识", "定义", "规则")
            ):
                query_intent = "KNOWLEDGE_QA"
            elif any(
                kw in msg
                for kw in (
                    "水深",
                    "深度",
                    "作业模式",
                    "能力",
                    "不能在",
                    "支持哪些",
                    "适合作业",
                    "哪些机器人",
                    "哪些设备",
                    "机器人有哪些",
                    "米级",
                )
            ):
                query_intent = "DEVICE_CAPABILITY"
            elif any(
                kw in msg for kw in ("工具", "载荷", "抓手", "传感器", "机械臂", "配备")
            ):
                query_intent = "TOOL_QUERY"
            elif any(kw in msg for kw in ("海况", "水温", "底质", "海床")):
                query_intent = "ENVIRONMENT_QUERY"
            else:
                query_intent = "KNOWLEDGE_QA"

        emergency_action = parsed.get("emergency_action")
        if dialogue_mode == "emergency_intervention" and not emergency_action:
            emergency_action = "stop"

        return IntentRouteResult(
            dialogue_mode=dialogue_mode,
            interaction_type="WRITE" if dialogue_mode == "task_collection" else "QUERY",
            confidence=confidence_float,
            reason=reason.strip(),
            query_intent=query_intent if dialogue_mode == "knowledge_qa" else (
                "CLARIFICATION" if dialogue_mode == "uncertain" else None
            ),
            source="llm",
            emergency_action=emergency_action,
        )
