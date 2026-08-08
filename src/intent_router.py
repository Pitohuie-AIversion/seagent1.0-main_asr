"""
src/intent_router.py - 结构化交互路由器

IntentRouter 将用户输入路由至三种对话模式之一：
- task_collection：任务数据收集与参数修改
- knowledge_qa：知识问答、能力/状态查询与普通聊天（含意图澄清 CLARIFICATION）
- emergency_intervention：紧急控制干预（需二次确定性验证）
"""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass
from typing import Any, Literal

from .llm_client import LLMClient
from .model_profile import ModelRole

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
你负责判断用户本轮输入的对话模式 (dialogue_mode)。

【对话模式类型】

1. task_collection:
用户正在创建任务、提交参数、补充信息、选择机器人/工具/位置/时间，
或回答系统追问的 expected_slots，或否定控制动作后继续提交参数。

2. knowledge_qa:
用户正在索取信息、查询设备能力/工具/状态、查询环境/任务规则、
进行普通聊天，或对控制操作进行解释性询问（如"如何取消任务"、"如果停止任务会怎样"），
或在表达模糊、包含歧义裸词或否定控制指令时进行意图澄清 (query_intent: CLARIFICATION)。
知识问答严禁修改任务状态。

3. emergency_intervention:
用户发出明确的紧急控制动作命令，要求暂停、停止、终止或取消当前任务。

【输出要求】

只能输出严格 JSON，不得输出其他文字。

task_collection 示例：
{
  "dialogue_mode": "task_collection",
  "interaction_type": "WRITE",
  "query_intent": null,
  "confidence": 0.97,
  "reason": "用户提交了准备写入任务状态的水深参数"
}

knowledge_qa 示例：
{
  "dialogue_mode": "knowledge_qa",
  "interaction_type": "QUERY",
  "query_intent": "DEVICE_CAPABILITY",
  "confidence": 0.96,
  "reason": "用户正在询问设备的最大作业水深"
}

emergency_intervention 示例：
{
  "dialogue_mode": "emergency_intervention",
  "interaction_type": "QUERY",
  "query_intent": null,
  "confidence": 0.99,
  "emergency_action": "stop",
  "reason": "用户要求立即停止当前任务"
}
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
        return {
            "interaction_type": self.interaction_type,
            "confidence": self.confidence,
            "reason": self.reason,
            "query_intent": self.query_intent,
            "dialogue_mode": self.dialogue_mode,
            "source": self.source,
            "emergency_action": self.emergency_action,
        }

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


