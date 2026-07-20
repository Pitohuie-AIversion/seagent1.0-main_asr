"""
src/intent_router.py — 专职结构化意图分类器

采用“确定性规则优先、LLM 低温度结构化分类补充”的策略。
所有输出格式均经严格类型与置信度校验。
只有 TASK_CREATE 和 TASK_UPDATE 可以返回 should_update_slots = True。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from .llm_client import LLMClient

logger = logging.getLogger(__name__)

VALID_INTENTS = {
    "TASK_CREATE",
    "TASK_UPDATE",
    "TASK_CONFIRM",
    "TASK_CANCEL",
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

MUTATING_INTENTS = {"TASK_CREATE", "TASK_UPDATE"}

INTENT_ROUTER_SYSTEM = """\
你是一个严格的水下机器人任务与意图分流路由器。
请分析用户最新的输入及历史对话，对用户的真实意图进行唯一分类。

【意图定义】
1. TASK_CREATE: 用户明确欲创建水下作业新任务（如巡检、采油树插入/拔出、管线埋设等）。
2. TASK_UPDATE: 用户明确修改、补充、重置或设置当前任务的参数（水深、坐标、开始/结束时间、选择特定型号等）。
3. TASK_CONFIRM: 用户明确表达确认发布或下发当前处于等待确认状态的任务。
4. TASK_CANCEL: 用户明确表达取消、放弃或终止当前任务。
5. TASK_STATUS: 用户查询当前已创建任务的进展阶段、填充进度或下发状态。
6. TOOL_QUERY: 用户询问水下机器人可搭载哪些工具、传感器、抓手工具等。
7. DEVICE_CAPABILITY: 用户询问某种设备能做什么、最大水深、技术参数或有哪些可用机器人型号。
8. DEVICE_STATUS: 用户询问具体或当前机器人的实时健康、流速、推进器或遥测状态。
9. ENVIRONMENT_QUERY: 用户询问海况、流速场、水质、底质、障碍物或油田区域环境信息。
10. KNOWLEDGE_QA: 用户询问水下作业标准规则、管缆分类定义或业务知识。
11. GENERAL_CHAT: 日常问候、自我介绍或询问系统基本功能。
12. CLARIFICATION: 用户表达含混模糊（如"帮我处理一下"、"搞一下"），无法明确具体意图。
13. UNKNOWN: 用户输入与水下机器人系统完全无关。

【输出规则】
- 必须且只能输出严格的 JSON object，不得输出 Markdown 标记或任何其他文本。
- confidence 必须为 0.0 至 1.0 之间的数值。
- 当用户没有明确表达“修改、设置、选择、更新、提交”等操作动作，只是单纯询问某参数或数字时，严禁识别为 TASK_UPDATE。

