"""
src/intent_router.py — 专职结构化交互路由器

第一层判断用户本轮交互性质（QUERY / WRITE / CONTROL / CHAT / AMBIGUOUS），
程序再校验该判断是否允许进入槽位写入或查询线路。
保留 legacy intent 字段，兼容 DialogueManager 现有分流。
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
QUERY_INTENTS = {
    "TASK_STATUS",
    "TOOL_QUERY",
    "DEVICE_CAPABILITY",
    "DEVICE_STATUS",
    "ENVIRONMENT_QUERY",
    "KNOWLEDGE_QA",
}
CONTROL_INTENTS = {"TASK_CONFIRM", "TASK_CANCEL"}
CHAT_INTENTS = {"GENERAL_CHAT"}

VALID_INTERACTION_TYPES = {"QUERY", "WRITE", "CONTROL", "CHAT", "AMBIGUOUS"}
VALID_WRITE_ACTIONS = {"CREATE", "UPDATE", "NONE"}
VALID_CONTROL_ACTIONS = {"CONFIRM", "CANCEL", "CONTINUE", "NONE"}
VALID_QUERY_INTENTS = QUERY_INTENTS | {None}

INTENT_ROUTER_SYSTEM = """\
你负责判断用户本轮输入的交互性质。你只做路由判断，不提取字段，不生成任务。

【第一层交互类型】
- WRITE：用户希望创建任务、填写参数、修改参数、选择设备、设置时间，或回答系统正在追问的任务字段。
- QUERY：用户希望了解信息，例如询问有哪些设备、最大水深、任务需要哪些参数、当前任务还缺什么。
- CONTROL：用户确认、取消、继续或忽略当前任务流程。
- CHAT：问候、感谢、询问系统功能等普通对话。
- AMBIGUOUS：根据当前输入和上下文仍无法可靠判断。

【第二层字段】
- write_action: CREATE / UPDATE / NONE
- control_action: CONFIRM / CANCEL / CONTINUE / NONE
- query_intent: TASK_STATUS / TOOL_QUERY / DEVICE_CAPABILITY / DEVICE_STATUS / ENVIRONMENT_QUERY / KNOWLEDGE_QA / null

【判别原则】
1. 先判断用户是在索取信息还是提交信息：
   - 索取信息、询问范围、询问原因、询问能力、询问状态 → QUERY。
   - 提交任务目标、参数值、选择项、修正值，或回答当前 expected_slots → WRITE。
2. 当前上下文存在 expected_slots 时，如果用户输入像字段答案而不是问题，优先视为 WRITE / UPDATE。
3. 不要因为文本中出现业务实体名就直接判为 QUERY；实体名既可能出现在问题里，也可能出现在字段值里。
4. 不要提取或规范化字段值；只判断本轮输入是否允许进入后续 Extractor。
5. CREATE 与 UPDATE 的区别：
   - 用户提出新的作业目标或任务规划意图 → CREATE。
   - 用户补充、选择、修改当前任务参数 → UPDATE。
【对比例子】
- “管缆类型有哪些？” → QUERY / KNOWLEDGE_QA
- “管缆类型海底油气管道” → WRITE / UPDATE
- “把管缆类型改成海底油气管道” → WRITE / UPDATE
- “我想做管缆巡检，管缆类型海底油气管道” → WRITE / CREATE
- “海底油气管道” 且 expected_slots 包含 cable_type → WRITE / UPDATE

【输出规则】
- 必须且只能输出严格的 JSON object，不得输出 Markdown 标记或任何其他文本。
- confidence 必须为 0.0 至 1.0 之间的数值。
- QUERY 不允许修改槽位；WRITE 只表示允许进入后续 Extractor 校验，不代表直接写入。

