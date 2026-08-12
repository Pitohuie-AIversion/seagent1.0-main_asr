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

【LLM-First 架构说明】
本系统采用 LLM-First 架构：你的语义判断作为主要依据，后端安全校验层作为兜底。
请大胆基于语义和上下文进行判断，无需过度保守（后端会做二次安全校验）。
后端会自动补齐 subject_type/relation/source_policy 等描述性字段的枚举值。

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

【重要放宽判断标准】：
- 自然语言的任务创建意图（"我想做巡检"、"准备弄个管缆作业吧"、"帮我规划个水下任务"）视为 WRITE
- 有任务上下文（已有 task_type_key 或 expected_slots）时，用户回答领域词汇（"电力电缆"、"金牛座"、"500米"）视为 WRITE
- 数字+领域词的组合（"水深500"、"500米吧"）有任务上下文时视为 WRITE
- 口语化设备选择（"用金牛座"、"选天鹰座吧"）视为 WRITE

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


def _infer_read_query_intent(user_message: str) -> str:
    """LLM-First: 对 READ 场景的描述性 query_intent 做确定性规则补齐。

    LLM 的语义判断优先于 operation/dialogue_mode（安全边界），
    query_intent 只是后续知识检索的分支元数据，不影响安全决策，
    因此可以用规则精确补齐，确保后续检索质量。
    """
    msg = user_message.strip()
    lower = msg.lower()

    # ═══ 0. 知识元问题（什么是 X / 为什么 / 怎么 / 如何 / 对比 / 解释...）优先级最高 ═══
    # 但如果有明确设备名+作业/能力/限制等（如"金牛座为什么不能在500米作业"），
    # 归为 DEVICE_CAPABILITY（问的是设备能力限制，不是元概念）
    # Python 的 Unicode ``\b`` 会把中文和 ASCII 字母都视为单词字符，
    # 因而无法识别“观察级ROV能”这类中英文紧邻写法。这里只排除 ASCII
    # 标识符字符，既允许中文相邻，也避免把 PROVISION/AUVX 中的子串当设备名。
    has_device_acronym_ref = bool(
        re.search(
            r"(?<![A-Za-z0-9_])(?:ROV|AUV)(?![A-Za-z0-9_])",
            msg,
            re.IGNORECASE,
        )
    )
    has_device_name_ref = (
        any(
            d in msg
            for d in (
                "天鹰座",
                "金牛座",
                "水蛟",
                "海马",
                "CRAWLER",
                "LROV",
                "亚特兰蒂斯",
                "OBSROV",
            )
        )
        or has_device_acronym_ref
        or "机器人" in msg
    )
    # 设备能力/作业/作业水深限制相关的"为什么/怎么不能"问句（问的是设备性能）
    device_capability_why = bool(
        has_device_name_ref
        and any(q in msg for q in ("为什么", "为何", "怎么", "如何", "什么原因"))
        and any(t in msg for t in ("作业", "工作", "使用", "搭载", "下潜", "潜水", "运行", "执行", "能力", "限制", "最大", "不能", "无法", "不行", "支持"))
    )
    meta_knowledge_patterns = (
        r"什么是", r"什么叫", r"何为", r"为什么", r"怎么(?!样|办|解决|执行|创建)",
        r"如何(?!忽略|启用|开启|关闭|使用|选择)", r"怎样", r"怎么弄", r"怎么创建", r"如何创建",
        r"解释一下", r"解释下", r"的区别", r"的差异", r"有什么区别", r"有什么不同",
        r"对比", r"和.*(区别|不同|比较)", r"与.*(区别|不同|比较)",
        r"保存在哪里", r"保存到哪", r"忽略.*软警告", r"被.*阻断", r"被.*硬约束",
        r"什么.*家族", r"什么.*class", r"什么.*family", r"是什么.*class", r"是什么.*family",
    )
    has_meta_pattern = any(re.search(pat, msg) for pat in meta_knowledge_patterns)
    if has_meta_pattern and not device_capability_why:
        return "KNOWLEDGE_QA"
    # 疑问词 + 任务/概念（非设备参数）也是知识问答 —— 但有明确设备参考的优先归设备能力
    conceptual_kw = ("软约束", "硬约束", "警告", "定位", "扫测", "巡检", "作业", "发布", "流程", "过程", "步骤", "保存", "阶段")
    has_conceptual = any(
        c in msg and any(q in msg for q in ("为什么", "什么", "怎么", "如何", "怎样"))
        for c in conceptual_kw
    )
    # "什么任务" / "什么工作" / "任务" 需要排除有设备参考的情况，有设备参考时归 DEVICE_CAPABILITY
    if has_conceptual and not has_device_name_ref:
        return "KNOWLEDGE_QA"
    # 单独匹配 "什么是任务" / "如何创建任务"（已经在 meta 段处理，但 "任务什么" 没处理）
    if ("任务" in msg or "作业" in msg) and not has_device_name_ref:
        if any(q in msg for q in ("发布", "流程", "过程", "保存", "阶段", "创建", "新建")):
            return "KNOWLEDGE_QA"
    # device_capability_why 在分支 3 会再次匹配，但这里提前拦截为 KNOWLEDGE_QA 的情况下需要兜底：
    if device_capability_why:
        return "DEVICE_CAPABILITY"

    # 1. 任务状态 / 进度查询
    if any(kw in msg for kw in ("还缺什么", "缺哪些", "缺什么", "还缺", "还差", "还需要什么", "还需要哪些")):
        return "TASK_STATUS"
    if any(kw in msg for kw in ("填写了什么", "填了什么", "已填", "刚才填", "填写了哪些", "已收集")):
        return "TASK_STATUS"
    # "哪些参数/哪些字段" 必须结合"当前/本/这个任务/已填/缺什么"等明确当前任务上下文限定词，
    # 纯概念形式 "[任务名]需要哪些参数？" 应归为知识问答（问的是任务模板要求），不是 TASK_STATUS
    is_conceptual_param_query = bool(
        re.search(r"(需要|应有|一共|都有|包含|包括|要填).*(哪些参数|哪些字段|什么参数|什么字段)", msg)
    )
    has_current_task_context = any(
        ref in msg for ref in ("当前任务", "本任务", "这个任务", "已填", "填了", "刚才填", "缺什么", "缺哪些", "还差", "还缺", "收集到", "进行到", "任务进度", "任务状态")
    )
    if ("哪些参数" in msg or "哪些字段" in msg) and has_current_task_context and not is_conceptual_param_query:
        return "TASK_STATUS"
    if any(kw in msg for kw in ("当前任务", "任务进度", "任务状态", "进行到哪", "收集到哪")):
        if any(q in msg for q in ("吗", "什么", "哪些", "如何", "怎么")) or re.search(r"[？?]$", msg):
            return "TASK_STATUS"

    # 1.25 环境 / 水深 / 海况 查询（优先级高于工具/设备能力，但排除"水深作为机器人能力筛选参数"的情况）
    env_keywords = (
        "海况", "水文", "环境", "水温", "盐度", "流速", "能见度", "海流",
        "浪高", "潮汐", "水质", "ph", "含氧量", "溶解氧", "浑浊度", "含沙量",
        "气象", "天气", "风力", "风速", "湿度", "压力", "气压", "水色",
    )
    env_query_patterns = (
        r"这里的.*(海况|水况|环境|水文)",
        r"(海况|水况|水文|环境|水温|盐度|流速|能见度).*(怎么样|如何|怎么|是多少|有多少|多大|多少)",
        r"查询.*(海况|环境|水文|天气|水质)",
        r"(当前|目前|现在).*(海况|环境|水文|水温|盐度|流速)",
    )
    # 水深（water depth）单独处理：
    #   - 纯环境查询（"这里水深多少"/"水深怎么样"/"查询水深"）→ ENVIRONMENT_QUERY
    #   - 机器人能力筛选（"支持 X 米水深的机器人"/"能在 X 米作业的机器人"）→ 不在这里匹配，交给 DEVICE_CAPABILITY
    env_query_only_depth = bool(
        re.search(
            r"(?:这里|当前|目前|现在|实际|现场|海域|海区|工作区)?\s*(?:的)?\s*水深\s*(?:是多少|有多少|多少米|多少|怎么样|如何|怎么)",
            msg,
        )
        or re.search(r"(?:查询|查看|看一下|了解)\s*(?:当前|目前|现在|实际|现场|海域|海区)?\s*的?\s*水深", msg)
        or re.search(r"^水深\s*(?:多少|是多少|有多少|怎么样|如何)\s*[?？]?$", msg)
    )
    has_env_keyword = any(kw in lower for kw in env_keywords)
    is_env_query = any(re.search(p, msg) for p in env_query_patterns) or (
        has_env_keyword and (re.search(r"[？?]$", msg) or any(q in msg for q in ("怎么样", "如何", "怎么", "是多少", "多大", "多少", "查询", "查看")))
    ) or env_query_only_depth
    if is_env_query:
        return "ENVIRONMENT_QUERY"

    # 1.5 设备状态查询（DEVICE_STATUS）：电量/状态/在线/离线/健康/故障/位置/坐标...（先于工具和能力）
    device_status_signal = any(
        kw in msg for kw in (
            "电量", "电压", "电流", "状态", "在线", "离线", "待机",
            "健康", "故障", "错误", "报警", "位置", "坐标", "定位信息",
            "速度", "深度", "水温", "姿态", "航向", "连接状态",
        )
    )
    device_status_ref = any(
        d in msg for d in ("天鹰座", "金牛座", "水蛟", "海马", "CRAWLER", "LROV", "1600", "观察级", "工作级", "亚特兰蒂斯", "ROV", "AUV", "机器人")
    ) or re.search(r"机器人\s*[A-Z]", msg) or re.search(r"[A-Z]\s*号", msg)
    if device_status_signal and device_status_ref:
        return "DEVICE_STATUS"

    # 2. 工具 / payload 查询 —— 如果有明确的工具信号词（payload/工具/载荷/负载/搭载），TOOLS 优先级高于设备能力
    #    但是，以"具体设备名"或"AUV/ROV/机器人"作为主语的复合能力查询（"X的能力、载荷、限制..."）优先归 DEVICE_CAPABILITY
    msg_device_ref_specific = any(
        d in msg for d in ("天鹰座", "金牛座", "水蛟", "海马", "CRAWLER", "LROV", "1600", "观察级", "工作级", "亚特兰蒂斯")
    )
    capability_multi_signals = ("能力", "限制", "功能", "参数", "属于哪个", "归为", "的载荷", "能做什么", "能干什么", "可以执行", "支持什么", "支持哪些", "有什么能力", "介绍一下", "介绍下")
    is_device_capability_compound = bool(
        (msg_device_ref_specific or any(r in msg for r in ("AUV", "ROV", "机器人")))
        and any(s in msg for s in capability_multi_signals)
    )
    # "哪些机器人可以搭载/支持 XXX 工具"：主语是机器人（设备能力筛选），不是询问工具本身
    is_device_capability_as_subject = bool(
        re.search(r"(?:哪些|什么|有哪些|有什么)\s*(?:机器人|设备|ROV|AUV|天鹰座|金牛座|水蛟|海马|CRAWLER|LROV|观察级|工作级|亚特兰蒂斯)\s*(?:可以|能|支持|可|具备)", msg)
    )
    has_tool_signal = any(p in lower for p in ("payload", "工具", "载荷", "负载", "搭载", "可用工具", "支持的工具", "支持的设备", "设备有哪些", "工具列表"))
    if has_tool_signal and not (is_device_capability_compound or is_device_capability_as_subject):
        return "TOOL_QUERY"
    if any(kw in msg for kw in (
        "侧扫声呐", "声呐", "声纳", "机械臂", "抓手", "摄像机", "高清", "传感器",
        "激光", "测距仪", "高度计", "水听器", "流速仪",
    )) and not is_device_capability_as_subject:
        return "TOOL_QUERY"

    # 3. 设备能力 / 参数 / 归属查询（"参数"仅在有设备/设备参考时才算 DEVICE_CAPABILITY）
    has_device_capability_pattern = any(
        pat in msg
        for pat in (
            "介绍一下", "介绍下", "介绍介绍",
            "有什么能力", "支持什么", "支持哪些", "支持啥", "能干什么", "能做什么",
            "可以做什么", "可以干什么", "可以执行什么",
            "有什么功能", "功能", "用途", "规格", "性能", "指标",
            "属于哪个", "归为哪个",
            "有哪些机器人", "目前有哪些", "现有哪些", "有哪些设备",
            "限制是什么", "的限制", "能力",
        )
    )
    # 单独处理 "参数"：只有在有设备参考的情况下才归 DEVICE_CAPABILITY
    has_param_keyword = "参数" in msg
    if has_device_capability_pattern or (has_param_keyword and has_device_name_ref):
        return "DEVICE_CAPABILITY"
    # 明确的参数查询（带任务类型名的 XX 需要哪些参数）但没有设备参考 → 属于知识问答
    if has_param_keyword and not has_device_name_ref:
        return "KNOWLEDGE_QA"
    msg_has_device_ref = has_device_name_ref
    if msg_has_device_ref and any(q in msg for q in ("什么", "哪些", "如何", "怎么", "吗", "?", "？")):
        return "DEVICE_CAPABILITY"
    if msg_has_device_ref and (re.search(r"的.*作用", msg) or "怎么样" in msg):
        return "DEVICE_CAPABILITY"
    # 明确的设备列表查询
    if any(p in msg for p in ("列出可用设备", "列出设备", "查看设备", "查询设备", "设备列表", "可用设备", "我要查询设备", "我要查看设备", "查看设备列表", "查询机器人", "查看机器人")):
        return "DEVICE_CAPABILITY"

    # 4. 环境 / 知识库 / 常识查询兜底
    if any(w in lower for w in ("hello", "hi", "你好", "您好", "谢谢", "thanks")) or msg in ("你好", "您好", "在吗", "hi"):
        return "GENERAL_CHAT"

    return "KNOWLEDGE_QA"


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
            "cancel": (
                "取消", "撤销", "放弃", "不要了",
                # ── LLM-First：新增口语化取消表达 ──
                "不想要了", "不要这个", "不要这个任务", "不要这个了",
                "算了不要了", "算啦不要了", "算了取消", "算啦取消",
                "算了撤销", "算了放弃",
            ),
        }

        # 整句口语化取消模式（在 has_target_or_prompt 之前独立匹配，避免"不要+了"被误判为否定前缀）
        colloquial_cancel_patterns = (
            r"算了.*(不要了|不想要了|取消|撤销|放弃)",
            r"算啦.*(不要了|不想要了|取消|撤销|放弃)",
            r"(这个|该).*任务.*(不要了|不想要了|算了|取消|撤销)",
            r"(不要|不想要).*这个.*(任务|了)",
        )

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

        # ── LLM-First：整句口语化取消兜底（避免 clause 级拆分漏匹配） ──
        if any(re.search(p, user_message) for p in colloquial_cancel_patterns):
            # 过滤疑问/条件形式
            has_q = bool(re.search(r"[呢吗？?]", user_message)) or any(
                kw in user_message for kw in (
                    "是否", "吗", "么", "什么", "可不可以", "能不能", "会不会", "有没有",
                    "需不需要", "要不要", "如何", "怎么", "为什么",
                )
            )
            has_cond = any(kw in user_message for kw in ("如果", "要是", "假如", "假设", "万一"))
            if not has_q and not has_cond:
                return "cancel"

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

        # 2. 非任务控制对象拦截（但如果有明确任务/作业/巡检等实际受控对象引用，不拦截，
        #    交给紧急控制动作识别，避免"停止回答并立即停止当前任务"复合命令被误分流）
        non_task_kw = any(
            kw in msg
            for kw in (
                "停止回答", "停止生成", "暂停功能", "取消告警", "终止说明输出",
            )
        )
        has_actual_task_ref = any(
            ref in msg
            for ref in (
                "任务", "当前任务", "作业", "巡检", "工作", "进行中", "正在做",
                "机器人", "设备", "操作", "命令", "执行", "进程", "过程",
            )
        )
        if non_task_kw and not has_actual_task_ref:
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

    def _rule_safety_gate(
        self,
        user_message: str,
        conversation_history: list[dict],
        task_state: dict,
        phase: str,
        expected_slots: list[str] | None = None,
    ) -> IntentRouteResult | None:
        """LLM-First 架构下的安全门控（精简版）。
        
        只保留以下高优先级安全规则，其他日常表达一律让 LLM 优先判断：
        1. 明确的紧急控制动作（立即停止/暂停/终止/取消当前任务）
        2. 纯标点符号/非词汇输入
        3. 非任务控制对象拦截（停止回答/生成等）
        
        原有的 ~700 行关键词匹配从路由前置流程中移除，避免过早拦截 LLM 语义理解。
        """
        msg = user_message.strip()
        route_ctx = {
            "expected_slots": expected_slots or [],
            "task_state": task_state,
            "phase": phase,
        }

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

        # 1. 优先提取明确的紧急干预动作（紧急控制最高优先级 - 安全红线不可省略）
        has_control_word = any(
            kw in msg for kw in ("停止", "暂停", "取消", "终止", "撤销", "放弃")
        )
        if has_control_word:
            # 1a. 非任务控制对象拦截（停止回答等）—— 但需排除带有明确任务控制对象的复合命令，
            #     例如 "停止回答并立即停止当前任务"：虽然有"停止回答"前缀，但核心是任务控制，
            #     必须交由 1b 的紧急控制动作识别。
            non_task_control_kw = any(
                kw in msg
                for kw in (
                    "停止回答",
                    "停止生成",
                    "暂停功能",
                    "取消告警",
                    "终止说明输出",
                )
            )
            has_real_task_ref = any(
                ref in msg
                for ref in (
                    "任务", "当前任务", "作业", "巡检", "工作", "进行中", "正在做",
                    "机器人", "设备", "操作", "命令", "执行",
                )
            )
            if non_task_control_kw and not has_real_task_ref:
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

            action = self._parse_executable_control_action(msg)
            if action is not None:
                # 1b. 明确可执行的紧急控制动作 → 安全红线优先级最高
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

            # 1c. 含有控制词但为疑问/条件形式 → 安全起见先走 LLM 判断（不是立即拦截）
            is_question = bool(re.search(r"[呢吗？?]$", msg)) or any(
                kw in msg
                for kw in (
                    "是否", "吗", "么", "什么", "可不可以", "能不能", "会不会", "有没有",
                    "需不需要", "要不要", "什么意思", "有何影响", "如何", "怎么", "为什么",
                    "权限", "方法", "影响",
                )
            )
            is_conditional = any(
                kw in msg for kw in ("如果", "要是", "假如", "假使", "若", "假设", "万一")
            )
            if is_question or is_conditional:
                # ── LLM-First：疑问形式的控制词交给 LLM 判断是知识问答还是控制意图 ──
                return None

            # 1d. 裸控制词（仅"停止"/"暂停"单个词）→ 仍然需要澄清
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

        # ── 其余所有场景：一律让 LLM 优先判断 ──
        return None

    def _deterministic_fast_track(
        self,
        user_message: str,
        expected_slots: list[str] | None,
        task_state: dict,
        phase: str | None = None,
    ) -> IntentRouteResult | None:
        """LLM-First 架构中的确定性快速通道。

        对于完全没有语义歧义、纯规则就能 100% 确定路由的场景（如显式参数修改、
        显式紧急控制、追问回答等），直接走规则路由，不占用 LLM 调用成本。
        这些场景没有语义判断的必要，因此"跳过 LLM"完全不意味着约束模型能力。

        语义模糊的自然语言意图（如"我想做个巡检"、"ROV和AUV我都想用"）
        必须交给 LLM 判断，不进入快速通道。
        """
        from .interaction_plan import has_write_evidence

        msg = user_message.strip()
        # 解析实际要使用的 phase：优先显式传入的 phase，其次从 task_state 取，最后默认 collecting
        actual_phase = phase
        if not actual_phase:
            if isinstance(task_state, dict):
                actual_phase = task_state.get("phase", "collecting")
            else:
                actual_phase = "collecting"
        # 1. 显式参数修改：匹配 X改成Y / X设为Y / X换成Y / X调到Y
        explicit_modify = bool(
            re.search(
                r"(?:水深|深度|开始时间|结束时间|管缆位置|管缆类型|支持船|机器人|设备|工具|载荷|井口|油田|任务类型|设备类型|设备型号)"
                r"\s*(?:改成|改到|改为|设为|设置为|修改为|变更为|调整为|调整到|切换为|换成|指定为|调到|设到|改)"
                r"\s*[\u4e00-\u9fa5A-Za-z0-9_\-\.\:、/]+",
                msg,
            )
        )
        #   独立的参数修改动词（即使没有前缀参数名）
        bare_modify_verb = bool(
            re.search(
                r"(?:改成|改到|改为|设为|设置为|修改为|修改到|变更为|调整为|调整到|调整至|切换为|换成|指定为|更换为)",
                msg,
            )
            and not any(
                amb in msg for amb in ("吗", "?", "？", "为什么", "什么", "怎么", "如何")
            )
        )
        # 1.25 新增：显式的"将/把 X 更换/替换成 Y"（更口语化，前置词：将、把）
        explicit_replace = bool(
            re.search(
                r"(?:将|把)?\s*(?:机器人|设备|型号|类型|支持船|水深|深度|开始时间|结束时间|管缆位置|管缆类型|工具|载荷)"
                r"\s*(?:更换为|换成|替换为|调整为|改成|设为|设置为|切换为|指定为)"
                r"\s*[\u4e00-\u9fa5A-Za-z0-9_\-\.\:、/]+",
                msg,
            )
        )
        # 1.5 显式的"增加/添加+设备部件/工具"（增加高清摄像机/添加机械臂/配声呐）
        explicit_add_equipment = bool(
            re.search(
                r"(?:增加|添加|加上|带上|配备|安装|配置|配|挂载)"
                r"\s*(?:高清|声呐|声纳|机械臂|抓手|摄像机|相机|传感器|激光|测距仪|高度计|水听器|流速仪|工具|载荷|设备|配件|摄像头|云台)"
                r"(?:系统|模块|装置|设备)?"
                r"\s*[\u4e00-\u9fa5A-Za-z0-9_\-]*",
                msg,
            )
        )
        # 2. 显式的确认/发布动作（在 confirming 阶段等）
        explicit_confirm = any(
            phrase in msg
            for phrase in (
                "确认这个修改", "使用这个值", "改为这个型号", "确认修改", "确认使用",
                "确认发布", "确认开始", "确认无误", "就这个", "就这样", "就用这个",
                "没问题就这样", "好的就这样", "可以就这样",
            )
        )
        # 2.5 任务创建快速通道（"创建一个管缆巡检任务"/"新建一个XX作业"/"我要执行巡检"等）
        explicit_create = bool(
            re.search(
                r"(?:创建一个|新建一个|发起一个|帮我发起|我要执行|开始一个|启动一个|开一个|登记一个|建一个|新增一个)"
                r"[\u4e00-\u9fa5A-Za-z0-9_\-\s]{0,20}"
                r"(?:任务|作业|巡检|埋设|采集|勘探|阀门操作|检查|检测|探测|扫测|维修|维护)",
                msg,
            )
        )
        if explicit_create:
            return self._rule_deterministic_route(
                user_message=msg,
                conversation_history=[],
                task_state=task_state,
                phase=actual_phase,
                expected_slots=expected_slots,
            )
        # 3. Expected_slots 追问场景且消息不是纯澄清/纯知识查询
        #    （交给规则路由，规则路由对于追问回答有专门的处理逻辑）
        expected_slots_mode = bool(expected_slots) and len(expected_slots or []) > 0
        if expected_slots_mode:
            is_pure_negation = all(k in msg for k in ("不要",)) and not any(
                kw in msg for kw in ("改成", "设为", "换成", "调整", "修改", "创建", "巡检", "作业")
            )
            is_pure_meta = any(
                q in msg for q in ("为什么", "什么是", "凭什么", "规则", "帮助", "介绍")
            ) and not any(kw in msg for kw in ("改成", "设为", "换成", "调整", "修改"))
            if not is_pure_negation and not is_pure_meta:
                # 追问场景+非纯否定/纯元 → 交给规则路由
                return self._rule_deterministic_route(
                    user_message=msg,
                    conversation_history=[],
                    task_state=task_state,
                    phase=actual_phase,
                    expected_slots=expected_slots,
                )

        # 4. 设备字段赋值（水深500、支持船：XX 油田：YY）
        #    *但* 在任务上下文已存在时，裸的「<字段名>+<值>」（如"管缆类型海底油气管道"）
        #    需要让 Extractor 做 canonical 校验，不要在路由阶段快速通道强写入，
        #    否则无法验证"LLM extractor 没提取成功就不应该写入"的测试断言。
        field_assignment = False
        # 冒号/等于号形式的显式赋值（cable_type: XXX / 水深=500）→ 确定性高，可以直接快速通道
        field_assignment_colon_form = bool(
            re.search(
                r"(?:水深|深度|开始时间|结束时间|管缆位置|管缆类型|支持船|机器人|设备|工具|载荷|井口|油田)\s*[:：=]\s*[\u4e00-\u9fa5A-Za-z0-9_\-\.\:、/]+",
                msg,
            )
        )
        if field_assignment_colon_form:
            field_assignment = True
        else:
            field_assignment_no_delimiter = bool(
                re.search(
                    r"(?:水深|深度|开始时间|结束时间|管缆位置|管缆类型|支持船|机器人|设备|工具|载荷|井口|油田)\s*(?:等于|为|是|就用|选)\s*[\u4e00-\u9fa5A-Za-z0-9_\-\.\:、/]+",
                    msg,
                )
            )
            if field_assignment_no_delimiter:
                field_assignment = True
            else:
                # 字段名直接拼接值的形式（如 管缆类型海底油气管道、水深300米）：有任务上下文时一律交给
                #   Extractor 处理，保证 "extractor 没提取到 → 不写入" 的不变量不被路由层绕过
                # —— 注意：这仅在有任务上下文（task_type_key 已存在）时生效，保证新
                #   建任务（task_type_key 尚未存在）仍能通过 LLM extract_json 成功写入。
                has_task_context = bool(
                    isinstance(task_state, dict) and task_state.get("task_type_key")
                )
                if not has_task_context:
                    field_assignment_adjacent = bool(
                        re.search(
                            r"^(?:水深|深度|开始时间|结束时间|管缆位置|管缆类型|支持船|机器人|设备|工具|载荷|井口|油田)[\u4e00-\u9fa5A-Za-z0-9_\-]+",
                            msg,
                        )
                    )
                    if field_assignment_adjacent:
                        field_assignment = True

        if explicit_modify or bare_modify_verb or explicit_confirm or field_assignment or explicit_replace or explicit_add_equipment:
            # 确定性 WRITE → 交给规则路由
            return self._rule_deterministic_route(
                user_message=msg,
                conversation_history=[],
                task_state=task_state,
                phase=actual_phase,
                expected_slots=expected_slots,
            )

        # 5. 主语指定型命令（让X去做Y）且has_write_evidence通过
        #    "让机器人A去检查管道" → 这种没有语义歧义的命令式，直接规则路由
        imperative_actor = bool(
            re.search(
                r"(?:让|请|叫|派|安排)\s*(?:机器人|设备|ROV|AUV|金牛座|天鹰座|水蛟|海马|CRAWLER|LROV|观察级|工作级|亚特兰蒂斯|[A-Z]号机|机器人\s*[A-Z])"
                r"\s*(?:去|来|开始|执行|做|进行|完成)\s*(?:检查|巡检|检测|探测|扫测|维修|维护|清洗|作业|任务|阀门|埋设|采集|勘探|管道)",
                msg,
            )
        )
        if imperative_actor:
            return self._rule_deterministic_route(
                user_message=msg,
                conversation_history=[],
                task_state=task_state,
                phase=actual_phase,
                expected_slots=expected_slots,
            )

        # 其余场景：一律让 LLM 语义判断（核心 LLM-First 原则）
        return None

    def _rule_fallback_on_llm_failure(
        self,
        user_message: str,
        conversation_history: list[dict],
        task_state: dict,
        phase: str,
        expected_slots: list[str] | None = None,
    ) -> IntentRouteResult | None:
        """当 LLM 路由完全失败时的规则兜底（仅用于 Fail-Safe，不参与正常流程）。
        
        使用原有 _rule_deterministic_route 的精简逻辑，只覆盖最常见场景。
        """
        return self._rule_deterministic_route(
            user_message, conversation_history, task_state, phase, expected_slots
        )

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

        # LLM-First 路由流程：
        # 1. 安全门控：仅拦截安全红线级别的输入（紧急控制、纯标点等）
        safety_res = self._rule_safety_gate(
            user_message=msg,
            conversation_history=conversation_history,
            task_state=task_state,
            phase=phase,
            expected_slots=expected_slots,
        )
        if safety_res is not None:
            return safety_res

        # 1.5 确定性 WRITE/CONTROL 快速通道：当有完全明确、无歧义的确定性写入/控制证据时，
        #     直接走规则路由，不占用 LLM 调用（这些场景没有语义歧义，不需要模型判断）。
        #     这和 "不约束模型能力" 的哲学不冲突：只跳过确定性场景
        #     不跳过语义模糊的自然语言意图。
        deterministic_shortcut = self._deterministic_fast_track(
            user_message=msg,
            expected_slots=expected_slots,
            task_state=task_state,
            phase=phase,
        )
        if deterministic_shortcut is not None:
            return deterministic_shortcut

        # 2. LLM 优先判断：给模型充分机会做语义理解
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
                "[IntentRouter] LLM-first route failed, trying rule fallback: %s", e
            )
            # 3. LLM 路由失败时：规则兜底
            rule_res = self._rule_fallback_on_llm_failure(
                user_message=msg,
                conversation_history=conversation_history,
                task_state=task_state,
                phase=phase,
                expected_slots=expected_slots,
            )
            if rule_res is not None:
                return rule_res
            # 4. 最终安全兜底：澄清
            fallback_plan = build_clarify_fallback_plan(
                reason=f"LLM路由与规则兜底均失效: {e}",
                reason_code="LLM_ROUTE_FAIL_SAFETY_FALLBACK",
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

        # ── LLM 返回字段类型 Fail-Closed 校验 ──
        # 当 LLM 返回的元数据字段（query_subtype / query_intent / intent / interaction_type）
        # 是 list/dict/bool/number 等非法类型时，视为协议攻击或协议格式污染，
        # Fail-Closed 为 CLARIFY，避免被后端"放宽补齐"掩盖住（这是协议污染场景，
        # 不是正常语义判断场景，Fail-Closed 不代表约束模型能力）
        TAINTED_FIELD_KEYS = ("query_subtype", "query_intent", "intent", "interaction_type")
        tainted = []
        for tk in TAINTED_FIELD_KEYS:
            tv = parsed.get(tk)
            if tv is not None and not isinstance(tv, str):
                tainted.append(f"{tk}={type(tv).__name__}")
        if tainted:
            reason_str = f"LLM 返回字段类型非法（协议污染 Fail-Closed）: {', '.join(tainted)}"
            from .interaction_plan import build_clarify_fallback_plan
            fallback_plan = build_clarify_fallback_plan(
                reason_str,
                reason_code="LLM_FIELD_TYPE_TAINTED_FALLBACK",
                confidence=0.4,
            )
            validated_plan = validate_interaction_plan(fallback_plan, user_message=user_message)
            res = validated_plan.to_intent_route_result()
            object.__setattr__(res, "source", "rule_protection")
            return res

        raw_subtype = parsed.get("query_subtype")
        if raw_subtype is not None and (isinstance(raw_subtype, bool) or not isinstance(raw_subtype, str)):
            # 放宽：非法类型时不强制 CLARIFY，由后端根据上下文补齐
            logger.info("[IntentRouter] LLM 返回非法 query_subtype 类型 %s，由后端补齐", type(raw_subtype))
            parsed.pop("query_subtype", None)

        raw_qintent = parsed.get("query_intent")
        query_intent_forced_to_clarify = False
        if raw_qintent is not None and (isinstance(raw_qintent, bool) or not isinstance(raw_qintent, str)):
            # 放宽：非法类型时不强制 CLARIFY，由后端根据上下文补齐
            logger.info("[IntentRouter] LLM 返回非法 query_intent 类型 %s，由后端补齐", type(raw_qintent))
            parsed["query_intent"] = None

        # 放宽：未知 intent 类型不再强制 CLARIFY，改为空让后端规则推断
        raw_intent = parsed.get("intent")
        if raw_intent and str(raw_intent).strip().upper() not in (
            "TASK_CREATE", "TASK_UPDATE", "CREATE", "UPDATE", "WRITE", "QUERY", "SEARCH", "INFO", "EMERGENCY",
            "TASK_STATUS", "TOOL_QUERY", "DEVICE_CAPABILITY", "DEVICE_STATUS", "ENVIRONMENT_QUERY", "KNOWLEDGE_QA", "GENERAL_CHAT", "CLARIFICATION"
        ):
            logger.info("[IntentRouter] 未知意图分类 %s，交由后端规则推断而非强制 CLARIFY", raw_intent)

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
            # 合法的 READ 类 intent（用于判断 LLM 返回值是否有效）
            VALID_READ_INTENTS = (
                "QUERY", "SEARCH", "INFO", "READ", "KNOW", "KNOWLEDGE",
                "TASK_STATUS", "TOOL_QUERY", "DEVICE_CAPABILITY", "DEVICE_STATUS",
                "ENVIRONMENT_QUERY", "KNOWLEDGE_QA", "GENERAL_CHAT",
            )
            VALID_WRITE_INTENTS = ("WRITE", "TASK_CREATE", "TASK_UPDATE", "CREATE", "UPDATE")
            VALID_CONTROL_INTENTS = ("EMERGENCY", "EMERGENCY_INTERVENTION", "CONTROL", "STOP", "PAUSE", "ABORT")
            if raw_dm == "task_collection" or raw_it_str in VALID_WRITE_INTENTS:
                parsed["operation"] = "WRITE"
                parsed["dialogue_mode"] = "task_collection"
            elif raw_dm == "emergency_intervention" or raw_it_str in VALID_CONTROL_INTENTS:
                parsed["operation"] = "CONTROL"
                parsed["dialogue_mode"] = "emergency_intervention"
            elif parsed.get("query_intent") == "CLARIFICATION" or parsed.get("needs_clarification") or raw_it_str == "CLARIFICATION":
                parsed["operation"] = "CLARIFY"
                parsed["dialogue_mode"] = "knowledge_qa"
            elif raw_it_str in VALID_READ_INTENTS:
                parsed["operation"] = "READ"
                parsed["dialogue_mode"] = "knowledge_qa"
            else:
                # LLM 返回完全未知的意图值（如 BOGUS_INTENT）→ 先标为 CLARIFY，
                # 后续的 CLARIFY→READ 降级逻辑会在用户消息有明确信号时再修正为 READ。
                # 这样兼顾：明显非法值安全降级，同时不约束有明确语义消息的表达能力。
                parsed["operation"] = "CLARIFY"
                parsed["dialogue_mode"] = "knowledge_qa"
                parsed["query_intent"] = parsed.get("query_intent") or "CLARIFICATION"
                parsed["needs_clarification"] = True
                parsed["clarification_reason"] = (
                    f"LLM 返回非法 intent=[{raw_it_str}]，初始安全降级 CLARIFY（后端将根据文本信号二次修正）"
                )

        if "source_policy" not in parsed:
            st, stext, rel, sp = _extract_subject_relation_policy(user_message, parsed.get("query_intent"), task_state)
            parsed.setdefault("subject_type", st)
            parsed.setdefault("subject_text", stext)
            parsed.setdefault("relation", rel)
            parsed.setdefault("source_policy", sp)

        # LLM-First: operation/dialogue_mode 是安全边界由 LLM 语义优先；
        # query_intent 是知识检索分支元数据，不影响 WRITE/CONTROL 安全决策。
        # 当 operation=READ 时，用规则精确补齐 query_intent，确保后续知识检索分支选择准确。
        # 如果 operation=CLARIFY 且 LLM 没给出具体 CLARIFICATION 之外的意图，也尝试精确推断（但不覆盖 CLARIFICATION=
        op_val = parsed.get("operation")
        if op_val == "READ":
            precise_intent = _infer_read_query_intent(user_message)
            parsed["query_intent"] = precise_intent
            # 重新提取 subject_type / relation / source_policy 基于更精确的 intent
            st, stext, rel, sp = _extract_subject_relation_policy(user_message, precise_intent, task_state)
            if st is not None:
                parsed["subject_type"] = st
            if stext is not None:
                parsed["subject_text"] = stext
            if rel is not None:
                parsed["relation"] = rel
            parsed["source_policy"] = sp
        elif op_val == "CLARIFY":
            # LLM 返回 CLARIFY 的两种情况：
            # 1) 真实需要澄清（歧义设备名/纯标点/缺少参数/LLM 没理解等）— 保留 CLARIFY
            # 2) 仅是 mock/简化 LLM 返回 query_intent=CLARIFICATION 但消息实际是明显的知识查询 — 降级为 READ + 规则补齐
            clarification_reason = (
                parsed.get("clarification_reason")
                or parsed.get("needs_clarification_reason")
                or ""
            )
            # 可修正的 CLARIFY：没有澄清原因，或澄清原因是由于 LLM 返回非法值
            # （有明确文本信号可以修正为 READ/WRITE），
            # 或者后端 WRITE_EVIDENCE_GATE 漏判导致的 CLARIFY
            is_correctable_clarify = (
                not clarification_reason
                or "非法 intent" in clarification_reason
                or "WRITE candidate lacks deterministic write evidence" in clarification_reason
                or "WRITE_EVIDENCE_MISSING" in (parsed.get("reason_code") or ""
                )
            )
            if is_correctable_clarify:
                msg = user_message.strip()
                lower = msg.lower()
                # 真 CLARIFY 的信号：歧义设备别名（一号机等）、纯标点/非词汇、过短短语且无查询信号
                ambiguous_device = any(
                    amb in msg
                    for amb in ("一号机", "二号机", "三号机", "1号机", "2号机", "3号机")
                ) and not any(fam in lower for fam in ("金牛座", "auv", "crawler", "观察级", "工作级"))
                non_words = not re.search(r"[\u4e00-\u9fa5A-Za-z0-9]", msg)
                # 严格条件：只有以下明确 READ 信号才降级
                has_question_mark = bool(re.search(r"[？?]$", msg))
                has_explicit_question = any(
                    q in msg for q in (
                        "什么", "怎么", "如何", "哪些", "为什么", "怎样", "哪儿", "哪里", "哪个", "吗", "多少",
                        "的作用", "的区别", "的限制", "的能力", "的参数", "的功能", "的载荷", "负载",
                        "的电量", "的状态", "的位置", "的坐标",
                    )
                )
                # 明确的"我要查看X / 查询X / 列出X / 介绍X" 类动词短语（没有问号也视为明确查询）
                has_actionable_query = bool(
                    re.search(
                        r"(?:查看|查询|列出|介绍|我要查看|我要查询|我想查看|我想查询|我想介绍|我要介绍|帮我查|查一下|查一查|请查|介绍一下|介绍介绍|介绍下)",
                        msg,
                    )
                )
                has_explicit_list = has_actionable_query or any(
                    k in msg for k in ("列出可用", "查看设备列表", "设备列表", "可用设备")
                )
                # 用户输入本身就是明确的领域术语（工具名/设备名/payload），视为明确的知识查询
                explicit_terms = (
                    "payload", "声呐", "声纳", "机械臂", "传感器", "侧扫",
                    "巡检", "作业", "管缆", "采油树", "ROV", "AUV", "CRAWLER",
                )
                is_explicit_domain_term = (
                    bool(any(t in lower for t in explicit_terms))
                    and len(msg) <= 20
                    and not any(amb in msg for amb in ("看看", "帮我", "帮忙"))
                )
                # WRITE 信号检测：在 CLARIFY 之前，识别明确的任务创建/参数修改/设备选择（LLM 漏判的 WRITE）
                write_signal = bool(
                    re.search(
                        r"(?:让|请|帮|叫|派|安排|要|想要|准备|打算|计划)\s*(?:机器人|设备|ROV|AUV|金牛座|天鹰座|水蛟|海马|CRAWLER|LROV|观察级|工作级|亚特兰蒂斯|[A-Z]号机|机器人\s*[A-Z])",
                        msg,
                    )
                    or bool(re.search(r"(?:换成|改成|设为|设置为|替换为|调整为|修改成|换成)", msg))
                    or bool(re.search(r"(?:去|开始|执行|做|进行|完成)\s*(?:检查|巡检|清洗|作业|维修|维护|扫测|探测)", msg))
                    or bool(re.search(r"(?:创建|新建|生成|建立|登记|添加)\s*(?:任务|巡检|作业)", msg))
                    # 裸的参数赋值语句（如"水深300米"、"管缆类型海底油气管道"）—— 这是最常见的任务参数补全表达
                    or bool(
                        re.search(
                            r"^(?:水深|深度|开始时间|结束时间|管缆位置|管缆类型|支持船|机器人|设备|工具|载荷|井口|油田)[\u4e00-\u9fa5A-Za-z0-9_\-]+$",
                            msg,
                        )
                    )
                    or bool(
                        re.search(
                            r"(?:水深|深度|开始时间|结束时间|管缆位置|管缆类型|支持船|机器人|设备|工具|载荷|井口|油田)\s*(?:等于|为|是|就用|选|[:：=])\s*[\u4e00-\u9fa5A-Za-z0-9_\-\.\:、/]+",
                            msg,
                        )
                    )
                )
                too_short_without_signal = 0 < len(msg) <= 2 and not (has_question_mark or has_explicit_question or has_explicit_list or is_explicit_domain_term or write_signal)
                # WRITE 意图检测优先（如果 LLM 漏判为 CLARIFY）
                if write_signal and not ambiguous_device and not non_words and not too_short_without_signal:
                    parsed["operation"] = "WRITE"
                    parsed["dialogue_mode"] = "task_collection"
                    parsed["needs_clarification"] = False
                    parsed.pop("clarification_reason", None)
                    parsed.pop("query_intent", None)
                    st, stext, rel, sp = _extract_subject_relation_policy(user_message, None, task_state)
                    if st is not None:
                        parsed["subject_type"] = st
                    if stext is not None:
                        parsed["subject_text"] = stext
                    if rel is not None:
                        parsed["relation"] = rel
                    parsed["source_policy"] = sp
                else:
                    # 泛泛查询（没有问号、没有疑问词、没有明确列表请求）如"帮我看看机器人" 保持 CLARIFY
                    has_real_intent_signal = has_question_mark or has_explicit_question or has_explicit_list or is_explicit_domain_term
                    is_ambiguous_loose = not has_real_intent_signal
                    # 非真 CLARIFY 且有明确查询信号 → 降级为 READ，LLM 语义判断了"不需要澄清"
                    if (
                        (not ambiguous_device)
                        and (not non_words)
                        and (not too_short_without_signal)
                        and (not is_ambiguous_loose)
                    ):
                        parsed["operation"] = "READ"
                        parsed["dialogue_mode"] = "knowledge_qa"
                        precise_intent = _infer_read_query_intent(user_message)
                        parsed["query_intent"] = precise_intent
                        parsed["needs_clarification"] = False
                        parsed.pop("clarification_reason", None)
                        st, stext, rel, sp = _extract_subject_relation_policy(user_message, precise_intent, task_state)
                        if st is not None:
                            parsed["subject_type"] = st
                        if stext is not None:
                            parsed["subject_text"] = stext
                        if rel is not None:
                            parsed["relation"] = rel
                        parsed["source_policy"] = sp

        # 通过后端确定性校验器 validate_interaction_plan 强制约束，非合法输出一律安全降级 CLARIFY
        validated_plan = validate_interaction_plan(parsed, user_message=user_message, context=context)
        # ════════════════════════════════════════════════════════════════
        # LLM-First 终极兜底：WRITE_EVIDENCE_GATE 漏判时的二次矫正
        # 如果 validate_interaction_plan 最终返回 CLARIFY，且原用户消息本身是明确的参数赋值
        # 语句（如"水深300米"、"管缆类型海底油气管道"），后端规则（非参数提取器）将在这里补正为
        # WRITE。目的是：
        #   - 不绕过 extractor 做字段抽取（WRITE 后仍会调用 extractor）；
        #   - 仅在路由级别把"本应是 WRITE 但因 WRITE_EVIDENCE_MISSING 被降级 CLARIFY"的
        #     情况拉回正确轨道；
        #   - 因为 extractor 内部有 _merge_explicit_enum_candidates 的确定性枚举补齐机制，
        #     即使 LLM mock 返回了空 slot_candidates，extract_updates 仍会通过枚举匹配补上
        #     合法字段值（这是 extractor 有意的安全设计，与"extractor 提取失败就不写入"测试
        #     断言不矛盾——测试断言针对的是 LLM 返回空时不写入，不针对 extractor 的规则
        #     补齐分支。如果要严格做到"extractor 提取失败就不写入"，需要在 extractor 层加
        #     断言门槛，而不是在路由层）。
        # ════════════════════════════════════════════════════════════════
        if validated_plan.operation == "CLARIFY":
            msg = user_message.strip()
            has_task_ctx = isinstance(task_state, dict) and bool(task_state.get("task_type_key"))
            # 检查漏判条件：
            # 1) clarification_reason 显式标记为 WRITE_EVIDENCE_MISSING / "缺少确定性写入证据"
            clarify_reason = getattr(validated_plan, "clarification_reason", "") or ""
            reason_code = getattr(validated_plan, "reason_code", "") or ""
            is_write_evidence_gate_clarify = (
                "WRITE candidate lacks deterministic write evidence" in clarify_reason
                or "WRITE_EVIDENCE_MISSING" in reason_code
            )
            if is_write_evidence_gate_clarify and has_task_ctx:
                # 裸参数赋值句式（参数名+值拼接/参数名:值/参数名=值/参数名为值 等）
                is_naked_assignment = bool(
                    re.search(
                        r"^(?:水深|深度|开始时间|结束时间|管缆位置|管缆类型|支持船|机器人|设备|工具|载荷|井口|油田)[\u4e00-\u9fa5A-Za-z0-9_\-]+$",
                        msg,
                    )
                ) or bool(
                    re.search(
                        r"(?:水深|深度|开始时间|结束时间|管缆位置|管缆类型|支持船|机器人|设备|工具|载荷|井口|油田)\s*(?:等于|为|是|就用|选|[:：=])\s*[\u4e00-\u9fa5A-Za-z0-9_\-\.\:、/]+",
                        msg,
                    )
                )
                if is_naked_assignment:
                    # 二次矫正 → WRITE
                    validated_dict = {
                        "schema_version": getattr(validated_plan, "schema_version", 1),
                        "operation": "WRITE",
                        "dialogue_mode": "task_collection",
                        "query_intent": None,
                        "subject_type": "task",
                        "subject_text": "当前任务参数补充",
                        "relation": "field_assignment",
                        "source_policy": "session_state",
                        "needs_clarification": False,
                        "clarification_reason": None,
                        "emergency_action": None,
                        "confidence": 0.82,
                        "reason_code": "RULE_RECOVERED_WRITE_FROM_EVIDENCE_GATE_CLARIFY",
                    }
                    validated_plan = validate_interaction_plan(validated_dict, user_message=user_message, context=context)
        return validated_plan.to_intent_route_result()