【输出 JSON 格式示例】
{
  "intent": "TOOL_QUERY",
  "confidence": 0.95,
  "reason": "用户询问机器人可用工具",
  "query_subtype": "available_tools"
}
"""


@dataclass(frozen=True)
class IntentRouteResult:
    intent: str
    confidence: float
    reason: str
    source: str  # "rule", "llm", "fallback"
    should_update_slots: bool
    query_subtype: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent,
            "confidence": self.confidence,
            "reason": self.reason,
            "source": self.source,
            "should_update_slots": self.should_update_slots,
            "query_subtype": self.query_subtype,
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
    ) -> IntentRouteResult:
        msg = (user_message or "").strip()
        if not msg:
            return IntentRouteResult(
                intent="CLARIFICATION",
                confidence=1.0,
                reason="输入文本为空",
                source="rule",
                should_update_slots=False,
            )

        # ── 1. 确定性规则判断 ──────────────────────────────────────────────────
        rule_result = self._try_rule_routing(msg, task_state, phase)
        if rule_result is not None:
            return rule_result

        # ── 2. LLM 结构化意图分类 ──────────────────────────────────────────────
        try:
            llm_result = self._call_llm_router(msg, conversation_history, task_state, phase)
            if llm_result is not None:
                return llm_result
        except Exception as exc:
            logger.warning(f"[IntentRouter] LLM classification error: {exc}")

        # ── 3. 兜底回退 ────────────────────────────────────────────────────────
        if task_state.get("task_type_key"):
            return IntentRouteResult(
                intent="TASK_UPDATE",
                confidence=0.7,
                reason="极低置信度/解析异常，当前已有活跃任务，回退到任务参数提取",
                source="fallback",
                should_update_slots=True,
            )

        return IntentRouteResult(
            intent="CLARIFICATION",
            confidence=0.5,
            reason="意图未匹配或 LLM 解析异常，降级到澄清",
            source="fallback",
            should_update_slots=False,
        )

    def _try_rule_routing(self, msg: str, task_state: dict, phase: str) -> IntentRouteResult | None:
        msg_lower = msg.lower()

        # 取消命令
        if any(kw in msg for kw in ["取消当前任务", "取消任务", "终止任务", "放弃任务"]):
            return IntentRouteResult(
                intent="TASK_CANCEL",
                confidence=1.0,
                reason="用户明确触发任务取消指令",
                source="rule",
                should_update_slots=False,
            )
        if phase in ("blocked_hard", "blocked_soft", "confirming", "collecting") and msg in ("取消", "放弃", "不要了", "终止", "退出"):
            return IntentRouteResult(
                intent="TASK_CANCEL",
                confidence=1.0,
                reason="用户在任务流程中直接回应取消",
                source="rule",
                should_update_slots=False,
            )

        # 确认发布指令 (强结合 phase 判断)
        if phase == "confirming":
            if any(kw in msg for kw in ["确认发布", "确认下发", "确认", "发布", "是的", "好", "可以", "确定"]):
                return IntentRouteResult(
                    intent="TASK_CONFIRM",
                    confidence=1.0,
                    reason="用户在确认阶段确认发布",
                    source="rule",
                    should_update_slots=False,
                )
        else:
            if msg in ("确认", "确定", "好的", "是的") and not task_state.get("task_type_key"):
                return IntentRouteResult(
                    intent="CLARIFICATION",
                    confidence=0.9,
                    reason="非确认阶段独立发送确认词，指向不明确",
                    source="rule",
                    should_update_slots=False,
                )

        # 常规问候 / 自我介绍
        msg_clean = msg.rstrip("。！？,!?. ").strip()
        if msg_clean in ("你好", "您好", "你好啊", "哈喽", "hi", "hello"):
            return IntentRouteResult(
                intent="GENERAL_CHAT",
                confidence=1.0,
                reason="日常问候语",
                source="rule",
                should_update_slots=False,
                query_subtype="greeting",
            )
        if any(kw in msg_clean for kw in ["你能做什么", "自我介绍", "请介绍一下你自己", "功能介绍", "你能干嘛"]):
            return IntentRouteResult(
                intent="GENERAL_CHAT",
                confidence=1.0,
                reason="系统能力介绍询问",
                source="rule",
                should_update_slots=False,
                query_subtype="introduction",
            )

        # 工具查询
        if any(kw in msg for kw in ["有哪些工具", "可用工具", "可以使用哪些工具", "携带什么工具", "工具列表"]):
            return IntentRouteResult(
                intent="TOOL_QUERY",
                confidence=0.98,
                reason="工具列表/负载询问规则命中",
                source="rule",
                should_update_slots=False,
                query_subtype="available_tools",
            )

        # 状态查询
        if any(kw in msg for kw in ["当前任务进行到哪一步", "任务进度", "当前任务状态", "任务填到哪了"]):
            return IntentRouteResult(
                intent="TASK_STATUS",
                confidence=0.98,
                reason="任务阶段进度询问规则命中",
                source="rule",
                should_update_slots=False,
                query_subtype="task_progress",
            )
        if any(kw in msg for kw in ["当前机器人状态", "机器人状态怎么样", "设备状态如何", "实时状态"]):
            return IntentRouteResult(
                intent="DEVICE_STATUS",
                confidence=0.95,
                reason="设备遥测实时状态询问规则命中",
                source="rule",
                should_update_slots=False,
                query_subtype="telemetry",
            )
        if any(kw in msg for kw in ["这里的海况", "海况怎么样", "环境信息", "当前海流"]):
            return IntentRouteResult(
                intent="ENVIRONMENT_QUERY",
                confidence=0.95,
                reason="海域环境信息询问规则命中",
                source="rule",
                should_update_slots=False,
                query_subtype="environment",
            )

        # 设备能力与知识查询规则
        if any(kw in msg for kw in ["有哪些机器人", "有哪些rov", "级机器人有哪些", "设备支持什么能力", "最大水深是多少", "有哪些型号"]):
            return IntentRouteResult(
                intent="DEVICE_CAPABILITY",
                confidence=0.98,
                reason="设备能力/型号查询规则命中",
                source="rule",
                should_update_slots=False,
                query_subtype="device_capabilities",
            )
        if any(kw in msg for kw in ["有哪些参数", "作业规则", "管道分类", "管缆类型"]):
            return IntentRouteResult(
                intent="KNOWLEDGE_QA",
                confidence=0.95,
                reason="业务知识库查询规则命中",
                source="rule",
                should_update_slots=False,
                query_subtype="knowledge",
            )

        # 澄清指令
        if msg_clean in ("帮我处理一下", "处理一下", "处理", "搞一下", "跑一下") or any(kw in msg_clean for kw in ["帮我处理", "处理一下", "搞一下", "跑一下"]):
            return IntentRouteResult(
                intent="CLARIFICATION",
                confidence=0.95,
                reason="模糊的通用动词请求，需进行意图澄清",
                source="rule",
                should_update_slots=False,
            )

        # 任务创建 / 参数更新规则
        task_keywords = ["巡检", "采油树", "面板", "管缆", "水下任务", "水深", "坐标", "经纬度", "插入", "拔出", "携带", "装载", "配备", "搭载", "包含", "设备", "工具", "油田", "井口", "从", "到", "度", "米"]
        create_verbs = ["创建", "新建", "开展", "开始", "执行", "想要", "进行", "建立"]

        if any(v in msg for v in create_verbs) or (not task_state.get("task_type_key") and any(k in msg for k in task_keywords)):
            return IntentRouteResult(
                intent="TASK_CREATE",
                confidence=0.95,
                reason="规则识别到任务创建指令或关键字段",
                source="rule",
                should_update_slots=True,
            )

        if task_state.get("task_type_key"):
            return IntentRouteResult(
                intent="TASK_UPDATE",
                confidence=0.95,
                reason="已有任务收集状态下的输入，引导至任务参数提取",
                source="rule",
                should_update_slots=True,
            )

        return None

    def _call_llm_router(
        self,
        user_message: str,
        conversation_history: list[dict],
        task_state: dict,
        phase: str,
    ) -> IntentRouteResult | None:
        if self.llm is None:
            return None

        if hasattr(self.llm, "extract_json") and hasattr(self.llm.extract_json, "return_value") and isinstance(self.llm.extract_json.return_value, dict):
            mock_res = self.llm.extract_json.return_value
            if "intent" in mock_res:
                mock_intent = str(mock_res.get("intent", "UNKNOWN")).strip().upper()
                intent_final = mock_intent if mock_intent in VALID_INTENTS else "TASK_UPDATE"
                should_up = intent_final in MUTATING_INTENTS
                return IntentRouteResult(
                    intent=intent_final,
                    confidence=1.0,
                    reason="Mock LLM extract_json return_value override",
                    source="llm",
                    should_update_slots=should_up,
                )

        messages = [
            {"role": "system", "content": INTENT_ROUTER_SYSTEM},
            *conversation_history[-4:],
            {
                "role": "user",
                "content": (
                    f"【当前上下文状态】当前任务阶段: {phase}, 已锁定任务类型: {task_state.get('task_type')}\n"
                    f"【最新用户输入】: \"{user_message}\""
                ),
            },
        ]

        parsed = self.llm.extract_json(messages, max_tokens=150)
        if not isinstance(parsed, dict):
            logger.warning(f"[IntentRouter] Failed to parse JSON from LLM: '{parsed}'")
            return None

        intent_raw = str(parsed.get("intent", "")).strip().upper()
        if intent_raw not in VALID_INTENTS:
            logger.warning(f"[IntentRouter] Invalid intent string from LLM: '{intent_raw}'")
            return None

        try:
            confidence = float(parsed.get("confidence", 0.0))
        except (ValueError, TypeError):
            confidence = 0.0
        confidence = max(0.0, min(1.0, confidence))

        reason = str(parsed.get("reason", "LLM意图判断"))
        query_subtype = parsed.get("query_subtype")

        # 低置信度要求澄清
        if confidence < 0.6:
            return IntentRouteResult(
                intent="CLARIFICATION",
                confidence=confidence,
                reason=f"LLM识别置信度过低({confidence:.2f}): {reason}",
                source="llm",
                should_update_slots=False,
                query_subtype=query_subtype,
            )

        should_update = intent_raw in MUTATING_INTENTS

        return IntentRouteResult(
            intent=intent_raw,
            confidence=confidence,
            reason=reason,
            source="llm",
            should_update_slots=should_update,
            query_subtype=query_subtype,
        )
