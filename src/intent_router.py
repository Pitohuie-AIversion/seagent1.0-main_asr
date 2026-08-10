"""
src/intent_router.py - 结构化交互路由器

IntentRouter 将用户输入路由至三种对话模式之一：
- task_collection：任务数据收集与参数修改
- knowledge_qa：知识问答、能力/状态查询与普通聊天（含意图澄清 CLARIFICATION）
- emergency_intervention：紧急控制干预（需二次确定性验证）

在 G7 架构中，IntentRouter 内部完全基于结构化 InteractionPlan 进行推导与后端确定性校验。
"""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass
from typing import Any, Literal

from .interaction_plan import (
    InteractionPlan,
    OperationType,
    DialogueModeType,
    SubjectType,
    RelationType,
    SourcePolicyType,
    validate_interaction_plan,
    build_clarify_fallback_plan,
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

INTENT_ROUTER_SYSTEM = """\
你负责判断用户本轮输入的交互语义，并输出结构化的交互计划 InteractionPlan。

【四种操作类型 (operation)】

1. READ (只读查询与知识问答):
用户正在索取信息、查询设备能力/工具/状态、查询环境/系统规则、
进行普通聊天，或对控制操作进行解释性询问（如"如何取消任务"、"如果停止任务会怎样"、"停止任务有什么影响"）。
严禁修改任务状态，不调用 Extractor，零状态副作用。

示例结构：
- 介绍金牛座 -> subject_type="device", subject_text="金牛座", relation="describe", source_policy="project_kb"
- 金牛座属于哪个family -> subject_type="device", subject_text="金牛座", relation="belongs_to", source_policy="project_kb"
- 金牛座支持哪些payload / 哪些机器人支持机械臂 -> relation="supports", source_policy="project_kb"
- AUV和ROV有什么区别 -> subject_type="device_class", relation="compare", source_policy="hybrid"
- 当前任务还缺什么 / 刚才填写了什么 -> subject_type="task", relation="missing_fields"|"filled_fields", source_policy="session_state"
- 金牛座现在状态怎么样 -> subject_type="device", relation="status", source_policy="realtime_state"

2. WRITE (显式任务数据提交与修改):
用户明确创建任务、修改任务参数（水深改成500米、更换机器人天鹰座、增加高清摄像机），
或回答系统追问的 expected_slots，或否定控制动作后继续提交参数。

3. CONTROL (明确紧急控制动作):
用户发出明确的紧急控制动作命令，要求暂停、停止、终止或取消当前任务。
必须包含 emergency_action ('stop', 'pause', 'abort', 'cancel')。

4. CLARIFY (极度模糊表达或裸词歧义):
用户表达模糊（"帮我看看机器人"、"处理一下设备"、"这个怎么样"）或包含单独裸词（"停止"），无法安全决定操作。
必须只读，needs_clarification=true。

【输出结构 JSON 示例】
{
  "schema_version": 1,
  "operation": "READ",
  "dialogue_mode": "knowledge_qa",
  "query_intent": "DEVICE_CAPABILITY",
  "subject_type": "device",
  "subject_text": "金牛座",
  "relation": "describe",
  "source_policy": "project_kb",
  "needs_clarification": false,
  "clarification_reason": null,
  "emergency_action": null,
  "confidence": 0.95,
  "reason_code": "DEVICE_CAPABILITY_READ"
}

只能输出严格 JSON，不得输出其他文字。
"""


class IntentRoutingError(Exception):
    """IntentRouter 协议识别失败。"""


VALID_EMERGENCY_ACTIONS = {"stop", "pause", "abort", "cancel"}


@dataclass(frozen=True)
class IntentRouteResult:
    interaction_type: InteractionType
    confidence: float
    reason: str
    query_intent: str | None = None
    dialogue_mode: DialogueMode = "task_collection"
    source: str = "rule"
    emergency_action: str | None = None
    interaction_plan: InteractionPlan | None = None

    def __post_init__(self) -> None:
        it_str = str(self.interaction_type).strip().upper()
        if it_str not in VALID_INTERACTION_TYPES:
            raise ValueError(f"非法 interaction_type: {self.interaction_type}")

        dm_str = str(self.dialogue_mode).strip().lower()
        query_intent = (
            str(self.query_intent).strip().upper() if self.query_intent else None
        )

        # 1. 统一校验 emergency_action 合法性
        act = self.emergency_action
        if act is not None and act not in VALID_EMERGENCY_ACTIONS:
            object.__setattr__(self, "emergency_action", None)
            act = None
            dm_str = "knowledge_qa"
            it_str = "QUERY"
            query_intent = "CLARIFICATION"

        if act in VALID_EMERGENCY_ACTIONS:
            dm_str = "emergency_intervention"
            it_str = "QUERY"
            query_intent = None
        elif dm_str == "emergency_intervention":
            dm_str = "knowledge_qa"
            it_str = "QUERY"
            query_intent = "CLARIFICATION"
            object.__setattr__(self, "emergency_action", None)
            object.__setattr__(
                self, "reason", "规则降级: 紧急介入模式缺少合法控制动作"
            )
        elif it_str == "WRITE":
            dm_str = "task_collection"
        elif it_str == "QUERY" and dm_str == "task_collection":
            dm_str = "knowledge_qa"

        if dm_str not in VALID_DIALOGUE_MODES:
            raise ValueError(f"非法 dialogue_mode: {self.dialogue_mode}")

        if dm_str == "task_collection":
            it_str = "WRITE"
            query_intent = None
        elif dm_str == "knowledge_qa" and (
            not query_intent or query_intent not in VALID_QUERY_INTENTS
        ):
            it_str = "QUERY"
            query_intent = "KNOWLEDGE_QA"

        object.__setattr__(self, "dialogue_mode", dm_str)
        object.__setattr__(self, "interaction_type", it_str)
        object.__setattr__(self, "query_intent", query_intent)

    def to_dict(self) -> dict[str, Any]:
        d = {
            "interaction_type": self.interaction_type,
            "confidence": self.confidence,
            "reason": self.reason,
            "query_intent": self.query_intent,
            "dialogue_mode": self.dialogue_mode,
            "source": self.source,
            "emergency_action": self.emergency_action,
        }
        if self.interaction_plan is not None:
            d["interaction_plan"] = self.interaction_plan.to_dict()
        return d

    @property
    def intent(self) -> str | None:
        if self.dialogue_mode == "task_collection":
            return "TASK_UPDATE"
        elif self.dialogue_mode == "emergency_intervention":
            return "EMERGENCY_INTERVENTION"
        return self.query_intent or self.interaction_type

    @property
    def is_query(self) -> bool:
        return self.dialogue_mode != "task_collection"

    @property
    def should_update_slots(self) -> bool:
        return self.dialogue_mode == "task_collection"


def _extract_subject_relation_policy(
    user_message: str,
    query_intent: str | None,
    task_state: dict,
) -> tuple[SubjectType | None, str | None, RelationType | None, SourcePolicyType]:
    msg = user_message.strip()

    subject_text = None
    subject_type: SubjectType | None = None

    families = ("天鹰座", "金牛座", "水蛟", "海马", "CRAWLER", "LROV", "1600", "WORK", "观察级", "工作级", "亚特兰蒂斯", "深海")
    for fam in families:
        if fam in msg:
            subject_text = fam
            subject_type = "device"
            break

    if not subject_type:
        classes = ("ROV", "AUV", "HOV", "潜水器", "机器人", "设备")
        for cls in classes:
            if cls in msg or cls.lower() in msg.lower():
                subject_text = cls
                subject_type = "device_class"
                break

    if not subject_type:
        payloads = ("机械臂", "摄像机", "声呐", "声纳", "抓手", "传感器", "payload", "负载", "载荷", "工具")
        for p in payloads:
            if p in msg or p.lower() in msg.lower():
                subject_text = p
                subject_type = "payload"
                break

    if not subject_type:
        rules = ("软约束", "硬约束", "忽略警告", "规则", "约束", "保存位置", "存储位置")
        for r in rules:
            if r in msg:
                subject_text = r
                subject_type = "system_rule"
                break

    if not subject_type:
        envs = ("海况", "水温", "底质", "海床")
        for e in envs:
            if e in msg:
                subject_text = e
                subject_type = "environment"
                break

    if not subject_type:
        realtime_terms = ("实时深度", "当前水深", "当前深度", "当前位置", "当前电量", "电量", "实时状态")
        for rt in realtime_terms:
            if rt in msg:
                subject_text = rt
                subject_type = "realtime_state"
                break

    if not subject_type:
        task_terms = ("当前任务", "进度", "步骤", "已经填写", "填写了什么", "缺什么", "缺少", "缺失", "参数")
        for t in task_terms:
            if t in msg:
                subject_text = "当前任务"
                subject_type = "task"
                break

    if not subject_type:
        subject_type = "general_concept"

    relation: RelationType | None = None
    if any(k in msg for k in ("属于哪个", "属于", "family", "族", "class")):
        relation = "belongs_to"
    elif any(k in msg for k in ("支持哪些", "支持", "搭载", "配备", "携带", "适合")):
        relation = "supports"
    elif any(k in msg for k in ("区别", "不同", "差异")):
        relation = "compare"
    elif any(k in msg for k in ("定义", "含义", "概念", "什么是", "为何")):
        relation = "definition"
    elif any(k in msg for k in ("缺什么", "缺少", "缺失", "缺啥")):
        relation = "missing_fields"
    elif any(k in msg for k in ("填写了哪些", "已填写", "已经填写", "填写了什么", "已有参数", "填写")):
        relation = "filled_fields"
    elif any(k in msg for k in ("能力", "最大水深", "限制", "作业吗", "工作吗")):
        relation = "capabilities"
    elif any(k in msg for k in ("状态", "电量", "位置", "深度")):
        relation = "status"
    elif any(k in msg for k in ("如何", "怎么", "流程", "步骤", "影响", "如果")):
        relation = "procedure"
    elif any(k in msg for k in ("有哪些", "列表", "包含哪些", "包含什么")):
        relation = "list"
    else:
        relation = "describe"

    source_policy: SourcePolicyType = "project_kb"
    if subject_type == "task" or relation in ("missing_fields", "filled_fields"):
        source_policy = "session_state"
    elif subject_type == "realtime_state" or "实时" in msg or "电量" in msg or "位置" in msg:
        source_policy = "realtime_state"
    elif relation == "compare":
        source_policy = "hybrid"
    elif subject_type in ("device", "device_class", "device_family", "payload", "system_rule", "general_concept", "environment"):
        source_policy = "project_kb"

    return subject_type, subject_text, relation, source_policy


class IntentRouter:
    def __init__(self, llm: LLMClient):
        self.llm = llm

    @staticmethod
    def _parse_executable_control_action(user_message: str) -> str | None:
        """从用户消息中提取明确可执行的紧急控制动作。"""
        clauses = [
            c.strip()
            for c in re.split(
                r"[,，;；!！.。\n]|(?<!^)(?=(?:为什么|为何|怎么|如果|要是|假如|假设|万一|而是|改为|改成|然后|接着|并且|同时|但是|但|不过|并))",
                user_message,
            )
            if c and c.strip()
        ]

        action_map = {
            "pause": ("暂停下潜", "暂停", "稍停"),
            "stop": ("停止", "停下"),
            "abort": ("终止", "强行终止", "中止"),
            "cancel": ("取消", "撤销", "放弃", "不要了"),
        }

        negation_phrases = (
            "不停止", "先不停止", "暂不停止", "不需要停止", "不要停止", "别停止", "不要立即停止", "不要马上停止", "不是要停止",
            "不暂停", "先不暂停", "暂不暂停", "不需要暂停", "不要暂停", "别暂停", "不要立即暂停", "不要马上暂停", "不是要暂停",
            "不取消", "先不取消", "暂不取消", "不需要取消", "不要取消", "别取消", "不要立即取消", "不要马上取消", "不是要取消",
            "不终止", "先不终止", "暂不终止", "不需要终止", "不要终止", "别终止", "不要立即终止", "不要马上终止", "不是要终止",
        )
        negation_prefixes = ("不要", "先不", "暂不", "不需要", "没必要", "别", "不用", "无需", "免", "不许", "不能", "不是", "不是要")

        non_task_objects = (
            "打印",
            "播报",
            "回答",
            "生成",
            "功能",
            "告警",
            "说明",
            "输出",
            "页面",
            "刷新",
            "日志",
            "展示",
        )

        for clause in clauses:
            is_non_task_clause = False
            for obj in non_task_objects:
                if obj in clause:
                    for act_key, keywords in action_map.items():
                        for kw in keywords:
                            if kw in clause:
                                kw_pos = clause.find(kw)
                                obj_pos = clause.find(obj)
                                if kw_pos != -1 and obj_pos != -1 and kw_pos < obj_pos and (obj_pos - kw_pos) <= 8:
                                    has_real_robot_target = any(
                                        t in clause
                                        for t in (
                                            "当前任务",
                                            "当前操作",
                                            "水下",
                                            "下潜",
                                            "巡检",
                                            "采集",
                                            "作业",
                                            "设备",
                                            "机器人",
                                            "管道",
                                        )
                                    )
                                    if not has_real_robot_target:
                                        is_non_task_clause = True
                                        break
                        if is_non_task_clause:
                            break
                if is_non_task_clause:
                    break

            if is_non_task_clause:
                continue

            action_found = None
            for act_key, keywords in action_map.items():
                if any(kw in clause for kw in keywords):
                    action_found = act_key
                    break

            if not action_found:
                continue

            has_local_negation = any(phrase in clause for phrase in negation_phrases)
            if not has_local_negation:
                for neg in negation_prefixes:
                    if neg in clause:
                        neg_pos = clause.find(neg)
                        for act_kw in action_map[action_found]:
                            act_pos = clause.find(act_kw)
                            if act_pos != -1 and neg_pos < act_pos and (act_pos - neg_pos) <= 3:
                                has_local_negation = True
                                break
                        if has_local_negation:
                            break

            if has_local_negation:
                continue

            has_local_question = bool(re.search(r"[呢吗？?]$", clause)) or any(
                kw in clause
                for kw in (
                    "是否",
                    "吗",
                    "么",
                    "什么",
                    "可不可以",
                    "能不能",
                    "会不会",
                    "有没有",
                    "需不需要",
                    "要不要",
                    "什么意思",
                    "有何影响",
                    "如何",
                    "怎么",
                    "为什么",
                    "权限",
                    "规则",
                    "条件",
                )
            )
            has_local_conditional = any(
                kw in clause
                for kw in ("如果", "要是", "假如", "假使", "若", "假设", "万一")
            )

            if has_local_question or has_local_conditional:
                continue

            has_target_or_prompt = any(
                cue in clause
                for cue in (
                    "当前任务",
                    "任务",
                    "操作",
                    "流程",
                    "当前操作",
                    "草稿",
                    "立即",
                    "马上",
                    "紧急",
                    "立刻",
                    "机器人",
                    "下潜",
                    "巡检",
                    "采集",
                    "作业",
                    "设备",
                    "指令",
                )
            )

            if has_target_or_prompt:
                if any(k in clause for k in ("修改", "更新", "槽位", "填写的")):
                    return None
                return action_found

        return None

    def _build_plan_result(
        self,
        user_message: str,
        operation: OperationType,
        dialogue_mode: DialogueModeType,
        query_intent: str | None = None,
        subject_type: SubjectType | None = "unknown",
        subject_text: str | None = None,
        relation: RelationType | None = "unknown",
        source_policy: SourcePolicyType = "none",
        needs_clarification: bool = False,
        clarification_reason: str | None = None,
        emergency_action: str | None = None,
        confidence: float = 0.95,
        reason_code: str = "RULE_ROUTE",
        source: str = "rule",
        context: dict | None = None,
    ) -> IntentRouteResult:
        raw_plan = {
            "schema_version": 1,
            "operation": operation,
            "dialogue_mode": dialogue_mode,
            "query_intent": query_intent,
            "subject_type": subject_type,
            "subject_text": subject_text,
            "relation": relation,
            "source_policy": source_policy,
            "needs_clarification": needs_clarification,
            "clarification_reason": clarification_reason,
            "emergency_action": emergency_action,
            "confidence": confidence,
            "reason_code": reason_code,
        }
        validated_plan = validate_interaction_plan(raw_plan, user_message=user_message, context=context)
        res = validated_plan.to_intent_route_result()
        object.__setattr__(res, "source", source)
        return res

    def _rule_deterministic_route(
        self,
        user_message: str,
        conversation_history: list[dict],
        task_state: dict,
        phase: str,
        expected_slots: list[str] | None = None,
    ) -> IntentRouteResult | None:
        """优先执行的确定性安全规则。"""
        msg = user_message.strip()

        # 构建规则路由上下文
        route_ctx = {
            "expected_slots": expected_slots or [],
            "task_state": task_state,
            "phase": phase,
        }

        # 1. 优先提取明确的紧急干预动作（紧急控制最高优先级）
        has_control_word = any(
            kw in msg for kw in ("停止", "暂停", "取消", "终止", "撤销", "放弃")
        )
        if has_control_word:
            action = self._parse_executable_control_action(msg)
            if action is not None:
                st, stext, rel, sp = _extract_subject_relation_policy(msg, None, task_state)
                return self._build_plan_result(
                    user_message=msg,
                    operation="CONTROL",
                    dialogue_mode="emergency_intervention",
                    emergency_action=action,
                    subject_type=st or "task",
                    subject_text=stext or "当前任务",
                    relation=rel or "procedure",
                    source_policy=sp or "session_state",
                    confidence=0.99,
                    reason_code=f"EMERGENCY_CONTROL_{action.upper()}",
                    context=route_ctx,
                )

        # 2. 非任务控制对象拦截
        if any(
            kw in msg
            for kw in (
                "停止回答",
                "停止生成",
                "暂停功能",
                "取消告警",
                "终止说明输出",
            )
        ):
            st, stext, rel, sp = _extract_subject_relation_policy(msg, "KNOWLEDGE_QA", task_state)
            return self._build_plan_result(
                user_message=msg,
                operation="READ",
                dialogue_mode="knowledge_qa",
                query_intent="KNOWLEDGE_QA",
                subject_type=st,
                subject_text=stext,
                relation=rel,
                source_policy=sp,
                confidence=0.9,
                reason_code="NON_TASK_CONTROL_INTERCEPT",
                context=route_ctx,
            )

        # 3. 含有控制词但整体为疑问/条件且未匹配独立紧急控制动作的否决分支 (Read-First 原则)
        is_question = bool(re.search(r"[呢吗？?]$", msg)) or any(
            kw in msg
            for kw in (
                "是否",
                "吗",
                "么",
                "什么",
                "可不可以",
                "能不能",
                "会不会",
                "有没有",
                "需不需要",
                "要不要",
                "什么意思",
                "有何影响",
                "如何",
                "怎么",
                "为什么",
                "权限",
                "方法",
                "影响",
            )
        )
        is_conditional = any(
            kw in msg
            for kw in ("如果", "要是", "假如", "假使", "若", "假设", "万一")
        )
        if has_control_word and (is_question or is_conditional):
            st, stext, rel, sp = _extract_subject_relation_policy(msg, "KNOWLEDGE_QA", task_state)
            return self._build_plan_result(
                user_message=msg,
                operation="READ",
                dialogue_mode="knowledge_qa",
                query_intent="KNOWLEDGE_QA",
                subject_type=st or "system_rule",
                subject_text=stext or "控制流程",
                relation=rel or "procedure",
                source_policy=sp or "project_kb",
                confidence=0.9,
                reason_code="CONTROL_QUESTION_READ",
                context=route_ctx,
            )

        # 4. 否定控制动作 (若同时提交显式参数修改如'改成500米'，优先走 task_collection WRITE)
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
                "暂不取消",
                "不是要取消",
                "不要停止",
                "别停止",
                "不停止",
                "先不停止",
                "不要立即停止",
                "不是要停止",
                "不要暂停",
                "别暂停",
                "不暂停",
                "先不暂停",
                "暂不暂停",
                "不是要暂停",
                "不要终止",
                "别终止",
                "不终止",
                "不需要终止",
                "暂不终止",
                "不是要终止",
            )
        ):
            if re.search(
                r"(?:[0-9]+|改成|设为|设置为|替换为|调整为|水深|深度|管缆|工具|设备|支持船)",
                msg,
            ):
                st, stext, rel, sp = _extract_subject_relation_policy(msg, None, task_state)
                return self._build_plan_result(
                    user_message=msg,
                    operation="WRITE",
                    dialogue_mode="task_collection",
                    subject_type=st or "task",
                    subject_text=stext,
                    relation=rel or "filled_fields",
                    source_policy=sp or "session_state",
                    confidence=0.95,
                    reason_code="NEGATE_CONTROL_WITH_WRITE",
                    context=route_ctx,
                )
            return self._build_plan_result(
                user_message=msg,
                operation="CLARIFY",
                dialogue_mode="knowledge_qa",
                query_intent="CLARIFICATION",
                needs_clarification=True,
                clarification_reason="用户表达了否定控制指令，需澄清后明确后续意图",
                confidence=0.85,
                reason_code="NEGATE_CONTROL_CLARIFY",
                context=route_ctx,
            )

        # 5. 确定性紧急动作与裸词处理
        if has_control_word:
            action = self._parse_executable_control_action(msg)
            if action is not None:
                st, stext, rel, sp = _extract_subject_relation_policy(msg, None, task_state)
                return self._build_plan_result(
                    user_message=msg,
                    operation="CONTROL",
                    dialogue_mode="emergency_intervention",
                    emergency_action=action,
                    subject_type=st or "task",
                    subject_text=stext or "当前任务",
                    relation=rel or "procedure",
                    source_policy=sp or "session_state",
                    confidence=0.95,
                    reason_code=f"EMERGENCY_CONTROL_{action.upper()}",
                    context=route_ctx,
                )
            if msg in ("停止", "暂停", "取消", "终止", "撤销", "放弃"):
                return self._build_plan_result(
                    user_message=msg,
                    operation="CLARIFY",
                    dialogue_mode="knowledge_qa",
                    query_intent="CLARIFICATION",
                    needs_clarification=True,
                    clarification_reason=f"接收到模糊控制裸词【{msg}】，需澄清明确作用对象",
                    confidence=0.85,
                    reason_code="BARE_CONTROL_CLARIFY",
                    context=route_ctx,
                )

        # 6. 确认/发布指令
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
                return self._build_plan_result(
                    user_message=msg,
                    operation="WRITE",
                    dialogue_mode="task_collection",
                    subject_type="task",
                    subject_text="当前任务",
                    relation="procedure",
                    source_policy="session_state",
                    confidence=0.95,
                    reason_code="CONFIRM_PUBLISH_WRITE",
                    context=route_ctx,
                )

        # 0. 纯标点符号/非词汇输入拦截
        if not re.search(r"[\u4e00-\u9fa5a-zA-Z0-9]", msg.strip()):
            return self._build_plan_result(
                user_message=msg,
                operation="CLARIFY",
                dialogue_mode="knowledge_qa",
                query_intent="CLARIFICATION",
                needs_clarification=True,
                clarification_reason="纯标点符号或非词汇输入",
                confidence=0.85,
                reason_code="PUNCTUATION_CLARIFY",
                context=route_ctx,
            )

        # 7. 只读查询与意图分流 (Read-First 原则)
        msg_for_query_check = msg.replace("支持船", "")
        is_query_sentence = is_question or any(
            kw in msg_for_query_check for kw in (
                "哪些", "如何", "为什么", "是否", "能否", "有没有", "怎么", "什么", "几", "多少",
                "支持", "适合", "区别", "作用", "介绍", "说明", "解释", "属于", "干什么", "保存在哪", "在哪", "怎么样"
            )
        )

        has_write_verb = any(
            kw in msg for kw in (
                "创建", "新建", "发起", "改成", "改为", "设为", "设置为",
                "替换为", "调整为", "调整到", "修改为", "修改到", "改到", "切换为", "换成", "去检查", "去巡检", "去操作", "去埋设", "让", "取消", "撤销",
                "增加", "添加", "加上", "带上", "配备", "搭载", "删除", "移除", "去掉", "指定", "使用", "携带"
            )
        )

        is_meta_workflow_query = is_query_sentence and any(
            kw in msg for kw in ("怎么", "如何", "为什么", "流程", "说明", "帮助", "规则", "介绍")
        )

        has_explicit_write_action = has_write_verb and not is_meta_workflow_query and not is_query_sentence

        msg_clean = msg.lower().strip()
        is_bare_domain_noun = msg_clean in (
            "payload", "负载", "载荷", "工具", "抓手", "传感器", "机械臂", "摄像机", "声呐", "声纳",
            "软约束", "硬约束", "设备", "机器人"
        )

        # 7.1 显式写入动作直接进入 task_collection (WRITE)
        if has_explicit_write_action:
            st, stext, rel, sp = _extract_subject_relation_policy(msg, None, task_state)
            return self._build_plan_result(
                user_message=msg,
                operation="WRITE",
                dialogue_mode="task_collection",
                subject_type=st or "task",
                subject_text=stext,
                relation=rel or "filled_fields",
                source_policy=sp or "session_state",
                confidence=0.95,
                reason_code="EXPLICIT_WRITE_ACTION",
                context=route_ctx,
            )

        # 7.2 裸领域名词只读处理 (READ)
        if is_bare_domain_noun:
            if msg_clean in ("payload", "负载", "载荷", "工具", "抓手", "传感器", "机械臂", "摄像机", "声呐", "声纳"):
                return self._build_plan_result(
                    user_message=msg,
                    operation="READ",
                    dialogue_mode="knowledge_qa",
                    query_intent="TOOL_QUERY",
                    subject_type="payload",
                    subject_text=msg_clean,
                    relation="list",
                    source_policy="project_kb",
                    confidence=0.9,
                    reason_code="BARE_NOUN_TOOL_READ",
                    context=route_ctx,
                )
            elif msg_clean in ("设备", "机器人"):
                return self._build_plan_result(
                    user_message=msg,
                    operation="READ",
                    dialogue_mode="knowledge_qa",
                    query_intent="DEVICE_CAPABILITY",
                    subject_type="device_class",
                    subject_text=msg_clean,
                    relation="list",
                    source_policy="project_kb",
                    confidence=0.9,
                    reason_code="BARE_NOUN_DEVICE_READ",
                    context=route_ctx,
                )
            else:
                return self._build_plan_result(
                    user_message=msg,
                    operation="READ",
                    dialogue_mode="knowledge_qa",
                    query_intent="KNOWLEDGE_QA",
                    subject_type="general_concept",
                    subject_text=msg_clean,
                    relation="describe",
                    source_policy="project_kb",
                    confidence=0.9,
                    reason_code="BARE_NOUN_KNOWLEDGE_READ",
                    context=route_ctx,
                )

        # 7.3 广义只读查询意图分流 (READ)
        is_broad_device_query = (
            any(verb in msg for verb in ("查询", "查看", "列出", "检索", "显示", "获取", "了解", "介绍", "说明", "解释"))
            and any(noun in msg for noun in ("设备", "机器人", "潜水器", "ROV", "AUV", "HOV", "负载", "载荷", "工具", "抓手", "传感器"))
        )

        if is_query_sentence or is_broad_device_query or is_meta_workflow_query:
            # A. 歧义设备别名拦截 (如 "一号机" / "001" 未带具体系列名) -> CLARIFY
            is_device_context = any(dev in msg for dev in ("机", "设备", "水深", "深度", "作业", "下潜", "机器人", "能力", "搭载", "模式"))
            is_ambiguous_alias = ("一号机" in msg or "二号机" in msg or ("001" in msg and is_device_context))
            has_family = any(fam in msg for fam in ("天鹰座", "金牛座", "水蛟", "海马", "CRAWLER", "LROV", "1600", "WORK"))
            if is_ambiguous_alias and not has_family:
                return self._build_plan_result(
                    user_message=msg,
                    operation="CLARIFY",
                    dialogue_mode="knowledge_qa",
                    query_intent="CLARIFICATION",
                    needs_clarification=True,
                    clarification_reason="歧义设备别名 (如 001/一号机)，缺少特定型号或族名称",
                    confidence=0.85,
                    reason_code="AMBIGUOUS_ALIAS_CLARIFY",
                    context=route_ctx,
                )

            # B. 极度模糊的查看/处理类只读澄清 ("帮我看看机器人", "处理一下设备", "这个怎么样", "看一下A") -> CLARIFY
            if any(p in msg for p in ("帮我看看机器人", "处理一下设备", "这个怎么样", "看一下")) and not any(k in msg for k in ("水深", "能力", "工具", "电量", "状态")):
                return self._build_plan_result(
                    user_message=msg,
                    operation="CLARIFY",
                    dialogue_mode="knowledge_qa",
                    query_intent="CLARIFICATION",
                    needs_clarification=True,
                    clarification_reason="模糊查看/处理请求，需明确具体查询意图",
                    confidence=0.85,
                    reason_code="VAGUE_QUERY_CLARIFY",
                    context=route_ctx,
                )

            # C. 规则、概念与差异比较问答 (KNOWLEDGE_QA) -> READ
            if any(kw in msg for kw in (
                "区别", "差异", "不同", "软约束", "硬约束", "忽略警告", "忽略软警告", "硬约束阻断",
                "保存在哪", "保存位置", "存储位置", "原理", "含义", "概念", "为什么需要"
            )):
                st, stext, rel, sp = _extract_subject_relation_policy(msg, "KNOWLEDGE_QA", task_state)
                return self._build_plan_result(
                    user_message=msg,
                    operation="READ",
                    dialogue_mode="knowledge_qa",
                    query_intent="KNOWLEDGE_QA",
                    subject_type=st or "system_rule",
                    subject_text=stext,
                    relation=rel or "definition",
                    source_policy=sp or "project_kb",
                    confidence=0.85,
                    reason_code="KNOWLEDGE_CONCEPT_READ",
                    context=route_ctx,
                )

            # D. 海洋环境与海况查询 (ENVIRONMENT_QUERY) -> READ
            if any(kw in msg for kw in ("海况", "水温", "底质", "海床")):
                st, stext, rel, sp = _extract_subject_relation_policy(msg, "ENVIRONMENT_QUERY", task_state)
                return self._build_plan_result(
                    user_message=msg,
                    operation="READ",
                    dialogue_mode="knowledge_qa",
                    query_intent="ENVIRONMENT_QUERY",
                    subject_type=st or "environment",
                    subject_text=stext,
                    relation=rel or "status",
                    source_policy=sp or "project_kb",
                    confidence=0.85,
                    reason_code="ENVIRONMENT_READ",
                    context=route_ctx,
                )

            # E. 实时设备状态/遥测深度/电量查询 (DEVICE_STATUS) -> READ
            if any(kw in msg for kw in ("当前深度", "当前水深", "当前状态", "实时深度", "实时状态", "当前位置", "当前电量", "电量")):
                st, stext, rel, sp = _extract_subject_relation_policy(msg, "DEVICE_STATUS", task_state)
                return self._build_plan_result(
                    user_message=msg,
                    operation="READ",
                    dialogue_mode="knowledge_qa",
                    query_intent="DEVICE_STATUS",
                    subject_type=st or "realtime_state",
                    subject_text=stext or "实时状态",
                    relation=rel or "status",
                    source_policy="realtime_state",
                    confidence=0.85,
                    reason_code="REALTIME_DEVICE_STATUS_READ",
                    context=route_ctx,
                )

            # F. 会话任务状态/进度/缺啥参数查询 (TASK_STATUS) -> READ
            if any(kw in msg for kw in ("还缺", "缺少", "缺失", "填写了哪些", "有哪些参数", "当前任务", "进度", "步骤")):
                st, stext, rel, sp = _extract_subject_relation_policy(msg, "TASK_STATUS", task_state)
                return self._build_plan_result(
                    user_message=msg,
                    operation="READ",
                    dialogue_mode="knowledge_qa",
                    query_intent="TASK_STATUS",
                    subject_type="task",
                    subject_text="当前任务",
                    relation=rel or "missing_fields",
                    source_policy="session_state",
                    confidence=0.85,
                    reason_code="TASK_STATUS_READ",
                    context=route_ctx,
                )

            # G. 特定型号/族机器人的能力与限制查询 (DEVICE_CAPABILITY) -> READ
            has_specific_family_mention = any(
                fam in msg for fam in ("天鹰座", "金牛座", "水蛟", "海马", "CRAWLER", "LROV", "1600", "WORK", "观察级", "工作级", "亚特兰蒂斯", "深海")
            )
            asks_which_robots = any(
                kw in msg for kw in (
                    "哪些机器人", "哪些设备可以", "哪些潜水器", "哪些rov", "哪些auv", "什么机器人", "能够在", "能在"
                )
            )

            if has_specific_family_mention or asks_which_robots or any(
                kw in msg for kw in ("属于哪个", "能执行什么", "class", "family", "族", "能力和限制", "限制是什么", "作业吗", "水深是", "最大水深")
            ):
                st, stext, rel, sp = _extract_subject_relation_policy(msg, "DEVICE_CAPABILITY", task_state)
                return self._build_plan_result(
                    user_message=msg,
                    operation="READ",
                    dialogue_mode="knowledge_qa",
                    query_intent="DEVICE_CAPABILITY",
                    subject_type=st or "device",
                    subject_text=stext,
                    relation=rel or "capabilities",
                    source_policy=sp or "project_kb",
                    confidence=0.85,
                    reason_code="DEVICE_CAPABILITY_READ",
                    context=route_ctx,
                )

            # H. 工具/载荷查询 (TOOL_QUERY) -> READ
            is_tool_focused = any(
                kw in msg.lower()
                for kw in (
                    "payload", "负载", "载荷", "工具", "抓手", "传感器", "机械臂", "电液机械臂",
                    "摄像机", "声呐", "声纳", "配", "搭载的设备", "支持的设备", "可用工具"
                )
            )
            if is_tool_focused:
                st, stext, rel, sp = _extract_subject_relation_policy(msg, "TOOL_QUERY", task_state)
                return self._build_plan_result(
                    user_message=msg,
                    operation="READ",
                    dialogue_mode="knowledge_qa",
                    query_intent="TOOL_QUERY",
                    subject_type=st or "payload",
                    subject_text=stext,
                    relation=rel or "supports",
                    source_policy=sp or "project_kb",
                    confidence=0.85,
                    reason_code="TOOL_QUERY_READ",
                    context=route_ctx,
                )

            # I. 宽泛设备列表或水深能力查询 (DEVICE_CAPABILITY) -> READ
            if is_broad_device_query or any(
                kw in msg for kw in ("水深", "深度", "作业模式", "能力", "不能在", "支持哪些", "适合作业", "目前有哪些机器人", "机器人有哪些")
            ):
                st, stext, rel, sp = _extract_subject_relation_policy(msg, "DEVICE_CAPABILITY", task_state)
                return self._build_plan_result(
                    user_message=msg,
                    operation="READ",
                    dialogue_mode="knowledge_qa",
                    query_intent="DEVICE_CAPABILITY",
                    subject_type=st or "device_class",
                    subject_text=stext,
                    relation=rel or "capabilities",
                    source_policy=sp or "project_kb",
                    confidence=0.85,
                    reason_code="BROAD_DEVICE_READ",
                    context=route_ctx,
                )

            # J. 规则/流程/通用概念知识问答 (KNOWLEDGE_QA) -> READ
            st, stext, rel, sp = _extract_subject_relation_policy(msg, "KNOWLEDGE_QA", task_state)
            return self._build_plan_result(
                user_message=msg,
                operation="READ",
                dialogue_mode="knowledge_qa",
                query_intent="KNOWLEDGE_QA",
                subject_type=st or "general_concept",
                subject_text=stext,
                relation=rel or "describe",
                source_policy=sp or "project_kb",
                confidence=0.85,
                reason_code="GENERAL_KNOWLEDGE_READ",
                context=route_ctx,
            )

        if any(
            kw in msg
            for kw in (
                "当前任务",
                "有哪些参数",
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
            if is_query_sentence or "哪些" in msg or "状态" in msg:
                return self._build_plan_result(
                    user_message=msg,
                    operation="READ",
                    dialogue_mode="knowledge_qa",
                    query_intent="TASK_STATUS",
                    subject_type="task",
                    subject_text="当前任务",
                    relation="missing_fields",
                    source_policy="session_state",
                    confidence=0.85,
                    reason_code="TASK_STATUS_READ",
                    context=route_ctx,
                )

        if any(
            kw in msg
            for kw in ("需要哪些", "包含哪些", "包含什么", "模板", "知识", "定义", "规则", "需要什么")
        ):
            st, stext, rel, sp = _extract_subject_relation_policy(msg, "KNOWLEDGE_QA", task_state)
            return self._build_plan_result(
                user_message=msg,
                operation="READ",
                dialogue_mode="knowledge_qa",
                query_intent="KNOWLEDGE_QA",
                subject_type=st or "system_rule",
                subject_text=stext,
                relation=rel or "list",
                source_policy=sp or "project_kb",
                confidence=0.85,
                reason_code="SYSTEM_RULE_READ",
                context=route_ctx,
            )

        # 8. 任务新建与参数/设备更新 (问句且无显式赋值动词时不误判为 WRITE)
        if not is_query_sentence or re.search(
            r"(?:改成|设为|设置为|替换为|调整为|切换为|为\s*[0-9]+)", msg
        ):
            if bool(
                re.search(
                    r"(?:[0-9]+|创建|新建|发起|改成|设为|设置为|替换为|调整为|切换为|使用|搭载|配备|携带|换成|指定|为\s*[0-9]+|为\s*[A-Za-z0-9_]+)",
                    msg,
                )
            ):
                st, stext, rel, sp = _extract_subject_relation_policy(msg, None, task_state)
                return self._build_plan_result(
                    user_message=msg,
                    operation="WRITE",
                    dialogue_mode="task_collection",
                    subject_type=st or "task",
                    subject_text=stext,
                    relation=rel or "filled_fields",
                    source_policy=sp or "session_state",
                    confidence=0.85,
                    reason_code="TASK_UPDATE_WRITE",
                    context=route_ctx,
                )

        if any(
            kw in msg for kw in ("进度", "缺", "缺少", "状态", "已有", "步骤", "进行到")
        ):
            return self._build_plan_result(
                user_message=msg,
                operation="READ",
                dialogue_mode="knowledge_qa",
                query_intent="TASK_STATUS",
                subject_type="task",
                subject_text="当前任务",
                relation="missing_fields",
                source_policy="session_state",
                confidence=0.85,
                reason_code="TASK_STATUS_READ",
                context=route_ctx,
            )
        if any(kw in msg for kw in ("海况", "水温", "底质", "海床")):
            st, stext, rel, sp = _extract_subject_relation_policy(msg, "ENVIRONMENT_QUERY", task_state)
            return self._build_plan_result(
                user_message=msg,
                operation="READ",
                dialogue_mode="knowledge_qa",
                query_intent="ENVIRONMENT_QUERY",
                subject_type=st or "environment",
                subject_text=stext,
                relation=rel or "status",
                source_policy=sp or "project_kb",
                confidence=0.85,
                reason_code="ENVIRONMENT_READ",
                context=route_ctx,
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
            return self._build_plan_result(
                user_message=msg,
                operation="READ",
                dialogue_mode="knowledge_qa",
                query_intent="GENERAL_CHAT",
                subject_type="general_concept",
                subject_text=None,
                relation="describe",
                source_policy="general_domain",
                confidence=0.85,
                reason_code="GENERAL_CHAT_READ",
                context=route_ctx,
            )

        return None

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

        # 优先通过确定性安全规则，避免确定性输入依赖 LLM
        rule_res = self._rule_deterministic_route(
            user_message=msg,
            conversation_history=conversation_history,
            task_state=task_state,
            phase=phase,
            expected_slots=expected_slots,
        )
        if rule_res is not None:
            return rule_res

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
            fallback_plan = build_clarify_fallback_plan(
                reason=f"规则兜底澄清: {e}",
                reason_code="LLM_ROUTE_FAIL_FALLBACK",
                confidence=0.5,
            )
            return fallback_plan.to_intent_route_result()

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
            "task_state": task_state,
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
                try:
                    parsed = self.llm.extract_json(messages, max_tokens=320, role=ModelRole.ROUTER)
                except TypeError as exc:
                    if not _is_unsupported_role_keyword_error(exc):
                        raise
                    parsed = self.llm.extract_json(messages, max_tokens=320)
            elif hasattr(self.llm, "classify_interaction"):
                try:
                    try:
                        res = self.llm.classify_interaction(messages, max_tokens=320, role=ModelRole.ROUTER)
                    except TypeError as exc:
                        if not _is_unsupported_role_keyword_error(exc):
                            raise
                        res = self.llm.classify_interaction(messages, max_tokens=320)
                    if isinstance(res, dict):
                        parsed = res
                except IntentRoutingError:
                    raise
                except Exception:
                    pass
            if parsed is None and hasattr(self.llm, "extract_json"):
                try:
                    parsed = self.llm.extract_json(messages, max_tokens=320, role=ModelRole.ROUTER)
                except TypeError as exc:
                    if not _is_unsupported_role_keyword_error(exc):
                        raise
                    parsed = self.llm.extract_json(messages, max_tokens=320)
        except Exception as exc:
            logger.warning("[IntentRouter] LLM call failed: %s", exc)
            raise IntentRoutingError(f"LLM 调用失败: {exc}") from exc

        if not isinstance(parsed, dict):
            logger.warning("[IntentRouter] LLM 未返回合法 JSON object: %r", parsed)
            raise IntentRoutingError("LLM 路由结果不是合法 JSON object")

        # 检查 query_subtype / query_intent 的数据类型合法性（针对非 string 类型如 list/dict/number/bool）
        raw_subtype = parsed.get("query_subtype")
        if raw_subtype is not None and (isinstance(raw_subtype, bool) or not isinstance(raw_subtype, str)):
            parsed["operation"] = "CLARIFY"
            parsed["dialogue_mode"] = "knowledge_qa"
            parsed["query_intent"] = "CLARIFICATION"
            parsed["needs_clarification"] = True
            parsed["clarification_reason"] = f"非法 query_subtype 类型: {type(raw_subtype)}"

        raw_qintent = parsed.get("query_intent")
        if raw_qintent is not None and (isinstance(raw_qintent, bool) or not isinstance(raw_qintent, str)):
            parsed["operation"] = "CLARIFY"
            parsed["dialogue_mode"] = "knowledge_qa"
            parsed["query_intent"] = "CLARIFICATION"
            parsed["needs_clarification"] = True
            parsed["clarification_reason"] = f"非法 query_intent 类型: {type(raw_qintent)}"

        # 检查非法/未识别的 intent
        raw_intent = parsed.get("intent")
        if raw_intent and str(raw_intent).strip().upper() not in (
            "TASK_CREATE", "TASK_UPDATE", "CREATE", "UPDATE", "WRITE", "QUERY", "SEARCH", "INFO", "EMERGENCY",
            "TASK_STATUS", "TOOL_QUERY", "DEVICE_CAPABILITY", "DEVICE_STATUS", "ENVIRONMENT_QUERY", "KNOWLEDGE_QA", "GENERAL_CHAT", "CLARIFICATION"
        ):
            parsed["operation"] = "CLARIFY"
            parsed["dialogue_mode"] = "knowledge_qa"
            parsed["query_intent"] = "CLARIFICATION"
            parsed["needs_clarification"] = True
            parsed["clarification_reason"] = f"未知的意图分类: {raw_intent}"

        # 双重提取安全校验: 针对 LLM 返回逻辑为紧急介入的分支，强行进行文本确定性可执行动作校验
        raw_dm = parsed.get("dialogue_mode")
        raw_op = parsed.get("operation")
        if raw_dm == "emergency_intervention" or raw_op == "CONTROL":
            validated_action = self._parse_executable_control_action(user_message)
            if validated_action is None:
                parsed["operation"] = "CLARIFY"
                parsed["dialogue_mode"] = "knowledge_qa"
                parsed["query_intent"] = "CLARIFICATION"
                parsed["emergency_action"] = None
                parsed["needs_clarification"] = True
                parsed["clarification_reason"] = "LLM 识别为紧急控制，但用户文本缺少确定性可执行紧急命令，安全降级澄清"
            else:
                parsed["operation"] = "CONTROL"
                parsed["dialogue_mode"] = "emergency_intervention"
                parsed["emergency_action"] = validated_action

        # 适配兼容遗留 LLM 字段向 InteractionPlan 补全
        if "operation" not in parsed:
            raw_it = parsed.get("interaction_type") or parsed.get("intent")
            raw_it_str = str(raw_it or "").strip().upper()
            if raw_dm == "task_collection" or raw_it_str in ("WRITE", "TASK_CREATE", "TASK_UPDATE", "CREATE", "UPDATE"):
                parsed["operation"] = "WRITE"
                parsed["dialogue_mode"] = "task_collection"
            elif raw_dm == "emergency_intervention" or raw_it_str in ("EMERGENCY", "EMERGENCY_INTERVENTION"):
                parsed["operation"] = "CONTROL"
                parsed["dialogue_mode"] = "emergency_intervention"
            elif parsed.get("query_intent") == "CLARIFICATION" or parsed.get("needs_clarification") or raw_it_str == "CLARIFICATION":
                parsed["operation"] = "CLARIFY"
                parsed["dialogue_mode"] = "knowledge_qa"
            else:
                parsed["operation"] = "READ"
                parsed["dialogue_mode"] = "knowledge_qa"

        if "source_policy" not in parsed:
            st, stext, rel, sp = _extract_subject_relation_policy(user_message, parsed.get("query_intent"), task_state)
            parsed.setdefault("subject_type", st)
            parsed.setdefault("subject_text", stext)
            parsed.setdefault("relation", rel)
            parsed.setdefault("source_policy", sp)

        # 通过后端确定性校验器 validate_interaction_plan 强制约束，非合法输出一律安全降级 CLARIFY
        validated_plan = validate_interaction_plan(parsed, user_message=user_message, context=context)
        return validated_plan.to_intent_route_result()
