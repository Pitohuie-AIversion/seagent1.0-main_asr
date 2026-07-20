"""
src/intent_router.py — 专职结构化意图分类器

采用“确定性规则优先、LLM 低温度结构化分类补充”的策略。
所有输出格式均经严格类型与置信度校验。
只有 TASK_CREATE 和 TASK_UPDATE 可以返回 should_update_slots = True。
"""

from __future__ import annotations

import json
import logging
import math
import re
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

QUESTION_WORDS = [
    "什么", "哪些", "如何", "为什么", "多少", "几", "吗", "是否", "能否", "有没有", "怎么", "？", "?"
]

CREATE_ACTION_PHRASES = [
    "创建一个", "新建一个", "帮我创建", "我要执行一个", "开始规划一个", "创建水下", "新建水下",
    "开展一个", "建立一个", "创建任务", "新建任务", "创建巡检", "新建巡检", "开始创建"
]

EXPLICIT_UPDATE_PHRASES = [
    "把水深改成", "水深改成", "水深设为", "水深为", "水深修改为", "请将支持船换成", "支持船换成",
    "设置开始时间为", "开始时间设为", "帮我选择", "选择观察级", "将坐标修改为", "坐标修改为", "定位在",
    "把开始时间改为", "模式切换为", "工具更换为", "水深深度为", "水深由", "把水深从", "水深从", "改为", "变更为"
]