class IntentRouter:
    def __init__(self, llm: LLMClient):
        self.llm = llm

    @staticmethod
    def _parse_executable_control_action(user_message: str) -> str | None:
        """从用户消息中提取明确可执行的紧急控制动作。"""
        # 1. 结合标点符号及前置控制动词后的疑问/条件句与转折/连词界限切分子句
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
            # 2. 检查子句是否为作用于非任务对象的控制句（如“停止回答”、“停止任务打印”）
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

            # 3. 检查子句级别的否定修饰
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

            # 4. 检查子句自身是否为疑问语气或条件表达
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

            # 5. 检查明确控制对象或紧急提示词
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
                # 若控制动作作用于特定槽位/字段修改（如"取消支持船修改"），不误判为全局紧急干预
                if any(k in clause for k in ("修改", "更新", "槽位", "填写的")):
                    return None
                return action_found

        return None


    def _rule_deterministic_route(
        self,
        user_message: str,
        conversation_history: list[dict],
        task_state: dict,
        phase: str,
    ) -> IntentRouteResult | None:
        """优先执行的确定性安全规则。"""
        msg = user_message.strip()

        # 1. 优先提取明确的紧急干预动作（紧急控制最高优先级）
        has_control_word = any(
            kw in msg for kw in ("停止", "暂停", "取消", "终止", "撤销", "放弃")
        )
        if has_control_word:
            action = self._parse_executable_control_action(msg)
            if action is not None:
                return IntentRouteResult(
                    interaction_type="QUERY",
                    dialogue_mode="emergency_intervention",
                    emergency_action=action,
                    confidence=0.99,
                    reason=f"规则确定性路由: 紧急介入动作【{action}】",
                    source="rule",
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
            return IntentRouteResult(
                interaction_type="QUERY",
                confidence=0.9,
                reason="规则拦截: 非任务对象控制",
                query_intent="KNOWLEDGE_QA",
                dialogue_mode="knowledge_qa",
                source="rule",
            )

        # 3. 含有控制词但整体为疑问/条件且未匹配独立紧急控制动作的否决分支
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
            return IntentRouteResult(
                dialogue_mode="knowledge_qa",
                interaction_type="QUERY",
                confidence=0.9,
                reason="规则拦截: 含有控制词的疑问/条件表达",
                query_intent="KNOWLEDGE_QA",
            )

        # 3. 否定控制动作 (若同时提交显式参数修改如'改成500米'，优先走 task_collection)
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
                return IntentRouteResult(
                    dialogue_mode="task_collection",
                    interaction_type="WRITE",
                    confidence=0.95,
                    reason="规则拦截: 否决控制动作并提交任务参数",
                    query_intent=None,
                )
            return IntentRouteResult(
                dialogue_mode="knowledge_qa",
                interaction_type="QUERY",
                confidence=0.85,
                reason="规则拦截: 否决控制动作",
                query_intent="CLARIFICATION",
            )

        # 4. 确定性紧急动作与裸词处理
        if has_control_word:
            action = self._parse_executable_control_action(msg)
            if action is not None:
                return IntentRouteResult(
                    dialogue_mode="emergency_intervention",
                    interaction_type="QUERY",
                    confidence=0.95,
                    reason=f"规则识别: 紧急控制动作 ({action})",
                    query_intent=None,
                    emergency_action=action,
                )
            if msg in ("停止", "暂停", "取消", "终止", "撤销", "放弃"):
                return IntentRouteResult(
                    dialogue_mode="knowledge_qa",
                    interaction_type="QUERY",
                    confidence=0.85,
                    reason="规则拦截: 模糊裸词控制动作",
                    query_intent="CLARIFICATION",
                )

        # 5. 确认/发布指令
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
                )

        # 0. 纯标点符号/非词汇输入拦截
        if not re.search(r"[\u4e00-\u9fa5a-zA-Z0-9]", msg.strip()):
            return IntentRouteResult(
                interaction_type="QUERY",
                confidence=0.85,
                reason="规则拦截: 纯标点符号/非词汇输入",
                query_intent="CLARIFICATION",
                dialogue_mode="knowledge_qa",
            )

        # 6. 只读查询与意图分流
        msg_for_query_check = msg.replace("支持船", "")
        is_query_sentence = is_question or any(
            kw in msg_for_query_check for kw in (
                "哪些", "如何", "为什么", "是否", "能否", "有没有", "怎么", "什么", "几", "多少",
                "支持", "适合", "区别", "作用", "介绍", "说明", "解释", "属于", "干什么", "保存在哪", "在哪", "怎么样"
            )
        )


        # 显式任务写入动词
        has_write_verb = any(
            kw in msg for kw in (
                "创建", "新建", "发起", "改成", "设为", "设置为",
                "替换为", "调整为", "切换为", "换成", "去检查", "去巡检", "去操作", "去埋设", "让", "取消", "撤销"
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

        # 6.1 显式写入动作直接进入 task_collection
        if has_explicit_write_action:
            return IntentRouteResult(
                dialogue_mode="task_collection",
                interaction_type="WRITE",
                confidence=0.95,
                reason="规则识别: 显式任务写入或参数/设备修改指令",
                query_intent=None,
            )

        # 6.2 裸领域名词只读处理
        if is_bare_domain_noun:
            if msg_clean in ("payload", "负载", "载荷", "工具", "抓手", "传感器", "机械臂", "摄像机", "声呐", "声纳"):
                return IntentRouteResult(
                    interaction_type="QUERY",
                    confidence=0.9,
                    reason="规则拦截: 裸领域名词只读工具查询",
                    query_intent="TOOL_QUERY",
                    dialogue_mode="knowledge_qa",
                )
            elif msg_clean in ("设备", "机器人"):
                return IntentRouteResult(
                    interaction_type="QUERY",
                    confidence=0.9,
                    reason="规则拦截: 裸领域名词只读设备查询",
                    query_intent="DEVICE_CAPABILITY",
                    dialogue_mode="knowledge_qa",
                )
            else:
                return IntentRouteResult(
                    interaction_type="QUERY",
                    confidence=0.9,
                    reason="规则拦截: 裸领域名词只读知识查询",
                    query_intent="KNOWLEDGE_QA",
                    dialogue_mode="knowledge_qa",
                )

        # 6.3 广义只读查询意图分流
        is_broad_device_query = (
            any(verb in msg for verb in ("查询", "查看", "列出", "检索", "显示", "获取", "了解", "介绍", "说明", "解释"))
            and any(noun in msg for noun in ("设备", "机器人", "潜水器", "ROV", "AUV", "HOV", "负载", "载荷", "工具", "抓手", "传感器"))
        )

        if is_query_sentence or is_broad_device_query or is_meta_workflow_query:
            # A. 歧义设备别名拦截 (如 "一号机" / "001" 未带具体系列名)
            is_device_context = any(dev in msg for dev in ("机", "设备", "水深", "深度", "作业", "下潜", "机器人", "能力", "搭载", "模式"))
            is_ambiguous_alias = ("一号机" in msg or "二号机" in msg or ("001" in msg and is_device_context))
            has_family = any(fam in msg for fam in ("天鹰座", "金牛座", "水蛟", "海马", "CRAWLER", "LROV", "1600", "WORK"))
            if is_ambiguous_alias and not has_family:
                reason = (
                    "规则拦截: 歧义设备别名 (001/一号机)"
                    if ("001" in msg or "一号机" in msg)
                    else "规则拦截: 歧义设备别名"
                )
                return IntentRouteResult(
                    interaction_type="QUERY",
                    confidence=0.85,
                    reason=reason,
                    query_intent="CLARIFICATION",
                    dialogue_mode="knowledge_qa",
                )

            # B. 极度模糊的查看/处理类只读澄清 ("帮我看看机器人", "处理一下设备", "看一下A")
            if any(p in msg for p in ("帮我看看机器人", "处理一下设备", "这个怎么样", "看一下")) and not any(k in msg for k in ("水深", "能力", "工具", "电量", "状态")):
                return IntentRouteResult(
                    interaction_type="QUERY",
                    confidence=0.85,
                    reason="规则拦截: 模糊只读意图澄清",
                    query_intent="CLARIFICATION",
                    dialogue_mode="knowledge_qa",
                )

            # C. 规则、概念与差异比较问答 (KNOWLEDGE_QA)
            # 如 "AUV 和 ROV 有什么区别？", "什么是软约束？", "如何忽略软警告？", "任务发布后保存在哪里？"
            if any(kw in msg for kw in (
                "区别", "差异", "不同", "软约束", "硬约束", "忽略警告", "忽略软警告", "硬约束阻断",
                "保存在哪", "保存位置", "存储位置", "原理", "含义", "概念", "为什么需要"
            )):
                return IntentRouteResult(
                    interaction_type="QUERY",
                    confidence=0.85,
                    reason="规则兜底: 业务规则与概念知识查询",
                    query_intent="KNOWLEDGE_QA",
                    dialogue_mode="knowledge_qa",
                )

            # D. 海洋环境与海况查询 (ENVIRONMENT_QUERY)
            if any(kw in msg for kw in ("海况", "水温", "底质", "海床")):
                return IntentRouteResult(
                    interaction_type="QUERY",
                    confidence=0.85,
                    reason="规则兜底: 环境海况查询",
                    query_intent="ENVIRONMENT_QUERY",
                    dialogue_mode="knowledge_qa",
                )

            # E. 实时设备状态/遥测深度/电量查询 (DEVICE_STATUS)
            if any(kw in msg for kw in ("当前深度", "当前水深", "当前状态", "实时深度", "实时状态", "当前位置", "当前电量", "电量")):
                return IntentRouteResult(
                    interaction_type="QUERY",
                    confidence=0.85,
                    reason="规则兜底: 实时设备状态查询",
                    query_intent="DEVICE_STATUS",
                    dialogue_mode="knowledge_qa",
                )

            # F. 会话任务状态/进度/缺啥参数查询 (TASK_STATUS)
            if any(kw in msg for kw in ("还缺", "缺少", "缺失", "填写了哪些", "有哪些参数", "当前任务", "进度", "步骤")):
                return IntentRouteResult(
                    interaction_type="QUERY",
                    confidence=0.85,
                    reason="规则兜底: 任务状态查询",
                    query_intent="TASK_STATUS",
                    dialogue_mode="knowledge_qa",
                )

            # G. 特定型号/族机器人的能力与限制查询 (DEVICE_CAPABILITY)
            # 若包含具体型号/族名称 (如 "金牛座"、"亚特兰蒂斯"、"观察级ROV") 或明确询问机器人列表 ("哪些机器人可以搭载机械臂", "能在1000米作业吗")
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
                return IntentRouteResult(
                    interaction_type="QUERY",
                    confidence=0.85,
                    reason="规则兜底: 特定设备能力与关系查询",
                    query_intent="DEVICE_CAPABILITY",
                    dialogue_mode="knowledge_qa",
                )


            # H. 工具/载荷查询 (TOOL_QUERY)
            # 如果问句关注载荷/工具/设备 ("机器人的负载有哪些", "机器人支持的设备有哪些", "侧扫声呐有什么作用")
            is_tool_focused = any(
                kw in msg.lower()
                for kw in (
                    "payload", "负载", "载荷", "工具", "抓手", "传感器", "机械臂", "电液机械臂",
                    "摄像机", "声呐", "声纳", "配", "搭载的设备", "支持的设备", "可用工具"
                )
            )
            if is_tool_focused:
                return IntentRouteResult(
                    interaction_type="QUERY",
                    confidence=0.85,
                    reason="规则兜底: 工具/载荷查询",
                    query_intent="TOOL_QUERY",
                    dialogue_mode="knowledge_qa",
                )

            # I. 宽泛设备列表或水深能力查询 (DEVICE_CAPABILITY)
            if is_broad_device_query or any(
                kw in msg for kw in ("水深", "深度", "作业模式", "能力", "不能在", "支持哪些", "适合作业", "目前有哪些机器人", "机器人有哪些")
            ):
                return IntentRouteResult(
                    interaction_type="QUERY",
                    confidence=0.85,
                    reason="规则兜底: 广义设备能力查询",
                    query_intent="DEVICE_CAPABILITY",
                    dialogue_mode="knowledge_qa",
                )

            # J. 规则/流程/通用概念知识问答 (KNOWLEDGE_QA)
            return IntentRouteResult(
                interaction_type="QUERY",
                confidence=0.85,
                reason="规则兜底: 业务规则与概念知识查询",
                query_intent="KNOWLEDGE_QA",
                dialogue_mode="knowledge_qa",
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
                return IntentRouteResult(
                    interaction_type="QUERY",
                    confidence=0.85,
                    reason="规则兜底: 任务状态查询",
                    query_intent="TASK_STATUS",
                    dialogue_mode="knowledge_qa",
                )

        if any(
            kw in msg
            for kw in ("需要哪些", "包含哪些", "包含什么", "模板", "知识", "定义", "规则", "需要什么")
        ):
            return IntentRouteResult(
                interaction_type="QUERY",
                confidence=0.85,
                reason="规则兜底: 业务知识查询",
                query_intent="KNOWLEDGE_QA",
                dialogue_mode="knowledge_qa",
            )

        # 7. 任务新建与参数/设备更新 (问句且无显式赋值动词时不误判为 WRITE)
        if not is_query_sentence or re.search(
            r"(?:改成|设为|设置为|替换为|调整为|切换为|为\s*[0-9]+)", msg
        ):
            if bool(
                re.search(
                    r"(?:[0-9]+|创建|新建|发起|改成|设为|设置为|替换为|调整为|切换为|使用|搭载|配备|携带|换成|指定|为\s*[0-9]+|为\s*[A-Za-z0-9_]+)",
                    msg,
                )
            ):
                return IntentRouteResult(
                    interaction_type="WRITE",
                    confidence=0.85,
                    reason="规则兜底: 包含任务新建或参数/设备更新",
                    query_intent=None,
                    dialogue_mode="task_collection",
                )

        if any(
            kw in msg for kw in ("进度", "缺", "缺少", "状态", "已有", "步骤", "进行到")
        ):
            return IntentRouteResult(
                dialogue_mode="knowledge_qa",
                interaction_type="QUERY",
                confidence=0.85,
                reason="规则兜底: 任务状态查询",
                query_intent="TASK_STATUS",
            )
        if any(kw in msg for kw in ("海况", "水温", "底质", "海床")):
            return IntentRouteResult(
                dialogue_mode="knowledge_qa",
                interaction_type="QUERY",
                confidence=0.85,
                reason="规则兜底: 环境查询",
                query_intent="ENVIRONMENT_QUERY",
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
            return IntentRouteResult(
                interaction_type="QUERY",
                confidence=0.5,
                reason="规则兜底: 澄清意图",
                query_intent="CLARIFICATION",
                dialogue_mode="knowledge_qa",
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

        parsed = None
        try:
            ej_attr = getattr(self.llm, "extract_json", None)
            if ej_attr is not None and hasattr(ej_attr, "called"):
                try:
                    parsed = self.llm.extract_json(messages, max_tokens=260, role=ModelRole.ROUTER)
                except TypeError:
                    parsed = self.llm.extract_json(messages, max_tokens=260)
            elif hasattr(self.llm, "classify_interaction"):
                try:
                    try:
                        res = self.llm.classify_interaction(messages, max_tokens=260, role=ModelRole.ROUTER)
                    except TypeError:
                        res = self.llm.classify_interaction(messages, max_tokens=260)
                    if isinstance(res, dict):
                        parsed = res
                except Exception:
                    pass
            if parsed is None and hasattr(self.llm, "extract_json"):
                try:
                    parsed = self.llm.extract_json(messages, max_tokens=260, role=ModelRole.ROUTER)
                except TypeError:
                    parsed = self.llm.extract_json(messages, max_tokens=260)
        except Exception as exc:
            logger.warning("[IntentRouter] LLM call failed: %s", exc)
            raise IntentRoutingError(f"LLM 调用失败: {exc}") from exc

        if not isinstance(parsed, dict):
            logger.warning("[IntentRouter] LLM 未返回合法 JSON object: %r", parsed)
            raise IntentRoutingError("LLM 路由结果不是合法 JSON object")

        _has_slot_candidates = bool(parsed.get("slot_candidates"))

        is_unknown_or_bogus = False
        raw_mode = parsed.get("dialogue_mode")
        if not raw_mode:
            raw_it = parsed.get("interaction_type") or parsed.get("intent")
            it_str = str(raw_it or "").strip().upper()
            if (
                it_str in ("WRITE", "TASK_CREATE", "TASK_UPDATE", "CREATE", "UPDATE")
                or _has_slot_candidates
            ):
                raw_mode = "task_collection"
            elif it_str in ("QUERY", "SEARCH", "INFO"):
                raw_mode = "knowledge_qa"
            elif it_str == "EMERGENCY":
                raw_mode = "emergency_intervention"
            else:
                raw_mode = "knowledge_qa"
                is_unknown_or_bogus = True

        dialogue_mode = str(raw_mode).strip().lower()
        if dialogue_mode == "uncertain":
            dialogue_mode = "knowledge_qa"
            is_unknown_or_bogus = True
        if dialogue_mode not in VALID_DIALOGUE_MODES:
            logger.warning("[IntentRouter] 非法 dialogue_mode: %r", dialogue_mode)
            raise IntentRoutingError("LLM 返回 dialogue_mode 非法")

        if "confidence" not in parsed or parsed["confidence"] is None:
            if dialogue_mode == "task_collection" and _has_slot_candidates:
                parsed["confidence"] = 0.9
            elif is_unknown_or_bogus or parsed.get("intent") == "UNKNOWN":
                parsed["confidence"] = 0.85
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
            reason = "意图识别"

        if confidence_float < 0.6:
            raise IntentRoutingError(
                f"LLM 识别置信度过低({confidence_float:.2f}): {reason.strip()}"
            )

        raw_query_intent = parsed.get("query_intent") or parsed.get("query_subtype")
        query_intent = (
            str(raw_query_intent).strip().upper() if raw_query_intent else None
        )

        if dialogue_mode == "knowledge_qa" and (
            not query_intent or query_intent == "UNKNOWN" or is_unknown_or_bogus
        ):
            if is_unknown_or_bogus:
                query_intent = "CLARIFICATION"
            else:
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

        emergency_action = None
        if dialogue_mode == "emergency_intervention":
            validated_action = self._parse_executable_control_action(user_message)
            if validated_action is None:
                return IntentRouteResult(
                    dialogue_mode="knowledge_qa",
                    interaction_type="QUERY",
                    confidence=confidence_float,
                    reason="LLM 识别为紧急干预，但缺乏确定性可执行紧急命令依据，安全降级为澄清",
                    query_intent="CLARIFICATION",
                    source="llm",
                    emergency_action=None,
                )
            emergency_action = validated_action

        return IntentRouteResult(
            dialogue_mode=dialogue_mode,
            interaction_type="WRITE" if dialogue_mode == "task_collection" else "QUERY",
            confidence=confidence_float,
            reason=reason.strip(),
            query_intent=query_intent if dialogue_mode == "knowledge_qa" else None,
            source="llm",
            emergency_action=emergency_action,
        )
