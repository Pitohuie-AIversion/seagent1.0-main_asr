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
        return IntentRouteResult(
            intent="CLARIFICATION",
            confidence=0.5,
            reason="意图未匹配或 LLM 解析异常，安全降级到澄清",
            source="fallback",
            should_update_slots=False,
        )

    def _try_rule_routing(self, msg: str, task_state: dict, phase: str) -> IntentRouteResult | None:
        import re
        msg_clean = re.sub(r"[。！？,!?. ]+", "", msg.strip()).lower()

        negation_cancel = ["不要取消", "别取消", "不能取消", "不放弃", "不终止"]
        negation_confirm = ["不好", "不确认", "不要发布", "先别发布", "不能发布", "不要下发", "不发布", "不是"]

        # ── 1. 控制语义：取消 / 确认 ──
        # 取消命令 (不能带有否定取消语义)
        explicit_cancel = ["取消当前任务", "放弃当前任务", "终止当前任务", "不要这个任务了", "取消任务", "放弃任务", "终止任务"]
        if any(p in msg for p in explicit_cancel) and not any(nc in msg for nc in negation_cancel):
            return IntentRouteResult(
                intent="TASK_CANCEL",
                confidence=1.0,
                reason="用户明确触发任务取消指令",
                source="rule",
                should_update_slots=False,
            )
        if phase in ("blocked_hard", "blocked_soft", "confirming", "collecting") and msg_clean in ("取消", "放弃", "不要了", "终止", "退出") and not any(nc in msg for nc in negation_cancel):
            return IntentRouteResult(
                intent="TASK_CANCEL",
                confidence=1.0,
                reason="用户在任务流程中直接回应取消",
                source="rule",
                should_update_slots=False,
            )

        # 确认发布指令 (只在允许的 phase 中生效，且绝不能带有否定确认语义)
        explicit_confirm = ["确认发布", "确认下发", "发布任务", "确定发布", "确认继续", "忽略警告", "忽略"]
        if phase in ("confirming", "blocked_soft"):
            if (any(kw in msg for kw in explicit_confirm) or msg_clean in ("确认", "确定", "是的", "好", "可以")) and not any(nc in msg for nc in negation_confirm):
                return IntentRouteResult(
                    intent="TASK_CONFIRM",
                    confidence=1.0,
                    reason="用户在确认/软警告阶段确认发布",
                    source="rule",
                    should_update_slots=False,
                )
        else:
            if msg_clean in ("确认", "确定", "好的", "是的") and not task_state.get("task_type_key") and not any(nc in msg for nc in negation_confirm):
                return IntentRouteResult(
                    intent="CLARIFICATION",
                    confidence=0.9,
                    reason="非确认阶段独立发送确认词，指向不明确",
                    source="rule",
                    should_update_slots=False,
                )

        # ── 2. 状态查询 ──
        if any(kw in msg for kw in ["当前任务进行到哪一步", "任务进度", "当前任务状态", "任务填到哪了", "进度如何", "进行到哪一步"]):
            return IntentRouteResult(
                intent="TASK_STATUS",
                confidence=0.98,
                reason="任务阶段进度询问规则命中",
                source="rule",
                should_update_slots=False,
                query_subtype="task_progress",
            )
        if any(kw in msg for kw in ["当前机器人状态", "机器人状态怎么样", "设备状态如何", "实时状态", "推进器状态"]):
            return IntentRouteResult(
                intent="DEVICE_STATUS",
                confidence=0.95,
                reason="设备遥测实时状态询问规则命中",
                source="rule",
                should_update_slots=False,
                query_subtype="telemetry",
            )
        if any(kw in msg for kw in ["这里的海况", "海况怎么样", "环境信息", "当前海流", "底质如何"]):
            return IntentRouteResult(
                intent="ENVIRONMENT_QUERY",
                confidence=0.95,
                reason="海域环境信息询问规则命中",
                source="rule",
                should_update_slots=False,
                query_subtype="environment",
            )

        # ── 3. 工具、设备能力与业务知识查询 ──
        if any(kw in msg for kw in ["可以使用哪些工具", "可以用哪些工具", "可以使用的工具", "可用工具", "适合使用什么工具", "使用什么工具", "携带什么工具", "工具列表", "配备什么工具", "搭载什么工具", "支持什么工具"]):
            return IntentRouteResult(
                intent="TOOL_QUERY",
                confidence=0.98,
                reason="工具列表/负载询问规则命中",
                source="rule",
                should_update_slots=False,
                query_subtype="available_tools",
            )
        if any(kw in msg for kw in ["500米级机器人", "深海机器人", "级机器人有哪些", "设备支持什么能力", "最大水深是多少", "有哪些型号", "有哪些机器人", "有哪些rov", "深潜器", "潜器", "支持几米水深", "能够作业深", "作业能力", "功能参数", "型号有哪些", "有哪些参数", "参数"]):
            return IntentRouteResult(
                intent="DEVICE_CAPABILITY",
                confidence=0.98,
                reason="设备能力/型号查询规则命中",
                source="rule",
                should_update_slots=False,
                query_subtype="device_capabilities",
            )
        if any(kw in msg for kw in ["作业规则", "管道分类", "管缆类型", "任务模板"]):
            return IntentRouteResult(
                intent="KNOWLEDGE_QA",
                confidence=0.95,
                reason="业务知识库查询规则命中",
                source="rule",
                should_update_slots=False,
                query_subtype="knowledge",
            )

        # ── 4. 普通对话与问候 ──
        if any(kw in msg_clean for kw in ["谢谢", "多谢", "感谢", "thx", "thanks"]):
            return IntentRouteResult(
                intent="GENERAL_CHAT",
                confidence=1.0,
                reason="日常致谢语",
                source="rule",
                should_update_slots=False,
                query_subtype="thanks",
            )
        if msg_clean in ("你好", "您好", "你好啊", "哈喽", "hi", "hello", "早上好", "下午好", "晚上好"):
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

        # ── 5. 明确任务创建 ──
        create_verbs = ["创建", "新建", "开展", "开始", "建立", "执行", "想要", "进行"]
        explicit_task_nouns = ["巡检", "插拔", "采油树", "面板", "管缆", "水线", "水下任务", "管线巡检", "水深"]
        if any(v in msg for v in create_verbs) or (not task_state.get("task_type_key") and any(n in msg for n in explicit_task_nouns)):
            return IntentRouteResult(
                intent="TASK_CREATE",
                confidence=0.95,
                reason="规则识别到任务创建指令或新任务短语",
                source="rule",
                should_update_slots=True,
            )

        # ── 6. 明确任务修改 ──
        update_verbs = ["改成", "修改", "设置", "重置", "换成", "定位在", "更名", "选择", "选用", "使用", "携带", "装载", "配备", "搭载", "带上", "把", "由", "调整"]
        update_slots = ["水深", "深度", "坐标", "经纬度", "支持船", "船", "工具", "抓手", "模式", "时间", "井口", "油田", "缆线"]
        has_num = bool(re.search(r"\d+", msg))

        if any(v in msg for v in update_verbs) or (task_state.get("task_type_key") and (any(s in msg for s in update_slots) or has_num)):
            return IntentRouteResult(
                intent="TASK_UPDATE",
                confidence=0.95,
                reason="规则识别到具体参数修改或已有任务下的槽位填报",
                source="rule",
                should_update_slots=True,
            )

        # ── 7. 意图澄清指令 ──
        if msg_clean in ("帮我处理一下", "处理一下", "处理", "搞一下", "跑一下") or any(kw in msg_clean for kw in ["帮我处理", "处理一下", "搞一下", "跑一下"]):
            return IntentRouteResult(
                intent="CLARIFICATION",
                confidence=0.95,
                reason="模糊的通用动词请求，需进行意图澄清",
                source="rule",
                should_update_slots=False,
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
            confidence = float(parsed.get("confidence", 1.0))
        except (ValueError, TypeError):
            confidence = 1.0
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