【输出 JSON 格式示例】
{
  "interaction_type": "WRITE",
  "write_action": "CREATE",
  "control_action": "NONE",
  "query_intent": null,
  "confidence": 0.97,
  "reason": "用户表达创建管缆巡检任务，并提供多个任务参数",
  "write_evidence": ["用户提交任务目标的原文片段", "用户提交字段值的原文片段"],
  "query_evidence": []
}
"""

QUESTION_WORDS = [
    "什么", "哪些", "如何", "为什么", "多少", "几", "吗", "是否", "能否", "有没有", "怎么", "？", "?"
]

DEVICE_ENTITIES = ["机器人", "rov", "auv", "潜器", "设备", "型号", "工作级", "观察级", "拖拉机", "深海机器人"]
TOOL_ENTITIES = ["工具", "载荷", "抓手", "机械臂", "传感器", "声呐", "摄像机", "探测仪", "剖面仪", "信标"]


def _normalize_device_text(value: Any) -> str:
    """统一设备词比较格式，忽略大小写、空白和常见标点。"""
    return re.sub(r"[。！？,!?.\s]+", "", str(value or "")).lower()


@dataclass(frozen=True)
class IntentRouteResult:
    intent: str
    confidence: float
    reason: str
    source: str  # "rule", "llm", "fallback"
    should_update_slots: bool
    query_subtype: str | None = None
    interaction_type: str | None = None
    write_action: str = "NONE"
    control_action: str = "NONE"
    query_intent: str | None = None
    write_evidence: list[str] | None = None
    query_evidence: list[str] | None = None

    def __post_init__(self) -> None:
        interaction_type = self.interaction_type or self._derive_interaction_type(self.intent)
        query_intent = self.query_intent
        write_action = self.write_action
        control_action = self.control_action

        if interaction_type == "WRITE":
            if write_action not in ("CREATE", "UPDATE"):
                write_action = "CREATE" if self.intent == "TASK_CREATE" else "UPDATE"
            query_intent = None
            control_action = "NONE"
        elif interaction_type == "QUERY":
            query_intent = query_intent or self.intent
            write_action = "NONE"
            control_action = "NONE"
        elif interaction_type == "CONTROL":
            if control_action not in ("CONFIRM", "CANCEL", "CONTINUE"):
                control_action = "CONFIRM" if self.intent == "TASK_CONFIRM" else "CANCEL"
            write_action = "NONE"
            query_intent = None
        elif interaction_type == "CHAT":
            write_action = "NONE"
            control_action = "NONE"
            query_intent = None
        else:
            interaction_type = "AMBIGUOUS"
            write_action = "NONE"
            control_action = "NONE"
            query_intent = None

        object.__setattr__(self, "interaction_type", interaction_type)
        object.__setattr__(self, "write_action", write_action)
        object.__setattr__(self, "control_action", control_action)
        object.__setattr__(self, "query_intent", query_intent)
        object.__setattr__(self, "write_evidence", list(self.write_evidence or []))
        object.__setattr__(self, "query_evidence", list(self.query_evidence or []))

    @staticmethod
    def _derive_interaction_type(intent: str) -> str:
        if intent in MUTATING_INTENTS:
            return "WRITE"
        if intent in QUERY_INTENTS:
            return "QUERY"
        if intent in CONTROL_INTENTS:
            return "CONTROL"
        if intent in CHAT_INTENTS:
            return "CHAT"
        return "AMBIGUOUS"

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent,
            "confidence": self.confidence,
            "reason": self.reason,
            "source": self.source,
            "should_update_slots": self.should_update_slots,
            "query_subtype": self.query_subtype,
            "interaction_type": self.interaction_type,
            "write_action": self.write_action,
            "control_action": self.control_action,
            "query_intent": self.query_intent,
            "write_evidence": self.write_evidence,
            "query_evidence": self.query_evidence,
        }


class RouteValidator:
    """路由安全校验层：校验 LLM/规则结果是否允许进入对应处理线路。"""

    def validate(
        self,
        route: IntentRouteResult,
        *,
        phase: str,
        task_state: dict,
        expected_slots: list[str] | None = None,
    ) -> IntentRouteResult:
        expected_slots = expected_slots or []

        if route.interaction_type == "QUERY":
            if route.should_update_slots:
                return self._downgrade(route, "QUERY 路由不得进入槽位写入线路")
            return route

        if route.interaction_type == "WRITE":
            if route.write_action not in ("CREATE", "UPDATE"):
                return self._downgrade(route, "WRITE 路由缺少合法 write_action")
            if not route.write_evidence and not expected_slots:
                return self._downgrade(route, "WRITE 路由缺少写入证据且当前没有 expected_slots")
            if route.write_action == "UPDATE" and not task_state.get("task_type_key") and not expected_slots:
                return self._downgrade(route, "UPDATE 路由缺少已有任务上下文")
            return route

        if route.interaction_type == "CONTROL":
            if route.control_action == "CONFIRM" and phase != "confirming":
                return self._downgrade(route, "CONFIRM 只能在 confirming 阶段执行")
            if route.control_action == "CONTINUE" and phase != "blocked_soft":
                return self._downgrade(route, "CONTINUE 只能在 blocked_soft 阶段执行")
            if route.control_action not in ("CONFIRM", "CANCEL", "CONTINUE"):
                return self._downgrade(route, "CONTROL 路由缺少合法 control_action")
            return route

        if route.interaction_type == "CHAT":
            if route.should_update_slots:
                return self._downgrade(route, "CHAT 路由不得进入槽位写入线路")
            return route

        return self._downgrade(route, "路由类型无法安全执行")

    @staticmethod
    def _downgrade(route: IntentRouteResult, reason: str) -> IntentRouteResult:
        return IntentRouteResult(
            intent="CLARIFICATION",
            confidence=min(route.confidence, 0.5),
            reason=f"{reason}；原始判断：{route.reason}",
            source=route.source,
            should_update_slots=False,
            interaction_type="AMBIGUOUS",
        )


class IntentRouter:
    def __init__(self, llm: LLMClient, device_terms: set[str] | Any | None = None):
        self.llm = llm
        self.route_validator = RouteValidator()
        if hasattr(device_terms, "get_device_alias_index"):
            try:
                self.device_alias_index = device_terms.get_device_alias_index()
                self.ambiguous_device_terms = device_terms.get_ambiguous_device_terms()
                self.device_terms = device_terms.get_all_device_terms()
            except Exception:
                self.device_terms = None
                self.ambiguous_device_terms = None
                self.device_alias_index = None
        else:
            try:
                from .knowledge_retriever import KnowledgeBase
                _kb = KnowledgeBase()
                self.device_alias_index = _kb.get_device_alias_index()
                self.ambiguous_device_terms = _kb.get_ambiguous_device_terms()
                self.device_terms = _kb.get_all_device_terms()
            except Exception:
                self.device_terms = device_terms if isinstance(device_terms, set) else None
                self.ambiguous_device_terms = set()
                self.device_alias_index = {}

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

        # 动态加载知识库设备词汇表、歧义集合和别名索引（避免写死与数据脱节）
        if self.device_terms is None or self.ambiguous_device_terms is None or self.device_alias_index is None:
            try:
                from .knowledge_retriever import KnowledgeBase
                _kb = KnowledgeBase()
                self.device_alias_index = _kb.get_device_alias_index()
                self.ambiguous_device_terms = _kb.get_ambiguous_device_terms()
                self.device_terms = _kb.get_all_device_terms()
            except Exception as e:
                logger.warning(f"[IntentRouter] Failed to load dynamic device terms: {e}")
                self.device_terms = set(DEVICE_ENTITIES)
                self.ambiguous_device_terms = set()
                self.device_alias_index = {}

        # ── 1. 高确定性控制保护规则 ────────────────────────────────────────────
        control_result = self._try_control_rule_routing(msg, task_state, phase)
        if control_result is not None:
            return self.route_validator.validate(
                control_result,
                phase=phase,
                task_state=task_state,
                expected_slots=expected_slots,
            )

        # ── 2. LLM 结构化交互分类 ─────────────────────────────────────────────
        try:
            llm_result = self._call_llm_router(
                msg,
                conversation_history,
                task_state,
                phase,
                expected_slots or [],
            )
            if llm_result is not None:
                return self.route_validator.validate(
                    llm_result,
                    phase=phase,
                    task_state=task_state,
                    expected_slots=expected_slots,
                )
        except Exception as exc:
            logger.warning(f"[IntentRouter] LLM interaction classification error: {exc}")

        # ── 3. 程序兜底规则：只做安全、可解释的分类 ───────────────────────────
        rule_result = self._try_rule_routing(msg, task_state, phase, expected_slots)
        if rule_result is not None:
            return self.route_validator.validate(
                rule_result,
                phase=phase,
                task_state=task_state,
                expected_slots=expected_slots,
            )

        # ── 4. 兜底回退 ────────────────────────────────────────────────────────
        return IntentRouteResult(
            intent="CLARIFICATION",
            confidence=0.5,
            reason="意图未匹配或 LLM 解析异常，安全降级到澄清",
            source="fallback",
            should_update_slots=False,
            interaction_type="AMBIGUOUS",
        )

    def _try_control_rule_routing(
        self,
        msg: str,
        task_state: dict,
        phase: str,
    ) -> IntentRouteResult | None:
        """只保留不会误写槽位的高确定性控制保护规则。"""
        msg_clean = re.sub(r"[。！？,!?. ]+", "", msg.strip()).lower()
        negation_cancel = ["不要取消", "别取消", "不能取消", "不放弃", "不终止", "不是要取消", "不是取消"]
        negation_confirm = ["不好", "不确认", "不要发布", "先别发布", "不能发布", "不要下发", "不发布", "不是确认"]

        explicit_cancel = ["取消当前任务", "放弃当前任务", "终止当前任务", "不要这个任务了", "取消任务", "放弃任务", "终止任务"]
        if any(p in msg for p in explicit_cancel) and not any(nc in msg for nc in negation_cancel):
            return IntentRouteResult(
                intent="TASK_CANCEL",
                confidence=1.0,
                reason="用户明确触发任务取消指令",
                source="rule",
                should_update_slots=False,
                interaction_type="CONTROL",
                control_action="CANCEL",
            )

        if phase in ("blocked_hard", "blocked_soft", "confirming", "collecting") and msg_clean in ("取消", "放弃", "不要了", "终止", "退出") and not any(nc in msg for nc in negation_cancel):
            return IntentRouteResult(
                intent="TASK_CANCEL",
                confidence=1.0,
                reason="用户在任务流程中直接回应取消",
                source="rule",
                should_update_slots=False,
                interaction_type="CONTROL",
                control_action="CANCEL",
            )

        has_negation_on_confirm = any(nc in msg for nc in negation_confirm) or bool(
            re.search(r"(?:不|别|不要|不能|先别).*(?:发布|确认|下发|开始|继续|忽略)", msg)
        )
        if phase == "blocked_soft":
            blocked_soft_confirms = ["确认继续", "确定继续", "忽略警告", "忽略", "继续", "可以继续"]
            if not has_negation_on_confirm and (any(kw in msg for kw in blocked_soft_confirms) or msg_clean in ("确认", "确定", "继续", "忽略", "是的", "好", "可以")):
                return IntentRouteResult(
                    intent="TASK_CONFIRM",
                    confidence=1.0,
                    reason="用户在软警告阶段选择忽略警告或继续",
                    source="rule",
                    should_update_slots=False,
                    interaction_type="CONTROL",
                    control_action="CONTINUE",
                )
        elif phase == "confirming":
            confirming_confirms = ["确认发布", "确认下发", "确定发布", "确认开始", "确定开始"]
            if not has_negation_on_confirm and (any(kw in msg for kw in confirming_confirms) or msg_clean in ("确认", "确定", "确认发布", "确定发布")):
                return IntentRouteResult(
                    intent="TASK_CONFIRM",
                    confidence=1.0,
                    reason="用户在确认阶段明确确认发布任务",
                    source="rule",
                    should_update_slots=False,
                    interaction_type="CONTROL",
                    control_action="CONFIRM",
                )

        if phase not in ("confirming", "blocked_soft") and msg_clean in ("确认", "确定", "好的", "是的", "确认发布") and not task_state.get("task_type_key") and not has_negation_on_confirm:
            return IntentRouteResult(
                intent="CLARIFICATION",
                confidence=0.9,
                reason="非确认阶段独立发送确认词，指向不明确",
                source="rule",
                should_update_slots=False,
                interaction_type="AMBIGUOUS",
            )

        return None

    def _try_rule_routing(
        self, msg: str, task_state: dict, phase: str, expected_slots: list[str] | None
    ) -> IntentRouteResult | None:
        """LLM 不可用或返回非法时的安全兜底规则。

        注意：这里不再做大规模关键词优先抢占，只处理可解释的高置信场景。
        """
        msg_clean = re.sub(r"[。！？,!?. ]+", "", msg.strip()).lower()
        is_q = any(q in msg for q in QUESTION_WORDS)
        has_tool = any(e in msg.lower() for e in TOOL_ENTITIES)

        if expected_slots and not is_q:
            if self._matches_expected_slot(msg, expected_slots, self.device_alias_index):
                return IntentRouteResult(
                    intent="TASK_UPDATE",
                    confidence=0.95,
                    reason="用户正在精准回答系统当前提问的缺失槽位",
                    source="rule",
                    should_update_slots=True,
                    interaction_type="WRITE",
                    write_action="UPDATE",
                    write_evidence=[msg],
                )

        if not is_q and self._looks_like_task_creation(msg):
            return IntentRouteResult(
                intent="TASK_CREATE",
                confidence=0.9,
                reason="兜底规则识别到任务动作结构和作业对象结构",
                source="rule",
                should_update_slots=True,
                interaction_type="WRITE",
                write_action="CREATE",
                write_evidence=[msg],
            )

        if not is_q and self._looks_like_slot_submission(msg):
            return IntentRouteResult(
                intent="TASK_UPDATE",
                confidence=0.9,
                reason="兜底规则识别到字段提交或字段修改结构",
                source="rule",
                should_update_slots=True,
                interaction_type="WRITE",
                write_action="UPDATE",
                write_evidence=[msg],
            )

        if any(kw in msg for kw in ("当前任务有哪些参数", "当前任务填了什么", "当前任务还缺什么", "当前任务进行到哪一步", "当前任务状态", "任务进度")):
            return IntentRouteResult(
                intent="TASK_STATUS",
                confidence=0.95,
                reason="兜底规则识别到任务状态查询",
                source="rule",
                should_update_slots=False,
                query_subtype="task_progress",
                interaction_type="QUERY",
                query_intent="TASK_STATUS",
                query_evidence=[msg],
            )

        if is_q and any(kw in msg for kw in ("管缆类型", "任务需要哪些参数", "需要哪些参数", "必填字段", "作业规则", "管缆如何分类", "管道分类", "任务模板")):
            return IntentRouteResult(
                intent="KNOWLEDGE_QA",
                confidence=0.95,
                reason="兜底规则识别到业务知识查询",
                source="rule",
                should_update_slots=False,
                query_subtype="knowledge",
                interaction_type="QUERY",
                query_intent="KNOWLEDGE_QA",
                query_evidence=[msg],
            )

        if (is_q or any(kw in msg for kw in ("列表", "哪些工具", "可以用什么", "适合使用"))) and has_tool:
            return IntentRouteResult(
                intent="TOOL_QUERY",
                confidence=0.95,
                reason="兜底规则识别到工具或载荷查询",
                source="rule",
                should_update_slots=False,
                query_subtype="available_tools",
                interaction_type="QUERY",
                query_intent="TOOL_QUERY",
                query_evidence=[msg],
            )

        is_device_capability_query = (
            is_q
            and any(kw in msg for kw in ("最大水深", "作业水深", "下潜能力", "作业能力", "能否作业", "设备参数", "设备能力", "型号"))
        )
        if is_device_capability_query:
            return IntentRouteResult(
                intent="DEVICE_CAPABILITY",
                confidence=0.95,
                reason="兜底规则识别到设备能力查询",
                source="rule",
                should_update_slots=False,
                query_subtype="device_capabilities",
                interaction_type="QUERY",
                query_intent="DEVICE_CAPABILITY",
                query_evidence=[msg],
            )

        if msg_clean in ("你好", "您好", "你好啊", "哈喽", "hi", "hello", "早上好", "下午好", "晚上好") or any(kw in msg_clean for kw in ("谢谢", "多谢", "感谢", "thx", "thanks", "你能做什么", "自我介绍", "功能介绍", "你能干嘛")):
            return IntentRouteResult(
                intent="GENERAL_CHAT",
                confidence=0.95,
                reason="兜底规则识别到普通聊天",
                source="rule",
                should_update_slots=False,
                interaction_type="CHAT",
            )

        return None

    @staticmethod
    def _looks_like_task_creation(msg: str) -> bool:
        """兜底识别任务创建结构：动作语义 + 作业对象，不绑定某个完整说法。"""
        action_pattern = r"(?:创建|新建|规划|安排|执行|开展|开始|做|进行|发起)"
        object_pattern = r"(?:任务|作业|巡检|插入|拔出|控制面板|管缆|采油树)"
        return bool(re.search(action_pattern, msg) and re.search(object_pattern, msg))

    @staticmethod
    def _looks_like_slot_submission(msg: str) -> bool:
        """兜底识别字段提交结构：字段名/字段域 + 值或选择动作。"""
        field_pattern = (
            r"(?:开始|结束|时间|起始|终止|坐标|经纬度|水深|深度|类型|系列|型号|编号|"
            r"设备|机器人|工具|载荷|母船|支持船|井口|油田)"
        )
        assignment_pattern = r"(?:为|是|用|使用|选择|选|改|换|携带|带|配备|搭载|编号|类型|型号)"
        return bool(re.search(field_pattern, msg) and re.search(assignment_pattern, msg))

    @staticmethod
    def _matches_expected_slot(msg: str, expected_slots: list[str], device_alias_index: dict | None = None) -> bool:
        msg_s = msg.strip()
        msg_device_norm = _normalize_device_text(msg_s)
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
            elif slot in ("equipment_type", "equipment_name", "robot_id", "robot", "equipment"):
                if any(kw in msg_s.lower() for kw in ["rov", "auv", "重载", "观察级", "工作级", "机器人", "金牛座", "天鹰座", "御夫座", "凤凰座", "crawler", "wrov"]):
                    return True
                if device_alias_index:
                    for alias in device_alias_index:
                        if alias and _normalize_device_text(alias) in msg_device_norm:
                            return True
            elif slot == "support_vessel":
                if any(kw in msg_s for kw in ["船", "海洋石油", "勘探", "号"]):
                    return True
            elif slot == "cable_type":
                if any(kw in msg_s for kw in ["电缆", "光缆", "脐带缆", "管缆", "油气管道", "管道"]):
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
        expected_slots: list[str],
    ) -> IntentRouteResult | None:
        if self.llm is None:
            return None

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
            logger.warning(f"[IntentRouter] Failed to parse interaction JSON from LLM: '{parsed}'")
            return None

        interaction_type = str(parsed.get("interaction_type", "")).strip().upper()
        if interaction_type not in VALID_INTERACTION_TYPES:
            logger.warning(f"[IntentRouter] Invalid interaction_type from LLM: '{interaction_type}'")
            return None

        if "confidence" not in parsed or parsed["confidence"] is None:
            logger.warning("[IntentRouter] Missing confidence field in LLM response")
            return None

        c_val = parsed["confidence"]
        if isinstance(c_val, bool) or not isinstance(c_val, (int, float)):
            logger.warning(f"[IntentRouter] Invalid type for confidence: {type(c_val)}")
            return None

        c_float = float(c_val)
        if not math.isfinite(c_float) or c_float < 0.0 or c_float > 1.0:
            logger.warning(f"[IntentRouter] Out of bounds or non-finite confidence: {c_float}")
            return None

        reason = parsed.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            logger.warning("[IntentRouter] Missing or empty reason field in LLM response")
            return None

        if c_float < 0.6:
            return IntentRouteResult(
                intent="CLARIFICATION",
                confidence=c_float,
                reason=f"LLM识别置信度过低({c_float:.2f}): {reason}",
                source="llm",
                should_update_slots=False,
                interaction_type="AMBIGUOUS",
            )

        write_action = str(parsed.get("write_action") or "NONE").strip().upper()
        control_action = str(parsed.get("control_action") or "NONE").strip().upper()
        query_intent = parsed.get("query_intent")
        query_intent = str(query_intent).strip().upper() if query_intent else None

        if write_action not in VALID_WRITE_ACTIONS:
            logger.warning(f"[IntentRouter] Invalid write_action from LLM: {write_action}")
            return None
        if control_action not in VALID_CONTROL_ACTIONS:
            logger.warning(f"[IntentRouter] Invalid control_action from LLM: {control_action}")
            return None
        if query_intent not in VALID_QUERY_INTENTS:
            logger.warning(f"[IntentRouter] Invalid query_intent from LLM: {query_intent}")
            return None

        write_evidence = parsed.get("write_evidence")
        query_evidence = parsed.get("query_evidence")
        if not isinstance(write_evidence, list):
            write_evidence = []
        if not isinstance(query_evidence, list):
            query_evidence = []
        write_evidence = [str(item).strip() for item in write_evidence if str(item).strip()]
        query_evidence = [str(item).strip() for item in query_evidence if str(item).strip()]

        if interaction_type == "WRITE":
            if write_action == "NONE":
                write_action = "UPDATE" if task_state.get("task_type_key") else "CREATE"
            intent = "TASK_CREATE" if write_action == "CREATE" else "TASK_UPDATE"
            return IntentRouteResult(
                intent=intent,
                confidence=c_float,
                reason=reason.strip(),
                source="llm",
                should_update_slots=True,
                interaction_type="WRITE",
                write_action=write_action,
                write_evidence=write_evidence,
            )

        if interaction_type == "QUERY":
            if query_intent is None:
                query_intent = "KNOWLEDGE_QA"
            return IntentRouteResult(
                intent=query_intent,
                confidence=c_float,
                reason=reason.strip(),
                source="llm",
                should_update_slots=False,
                query_subtype=self._query_subtype_for_intent(query_intent),
                interaction_type="QUERY",
                query_intent=query_intent,
                query_evidence=query_evidence,
            )

        if interaction_type == "CONTROL":
            intent = "TASK_CONFIRM" if control_action in ("CONFIRM", "CONTINUE") else "TASK_CANCEL"
            return IntentRouteResult(
                intent=intent,
                confidence=c_float,
                reason=reason.strip(),
                source="llm",
                should_update_slots=False,
                interaction_type="CONTROL",
                control_action=control_action,
            )

        if interaction_type == "CHAT":
            return IntentRouteResult(
                intent="GENERAL_CHAT",
                confidence=c_float,
                reason=reason.strip(),
                source="llm",
                should_update_slots=False,
                interaction_type="CHAT",
            )

        return IntentRouteResult(
            intent="CLARIFICATION",
            confidence=c_float,
            reason=reason.strip(),
            source="llm",
            should_update_slots=False,
            interaction_type="AMBIGUOUS",
        )

    @staticmethod
    def _query_subtype_for_intent(query_intent: str | None) -> str | None:
        return {
            "TASK_STATUS": "task_progress",
            "TOOL_QUERY": "available_tools",
            "DEVICE_CAPABILITY": "device_capabilities",
            "DEVICE_STATUS": "telemetry",
            "ENVIRONMENT_QUERY": "environment",
            "KNOWLEDGE_QA": "knowledge",
        }.get(query_intent or "")