DEVICE_ENTITIES = ["机器人", "rov", "auv", "潜器", "设备", "型号", "工作级", "观察级", "拖拉机", "深海机器人"]
TOOL_ENTITIES = ["工具", "载荷", "抓手", "机械臂", "传感器", "声呐", "摄像机", "探测仪", "剖面仪", "信标"]


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
    def __init__(self, llm: LLMClient, device_terms: set[str] | None = None):
        self.llm = llm
        self.device_terms = device_terms

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
            return IntentRouteResult(
                intent="CLARIFICATION",
                confidence=1.0,
                reason="输入文本为空",
                source="rule",
                should_update_slots=False,
            )

        # 动态加载知识库设备词汇表（避免写死与数据脱节）
        if self.device_terms is None:
            try:
                from .knowledge_retriever import KnowledgeBase
                self.device_terms = KnowledgeBase().get_all_device_terms()
            except Exception as e:
                logger.warning(f"[IntentRouter] Failed to load dynamic device terms: {e}")
                self.device_terms = set(DEVICE_ENTITIES)

        # ── 1. 确定性规则判断 ──────────────────────────────────────────────────
        rule_result = self._try_rule_routing(msg, task_state, phase, expected_slots)
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

    def _try_rule_routing(
        self, msg: str, task_state: dict, phase: str, expected_slots: list[str] | None
    ) -> IntentRouteResult | None:
        msg_clean = re.sub(r"[。！？,!?. ]+", "", msg.strip()).lower()
        is_q = any(q in msg for q in QUESTION_WORDS)
        device_terms = self.device_terms or set(DEVICE_ENTITIES)
        has_dev = any(e.lower() in msg.lower() for e in (device_terms | set(DEVICE_ENTITIES)) if len(e) >= 2 and not e.isdigit())
        has_tool = any(e in msg.lower() for e in TOOL_ENTITIES)

        negation_cancel = ["不要取消", "别取消", "不能取消", "不放弃", "不终止"]
        negation_confirm = ["不好", "不确认", "不要发布", "先别发布", "不能发布", "不要下发", "不发布", "不是", "不", "别", "不要"]

        # ── 1. 控制语义：取消 / 确认 ──
        explicit_cancel = ["取消当前任务", "放弃当前任务", "终止当前任务", "不要这个任务了", "取消任务", "放弃任务", "终止任务"]
        if any(p in msg for p in explicit_cancel) and not any(nc in msg for nc in negation_cancel):
            return IntentRouteResult(
                intent="TASK_CANCEL",
                confidence=1.0,
                reason="用户明确触发任务取消指令",
                source="rule",
                should_update_slots=False,
            )

        # ── 检查是否为显式修改表达 ──
        update_verbs = ["改成", "改为", "变更为", "修改", "设置", "重置", "换成", "更名", "选择", "选用", "使用", "携带", "装载", "配备", "搭载", "带上", "把", "调整", "为", "由", "从", "到"]
        slot_keywords = ["水深", "深度", "坐标", "经纬度", "支持船", "船", "工具", "抓手", "载荷", "模式", "时间", "井口", "油田", "缆线", "摄像机", "声呐", "开线", "设备", "定位"]

        has_explicit_upd = any(p in msg for p in EXPLICIT_UPDATE_PHRASES)
        has_verb_and_slot = any(v in msg for v in update_verbs) and any(s in msg for s in slot_keywords)
        has_num_slot = bool(re.search(r"(?:水深|深度|坐标|时间)?\s*\d+(?:\.\d+)?\s*(?:米|m)?", msg, re.IGNORECASE)) and any(v in msg for v in update_verbs)
        has_cancel_slot = "取消" in msg and any(s in msg for s in slot_keywords)
        is_modification_request = has_explicit_upd or has_verb_and_slot or has_num_slot or has_cancel_slot
        if phase in ("blocked_hard", "blocked_soft", "confirming", "collecting") and msg_clean in ("取消", "放弃", "不要了", "终止", "退出") and not any(nc in msg for nc in negation_cancel):
            return IntentRouteResult(
                intent="TASK_CANCEL",
                confidence=1.0,
                reason="用户在任务流程中直接回应取消",
                source="rule",
                should_update_slots=False,
            )

        # 检查否定词逻辑（否定词 + 确认动词，或者以“不/别”开头的确认短语）
        has_negation_on_confirm = any(nc in msg for nc in negation_confirm) or bool(re.search(r"(?:不|别|不要|不能|先别).*(?:发布|确认|下发|开始|继续|忽略)", msg))

        # 如果包含参数修改动作，优先走修改/槽位处理，绝不判定为 TASK_CONFIRM
        if not is_modification_request and not has_negation_on_confirm:
            if phase == "blocked_soft":
                blocked_soft_confirms = ["确认继续", "确定继续", "忽略警告", "忽略", "继续", "可以继续"]
                if any(kw in msg for kw in blocked_soft_confirms) or msg_clean in ("确认", "确定", "继续", "忽略", "是的", "好", "可以"):
                    return IntentRouteResult(
                        intent="TASK_CONFIRM",
                        confidence=1.0,
                        reason="用户在软警告阶段选择忽略警告或继续",
                        source="rule",
                        should_update_slots=False,
                    )
            elif phase == "confirming":
                confirming_confirms = ["确认发布", "确认下发", "确定发布", "确认开始", "确定开始"]
                if any(kw in msg for kw in confirming_confirms) or msg_clean in ("确认", "确定", "确认发布", "确定发布"):
                    return IntentRouteResult(
                        intent="TASK_CONFIRM",
                        confidence=1.0,
                        reason="用户在确认阶段明确确认发布任务",
                        source="rule",
                        should_update_slots=False,
                    )
        if not phase in ("confirming", "blocked_soft"):
            if msg_clean in ("确认", "确定", "好的", "是的", "确认发布") and not task_state.get("task_type_key") and not has_negation_on_confirm:
                return IntentRouteResult(
                    intent="CLARIFICATION",
                    confidence=0.9,
                    reason="非确认阶段独立发送确认词，指向不明确",
                    source="rule",
                    should_update_slots=False,
                )

        # ── 2. 状态查询 ──
        if any(kw in msg for kw in ["当前任务有哪些参数", "当前任务填了什么", "当前任务还缺什么", "当前任务进行到哪一步", "当前任务是否可以发布", "任务进度", "当前任务状态", "任务填到哪了", "进度如何"]):
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

        # ── 3. 业务知识与查询 (KNOWLEDGE_QA, TOOL_QUERY, DEVICE_CAPABILITY) ──
        if any(kw in msg for kw in ["管缆巡检任务需要哪些参数", "管缆巡检需要哪些参数", "必填字段", "作业规则", "管缆如何分类", "管道分类", "管缆类型", "任务模板"]):
            return IntentRouteResult(
                intent="KNOWLEDGE_QA",
                confidence=0.95,
                reason="业务知识库查询规则命中",
                source="rule",
                should_update_slots=False,
                query_subtype="knowledge",
            )

        if (is_q or any(kw in msg for kw in ["列表", "使用什么工具", "哪些工具", "可以用什么", "适合使用"])) and has_tool:
            return IntentRouteResult(
                intent="TOOL_QUERY",
                confidence=0.98,
                reason="工具列表/负载询问规则命中",
                source="rule",
                should_update_slots=False,
                query_subtype="available_tools",
            )

        is_device_cap_pattern = bool(re.search(r"(?:最大水深|作业水深|下潜能力|作业能力|能否作业|深水作业|能在\d+米|在\d+米|能否在)", msg)) or "最大水深" in msg
        if (has_dev or is_device_cap_pattern) and (is_q or any(kw in msg for kw in ["能在", "作业", "水深", "下潜", "设备", "参数", "能力", "支持", "最大水深"])) and "当前任务有哪些参数" not in msg and not is_modification_request:
            return IntentRouteResult(
                intent="DEVICE_CAPABILITY",
                confidence=0.98,
                reason="设备能力/型号查询规则命中",
                source="rule",
                should_update_slots=False,
                query_subtype="device_capabilities",
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

        # ── 5. 明确任务创建 (动作门控) ──
        has_create_act = any(p in msg for p in CREATE_ACTION_PHRASES) or (
            any(v in msg for v in ["创建", "新建", "开展", "开始规划"]) and any(e in msg for e in ["任务", "巡检", "插拔", "管缆"])
        )
        if has_create_act and not is_q:
            return IntentRouteResult(
                intent="TASK_CREATE",
                confidence=0.95,
                reason="规则识别到显式任务创建动作指令",
                source="rule",
                should_update_slots=True,
            )

        # ── 6. 明确任务修改 (动作门控 或 已有任务下填报) ──
        if not is_q and (has_explicit_upd or (task_state.get("task_type_key") and (has_verb_and_slot or is_modification_request))):
            return IntentRouteResult(
                intent="TASK_UPDATE",
                confidence=0.95,
                reason="规则识别到具体参数修改或已有任务下的槽位填报",
                source="rule",
                should_update_slots=True,
            )

        # 用户正在回答系统明确询问的 missing expected_slot
        if expected_slots and not is_q:
            if self._matches_expected_slot(msg, expected_slots):
                return IntentRouteResult(
                    intent="TASK_UPDATE",
                    confidence=0.95,
                    reason="用户正在精准回答系统当前提问的缺失槽位",
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

    @staticmethod
    def _matches_expected_slot(msg: str, expected_slots: list[str]) -> bool:
        msg_s = msg.strip()
        for slot in expected_slots:
            if slot == "water_depth":
                if re.search(r"(?:水深|深度)?\s*\d+(?:\.\d+)?\s*(?:米|m)", msg_s, re.IGNORECASE) or re.match(r"^\d+(?:\.\d+)?$", msg_s):
                    return True
            elif slot in ("start_time", "end_time"):
                if any(kw in msg_s for kw in ["现在", "分钟后", "小时后", "明天", "后天"]) or re.search(r"\d{4}[-/.]\d{1,2}[-/.]\d{1,2}", msg_s):
                    return True
            elif slot in ("start_point", "end_point", "oilfield_coordinates"):
                if ("北纬" in msg_s and "东经" in msg_s) or re.search(r"\d+\.\d+.*,\s*\d+\.\d+", msg_s):
                    return True
            elif slot in ("equipment_type", "equipment_name"):
                if any(kw in msg_s.lower() for kw in ["rov", "auv", "重载", "观察级", "工作级", "机器人"]):
                    return True
            elif slot == "support_vessel":
                if any(kw in msg_s for kw in ["船", "海洋石油", "勘探", "号"]):
                    return True
            elif slot == "cable_type":
                if any(kw in msg_s for kw in ["电缆", "光缆", "脐带缆", "管缆"]):
                    return True
            elif slot == "payload":
                if any(kw in msg_s for kw in ["摄像机", "声呐", "抓手", "探头", "工具", "机械臂"]):
                    return True
        return False

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
            return IntentRouteResult(
                intent="CLARIFICATION", confidence=0.0, reason="LLM解析JSON失败", source="llm", should_update_slots=False
            )

        intent_raw = str(parsed.get("intent", "")).strip().upper()
        if not intent_raw or intent_raw not in VALID_INTENTS:
            logger.warning(f"[IntentRouter] Invalid or missing intent string from LLM: '{intent_raw}'")
            return IntentRouteResult(
                intent="CLARIFICATION", confidence=0.0, reason="LLM返回意图非法或缺失", source="llm", should_update_slots=False
            )

        # ── 严格统一的 confidence 校验（无任何 mock 绕过）──
        if "confidence" not in parsed or parsed["confidence"] is None:
            logger.warning("[IntentRouter] Missing confidence field in LLM response")
            return IntentRouteResult(
                intent="CLARIFICATION", confidence=0.0, reason="LLM缺少confidence字段", source="llm", should_update_slots=False
            )

        c_val = parsed["confidence"]
        if isinstance(c_val, bool) or not isinstance(c_val, (int, float)):
            logger.warning(f"[IntentRouter] Invalid type for confidence: {type(c_val)}")
            return IntentRouteResult(
                intent="CLARIFICATION", confidence=0.0, reason="LLM confidence类型非法", source="llm", should_update_slots=False
            )

        c_float = float(c_val)
        if not math.isfinite(c_float) or c_float < 0.0 or c_float > 1.0:
            logger.warning(f"[IntentRouter] Out of bounds or non-finite confidence: {c_float}")
            return IntentRouteResult(
                intent="CLARIFICATION", confidence=0.0, reason="LLM confidence数值越界或非有限值", source="llm", should_update_slots=False
            )

        reason = parsed.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            logger.warning("[IntentRouter] Missing or empty reason field in LLM response")
            return IntentRouteResult(
                intent="CLARIFICATION", confidence=0.0, reason="LLM缺少reason或为空", source="llm", should_update_slots=False
            )

        query_subtype = parsed.get("query_subtype")
        if query_subtype is not None:
            if not isinstance(query_subtype, str):
                logger.warning(f"[IntentRouter] Invalid type for query_subtype: {type(query_subtype)}")
                return IntentRouteResult(
                    intent="CLARIFICATION", confidence=0.0, reason="LLM query_subtype类型非法", source="llm", should_update_slots=False
                )
            query_subtype = query_subtype.strip()
            if not query_subtype:
                query_subtype = None

        if c_float < 0.6:
            return IntentRouteResult(
                intent="CLARIFICATION",
                confidence=c_float,
                reason=f"LLM识别置信度过低({c_float:.2f}): {reason}",
                source="llm",
                should_update_slots=False,
                query_subtype=query_subtype,
            )

        should_update = intent_raw in MUTATING_INTENTS

        return IntentRouteResult(
            intent=intent_raw,
            confidence=c_float,
            reason=reason,
            source="llm",
            should_update_slots=should_update,
            query_subtype=query_subtype,
        )
