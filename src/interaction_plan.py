"""
src/interaction_plan.py - 结构化交互计划定义与后端确定性校验

InteractionPlan 是 SEAgent 内部对用户表达意图的底层强类型建模。
它解耦了“句子中出现了什么关键词”，提供可校验、确定性、包含 Subject / Relation / SourcePolicy 的计划表示。
"""

from __future__ import annotations

import math
import logging
import re
from dataclasses import dataclass
from typing import Any, Literal

logger = logging.getLogger(__name__)

OperationType = Literal["READ", "WRITE", "CONTROL", "CLARIFY"]
DialogueModeType = Literal["task_collection", "knowledge_qa", "emergency_intervention"]
SubjectType = Literal[
    "general_concept",
    "system_rule",
    "task",
    "device",
    "device_class",
    "device_family",
    "payload",
    "environment",
    "realtime_state",
    "unknown",
]
RelationType = Literal[
    "definition",
    "describe",
    "list",
    "compare",
    "supports",
    "belongs_to",
    "capabilities",
    "limitations",
    "status",
    "missing_fields",
    "filled_fields",
    "procedure",
    "unknown",
]
SourcePolicyType = Literal[
    "project_kb",
    "session_state",
    "realtime_state",
    "general_domain",
    "hybrid",
    "none",
]

VALID_OPERATIONS = {"READ", "WRITE", "CONTROL", "CLARIFY"}
VALID_DIALOGUE_MODES = {"task_collection", "knowledge_qa", "emergency_intervention"}
VALID_SUBJECT_TYPES = {
    "general_concept",
    "system_rule",
    "task",
    "device",
    "device_class",
    "device_family",
    "payload",
    "environment",
    "realtime_state",
    "unknown",
}
VALID_RELATIONS = {
    "definition",
    "describe",
    "list",
    "compare",
    "supports",
    "belongs_to",
    "capabilities",
    "limitations",
    "status",
    "missing_fields",
    "filled_fields",
    "procedure",
    "unknown",
}
VALID_SOURCE_POLICIES = {
    "project_kb",
    "session_state",
    "realtime_state",
    "general_domain",
    "hybrid",
    "none",
}
VALID_EMERGENCY_ACTIONS = {"stop", "pause", "abort", "cancel"}


@dataclass(frozen=True)
class InteractionPlan:
    schema_version: int

    operation: OperationType
    dialogue_mode: DialogueModeType

    query_intent: str | None

    subject_type: SubjectType | None
    subject_text: str | None
    relation: RelationType | None
    source_policy: SourcePolicyType

    needs_clarification: bool
    clarification_reason: str | None

    emergency_action: str | None

    confidence: float
    reason_code: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "operation": self.operation,
            "dialogue_mode": self.dialogue_mode,
            "query_intent": self.query_intent,
            "subject_type": self.subject_type,
            "subject_text": self.subject_text,
            "relation": self.relation,
            "source_policy": self.source_policy,
            "needs_clarification": self.needs_clarification,
            "clarification_reason": self.clarification_reason,
            "emergency_action": self.emergency_action,
            "confidence": self.confidence,
            "reason_code": self.reason_code,
        }

    def to_intent_route_result(self) -> Any:
        """适配转换至现有的 IntentRouteResult，保障 DialogueManager / Router 接口兼容。"""
        from .intent_router import IntentRouteResult

        interaction_type = "WRITE" if self.operation == "WRITE" else "QUERY"
        return IntentRouteResult(
            interaction_type=interaction_type,
            confidence=self.confidence,
            reason=f"[{self.reason_code}] {self.clarification_reason or 'InteractionPlan 导出的路由结果'}",
            query_intent=self.query_intent,
            dialogue_mode=self.dialogue_mode,
            source="interaction_plan",
            emergency_action=self.emergency_action,
            interaction_plan=self,
        )


def build_clarify_fallback_plan(
    reason: str,
    reason_code: str = "VALIDATION_FALLBACK_CLARIFY",
    confidence: float = 0.5,
) -> InteractionPlan:
    """产生标准的安全降级 CLARIFY InteractionPlan。"""
    return InteractionPlan(
        schema_version=1,
        operation="CLARIFY",
        dialogue_mode="knowledge_qa",
        query_intent="CLARIFICATION",
        subject_type="unknown",
        subject_text=None,
        relation="unknown",
        source_policy="none",
        needs_clarification=True,
        clarification_reason=reason,
        emergency_action=None,
        confidence=confidence,
        reason_code=reason_code,
    )


def has_write_evidence(
    user_message: str,
    expected_slots: list[str] | None = None,
    task_state: dict | None = None,
    plan_candidate: InteractionPlan | dict | None = None,
) -> bool:
    """
    LLM-First 架构下的 WRITE Evidence Gate：
    放宽证据判定标准，让 LLM 有更多机会表达自然语言意图，
    但保留安全底线（纯查询/纯条件问句不允许 WRITE）。

    主要放宽点：
    - 自然语言创建意图（"想去做..."、"准备做..."、"做巡检吧"等口语化表达）视为 WRITE
    - expected_slots 追问场景下，只要不是纯否定/纯元问题就视为 WRITE
    - 含数字+任务上下文的场景（"水深500"）视为 WRITE
    - 纯条件问句+纯知识查询才会被拦截
    """
    msg = (user_message or "").strip()
    if not msg:
        return False

    # ═══ 安全底线 0：无条件/假设性问句 或 疑问形式的影响/后果评估，绝对不允许 WRITE ═══
    # 例如："停止任务会有什么影响"、"如果水深改成500米会怎样" —— 都是知识/影响评估，不是实际写入
    is_hypothetical_or_impact_query = bool(
        (
            any(cond in msg for cond in ("如果", "要是", "假如", "假使", "若", "假设", "万一"))
            and (
                any(q in msg for q in ("会怎样", "怎么办", "会发生", "会有什么", "影响", "后果", "如何", "怎么", "为什么", "什么"))
                or re.search(r"[会将可能]\s*(?:怎样|如何|怎么|什么样|什么)", msg)
            )
        )
        or (
            re.search(r"(?:会有什么|有什么|会有哪些).*(?:影响|后果|问题|风险|副作用|变化)", msg)
        )
        or (
            re.search(r"(?:停止|取消|暂停|撤销|放弃|终止).*(?:任务|作业|巡检|采集).*(?:什么|为何|为什么|如何|怎么|影响|后果)", msg)
        )
    )
    if is_hypothetical_or_impact_query:
        return False

    # ── 安全底线：纯条件问句+元问题查询绝对不允许 WRITE ──
    is_pure_conditional_meta_question = (
        any(cond in msg for cond in ("如果", "要是", "假如", "假使", "若", "假设", "万一"))
        and any(q in msg for q in ("怎么办", "如何", "怎么", "为什么", "什么是", "为何", "需要准备"))
        and not any(t in msg for t in ("任务", "巡检", "埋设", "作业", "采集", "阀门", "管缆"))
    )
    if is_pure_conditional_meta_question:
        return False

    # ── 安全底线：纯知识库查询（无任何任务创建/修改信号）不 WRITE ──
    has_task_domain_signal = any(
        kw in msg for kw in (
            "巡检", "埋设", "作业", "任务", "采集", "阀门", "管缆", "管道", "油气",
            "ROV", "AUV", "机器人", "设备", "支持船",
            "水深", "深度", "开始时间", "结束时间", "管缆位置", "管缆类型", "油田", "井口",
        )
    )
    is_pure_knowledge_query = (
        (bool(re.search(r"[呢吗？?]$", msg))
         or any(q in msg for q in ("介绍", "说明", "什么是", "为何", "为什么", "含义", "概念", "区别", "差异", "不同",
                                   "有哪些", "属于哪个", "属于", "搭载哪些", "支持哪些", "适合作业", "工作吗", "作业吗",
                                   "能做什么", "可以做什么", "最大水深是", "最大水深多少", "有什么影响", "会发生什么")))
        and not has_task_domain_signal
    )
    if is_pure_knowledge_query:
        return False

    # 1. 显式写入动词（原始严格逻辑保留）
    has_explicit_write_verb = any(
        kw in msg for kw in (
            "改成", "改到", "改为", "设为", "设置为", "修改为", "修改到", "变更为", "调整为", "调整到", "调整至", "切换为", "换成", "指定为",
            "创建一个", "新建一个", "发起一个", "帮我发起", "我要执行", "增加", "添加", "加上", "带上", "配备",
            "搭载", "安装", "配置", "配", "挂载", "删除", "移除", "去掉", "清空", "清掉", "都不要", "不要了", "取消修改", "撤销修改", "取消水深修改",
            "不修改", "确认发布", "确认开始", "确认无误"
        )
    ) or bool(re.search(r"取消.*修改", msg))
    if has_explicit_write_verb:
        return True

    # 2. Expected Slot 追问回答：大幅放宽
    if expected_slots and len(expected_slots) > 0:
        is_pure_negation = (
            all(k in msg for k in ("不要",)) or any(k in msg for k in ("不确认", "暂不确认", "不发布", "先不发布"))
            and not has_task_domain_signal
        )
        is_pure_meta_query = (
            any(k in msg for k in ("为什么", "什么是", "凭什么", "帮助", "规则"))
            and not has_task_domain_signal
        )
        if not is_pure_negation and not is_pure_meta_query:
            return True

    # 3. ── 放宽：自然语言任务创建意图 ──
    natural_creation_patterns = (
        r"(?:我想|想|要|准备|打算|计划|帮我|请|给我|需要|让|叫|派|安排|请让|请叫|请派|请安排)\s*(?:去|做|弄|搞|来|整|规划|安排|执行|完成|开始|进行)?\s*(?:个|个水下|一个)?\s*(?:管缆|管道|油气|水下|ROV|AUV|)?\s*(?:巡检|埋设|采集|勘探|作业|任务|操作|阀门|检查|检测|探测|扫测|维修|维护|清洗|修理)",
        r"(?:管缆|管道|油气|水下)?\s*(?:巡检|埋设|采集|勘探|作业|任务|阀门操作|检查|检测|探测|扫测|维修|维护|清洗)\s*(?:吧|好了|一下|的话|呢)?\s*(?:明天|今天|后天|下午|上午)?",
        r"(?:开始|做|弄|搞|去|执行|来|完成|进行)\s*(?:管缆|管道|油气|水下|ROV|AUV|)?\s*(?:巡检|埋设|采集|勘探|作业|阀门|检查|检测|探测|扫测|维修|维护|清洗)",
    )
    for pat in natural_creation_patterns:
        if re.search(pat, msg):
            return True

    has_natural_creation_keyword = any(
        phrase in msg
        for phrase in (
            "做个巡检", "做个管缆", "做个任务", "弄个巡检", "搞个巡检", "去巡检吧", "去作业吧",
            "做巡检吧", "来个巡检", "安排个巡检", "规划个巡检", "做管道巡检", "做管缆巡检",
            "我要巡检", "想做巡检", "要做巡检", "准备巡检", "准备做巡检",
            "我做巡检", "我作业", "我去巡检", "去检查管道", "去检查一下", "去扫测一下",
            "检查管道", "检查一下管道", "执行巡检", "进行巡检", "完成巡检", "开始巡检",
        )
    )
    if has_natural_creation_keyword:
        return True

    # 3.5 ── 额外：主语指定型任务创建（让 X 去 Y） 如 "让机器人 A 去检查管道" ──
    explicit_actor_command = bool(
        re.search(
            r"(?:让|请|帮|叫|派|安排)\s*(?:机器人|设备|ROV|AUV|金牛座|天鹰座|水蛟|海马|CRAWLER|LROV|观察级|工作级|亚特兰蒂斯|[A-Z]号机|机器人\s*[A-Z])"
            r"\s*(?:去|来|开始|执行|做|进行|完成)\s*(?:检查|巡检|检测|探测|扫测|维修|维护|清洗|作业|任务|阀门|埋设|采集|勘探|管道)",
            msg,
        )
    )
    if explicit_actor_command:
        return True

    # 4. 参数修改/填写：放宽
    has_modify_verb = bool(
        re.search(
            r"(?:改成|改到|改为|设为|设置为|修改为|修改到|变更为|调整为|调整到|调整至|切换为|换成|指定为|为\s*[0-9]+|调(?:到|为|整)|改(?:成|为|到))",
            msg,
        )
    ) or any(k in msg for k in ("取消修改", "撤销修改", "取消水深修改", "不修改", "取消更新"))
    if has_modify_verb:
        return True

    # ── 放宽：数字+参数名 或 上下文有任务时的纯数字 ──
    has_numeric_assignment = bool(
        re.search(
            r"(?:水深|深度|开始时间|结束时间|水温|时间|米|m|度)\s*[:：等于为是约大概\s]*[0-9]+",
            msg,
        )
    )
    if has_numeric_assignment:
        return True

    task_context_exists = bool(
        (task_state and (
            task_state.get("task_type_key") or task_state.get("task_type") or task_state.get("equipment_family")
        )) or (expected_slots and len(expected_slots) > 0)
    )
    if task_context_exists and re.search(r"[0-9]{2,}", msg) and not is_pure_knowledge_query:
        return True

    has_explicit_field_assignment = bool(
        re.search(
            r"(?:水深|深度|开始时间|结束时间|管缆位置|管缆类型|支持船|机器人|设备|工具|载荷|井口|油田)\s*[:：等于为是就用选]\s*[\u4e00-\u9fa5A-Za-z0-9_\-\.\:]+",
            msg,
        )
    )
    if has_explicit_field_assignment:
        return True

    # ── 放宽：口语化设备选择 ──
    device_selection_patterns = (
        r"(?:用|选|选择|使用|采用|就用|选个|挑个|拿个)\s*(?:[A-Za-z0-9_\-]+|金牛座|天鹰座|水蛟|海马|CRAWLER|LROV|1600|WORK|观察级|工作级|亚特兰蒂斯|深海|OBSROV-\d+)",
        r"(?:金牛座|天鹰座|水蛟|海马|CRAWLER|LROV|1600|WORK|观察级|工作级|亚特兰蒂斯|深海)\s*(?:吧|好了|就可以|就行|够用)",
    )
    for pat in device_selection_patterns:
        if re.search(pat, msg):
            return True

    has_device_use_cmd = bool(
        re.search(
            r"(?:使用|选用|选择|采用|搭载|配备|换成)\s*(?:[A-Za-z0-9_\-]+|金牛座|天鹰座|水蛟|海马|CRAWLER|LROV|1600|WORK|观察级|工作级|亚特兰蒂斯|OBSROV-\d+)",
            msg,
        )
    )
    if has_device_use_cmd:
        return True

    # ── 放宽：多设备组合（隐含任务创建/设备配置）
    #    例如 "ROV和AUV我都想用一下"、"金牛座和天鹰座都配" ──
    multi_device_patterns = (
        # 设备A 和/跟/与 设备B (都)? (想|要|用|选|配|搭载|需要)...
        r"(?:ROV|AUV|机器人|CRAWLER|LROV|金牛座|天鹰座|水蛟|海马|观察级|工作级|OBSROV-\d+|支持船)\s*(?:和|跟|与|加|及)\s*(?:ROV|AUV|机器人|CRAWLER|LROV|金牛座|天鹰座|水蛟|海马|观察级|工作级|OBSROV-\d+|支持船)\s*(?:.*)?(?:都|全|一起|一块儿|同时)?\s*(?:想|要|用|选|配|搭配|装备|带上|需要|安排)",
    )
    for pat in multi_device_patterns:
        if re.search(pat, msg):
            return True

    # ── 放宽：设备类别词 + 想用/要用/想选 等口语化配置意图 ──
    colloquial_equipment_intent = bool(
        re.search(
            r"(?:ROV|AUV|机器人|CRAWLER|LROV|金牛座|天鹰座|水蛟|海马|观察级|工作级|OBSROV-\d+|支持船|机械臂|声呐|摄像机)\s*(?:.*)?(?:我想|我要|想|要|准备|打算|计划)\s*(?:用|选|配|装备|安排)",
            msg,
        )
    )
    if colloquial_equipment_intent:
        return True

    # 5. 增删替换证据
    has_add_delete_replace = (
        any(v in msg for v in ("增加", "添加", "加上", "带上", "配备", "搭载", "安装", "配置", "配", "挂载", "加个", "加一套"))
        and any(n in msg for n in ("摄像机", "机械臂", "声呐", "声纳", "抓手", "传感器", "工具", "载荷", "payload", "激光", "测距仪", "流速仪", "高度计", "水听器", "高清"))
    ) or (
        any(v in msg for v in ("删除", "移除", "去掉", "取消", "卸载", "拿掉", "不要"))
        and any(n in msg for n in ("侧扫声呐", "摄像机", "机械臂", "抓手", "传感器", "工具", "载荷", "payload", "激光", "测距仪", "流速仪", "高度计", "水听器"))
    ) or (
        any(v in msg for v in ("替换", "换成", "更换", "换个", "换一套"))
        and any(n in msg for n in ("天鹰座", "金牛座", "水蛟", "海马", "机器人", "设备", "支持船", "A", "B", "C"))
    )
    if has_add_delete_replace:
        return True

    # 6. 确认/发布证据
    if any(k in msg for k in (
        "确认这个修改", "使用这个值", "改为这个型号", "确认修改", "确认使用",
        "确认发布", "确认开始", "确认无误", "确认", "就这个", "就这样", "就用这个", "没问题就这样",
        "好的就这样", "可以就这样",
    )):
        return True

    # ── 放宽：LLM 先提出 WRITE 且有 task_state/expected_slots 上下文时，
    #    若包含设备系列名/油田名/电缆名等领域词，允许通过（让 Extractor 后续判断能否抽取）──
    if plan_candidate is not None and task_context_exists:
        domain_keywords = (
            "管缆", "管道", "电力", "光纤", "油气", "海底",
            "金牛座", "天鹰座", "水蛟", "海马", "CRAWLER", "LROV", "观察级", "工作级", "亚特兰蒂斯", "深海", "1600", "WORK",
            "机械臂", "摄像机", "声呐", "声纳", "抓手", "传感器",
            "支持船", "井口", "油田",
            "北纬", "东经", "纬度", "经度",
        )
        if any(kw in msg for kw in domain_keywords):
            return True

    return False


def validate_interaction_plan(
    plan_candidate: Any,
    user_message: str = "",
    context: dict | None = None,
) -> InteractionPlan:
    """
    LLM-First 架构下的后端校验器：
    安全边界（operation/dialogue_mode/CONTROL action）严格校验，
    描述性字段（subject_type/relation/source_policy）采用后端规则自动补齐，
    避免因 LLM 输出枚举值略有偏差而整案降级 CLARIFY。
    """
    if isinstance(plan_candidate, InteractionPlan):
        data = plan_candidate.to_dict()
    elif isinstance(plan_candidate, dict):
        data = dict(plan_candidate)
    else:
        logger.warning("[validate_interaction_plan] 候选 Plan 类型非法: %r", type(plan_candidate))
        return build_clarify_fallback_plan("候选 Plan 非 dict 或 InteractionPlan 结构")

    try:
        # ═══ 额外：LLM 返回的字段元数据类型校验（fail-closed 到 CLARIFY，但仅针对显式被污染的场景）
        # 校验：query_subtype、query_intent、intent 等如果是 list/dict/bool/number（非 str），
        # 说明协议/攻击注入，Fail-Closed 为 CLARIFY
        TAINTED_FIELD_KEYS = ("query_subtype", "query_intent", "intent", "interaction_type")
        for tk in TAINTED_FIELD_KEYS:
            tv = data.get(tk)
            if tv is not None and not isinstance(tv, str):
                return build_clarify_fallback_plan(
                    f"LLM 返回字段 {tk!r} 类型非法（应为 str，实际 {type(tv).__name__}），Fail-Closed 至 CLARIFY"
                )

        conf = data.get("confidence")
        if conf is None or isinstance(conf, bool) or not isinstance(conf, (int, float)):
            conf_float = 0.7  # 放宽：缺失 confidence 时默认 0.7，不强制降级
        else:
            # NaN / Inf 判定为 confidence 非法 → Fail-Closed 到 CLARIFY（安全协议兼容）
            try:
                cf = float(conf)
            except (TypeError, ValueError):
                return build_clarify_fallback_plan(
                    f"LLM 返回 confidence={conf!r} 无法转换为数字，Fail-Closed 至 CLARIFY"
                )
            if not math.isfinite(cf):
                return build_clarify_fallback_plan(
                    f"LLM 返回 confidence={cf!r}（NaN/Inf 非法值），Fail-Closed 至 CLARIFY"
                )
            conf_float = cf
            conf_float = max(0.0, min(1.0, conf_float))
        # 置信度阈值从 0.6 下调至 0.3 —— 允许 LLM 低置信度试探性表达，由后端进一步校验
        if conf_float < 0.3:
            conf_float = 0.5

        op = data.get("operation")
        if not isinstance(op, str) or op not in VALID_OPERATIONS:
            # 放宽：operation 非法时基于上下文智能推断，而非直接 CLARIFY
            has_task_signal = (
                isinstance(context, dict) and (
                    context.get("task_type_key") or context.get("has_task")
                )
            )
            if isinstance(context, dict) and context.get("expected_slots"):
                op = "WRITE"
            elif has_task_signal:
                op = "WRITE"
            else:
                op = "READ"

        dm = data.get("dialogue_mode")
        if not isinstance(dm, str) or dm not in VALID_DIALOGUE_MODES:
            # 放宽：根据 operation 补全合理的 dialogue_mode
            if op == "WRITE":
                dm = "task_collection"
            elif op == "CONTROL":
                dm = "emergency_intervention"
            else:
                dm = "knowledge_qa"

        # ── 放宽：subject_type / relation / source_policy 枚举不强制校验，
        #    使用后端规则从用户文本自动推导补齐 ──
        st_raw = data.get("subject_type")
        st = st_raw if (isinstance(st_raw, str) and st_raw in VALID_SUBJECT_TYPES) else None

        rel_raw = data.get("relation")
        rel = rel_raw if (isinstance(rel_raw, str) and rel_raw in VALID_RELATIONS) else None

        sp_raw = data.get("source_policy")
        sp = sp_raw if (isinstance(sp_raw, str) and sp_raw in VALID_SOURCE_POLICIES) else None

        # 自动补齐未匹配的描述性字段
        if st is None or rel is None or sp is None:
            from .intent_router import _extract_subject_relation_policy
            task_state = (
                (context or {}).get("task_state")
                if isinstance(context, dict)
                else None
            )
            query_intent_hint = data.get("query_intent")
            inferred_st, inferred_stext, inferred_rel, inferred_sp = _extract_subject_relation_policy(
                user_message, query_intent_hint, task_state or {}
            )
            if st is None:
                st = inferred_st or "general_concept"
            if rel is None:
                rel = inferred_rel or "describe"
            if sp is None:
                sp = inferred_sp or "project_kb"

        act = data.get("emergency_action")
        if act is not None and (not isinstance(act, str) or act not in VALID_EMERGENCY_ACTIONS):
            act = None  # 安全：非法 emergency_action 置空而非整体降级

        # 2. 逻辑约束与 Evidence Gate 校验
        if op == "CONTROL":
            if dm != "emergency_intervention" or act not in VALID_EMERGENCY_ACTIONS:
                return build_clarify_fallback_plan("CONTROL 模式缺少合法 emergency_action 或 dialogue_mode 矛盾")

        elif op == "READ":
            if dm != "knowledge_qa":
                return build_clarify_fallback_plan(f"READ 模式的 dialogue_mode 必须为 knowledge_qa，实际为 {dm!r}")
            if act is not None:
                return build_clarify_fallback_plan("READ 模式不可包含可执行控制动作")

        elif op == "WRITE":
            if dm != "task_collection":
                return build_clarify_fallback_plan(f"WRITE 模式的 dialogue_mode 必须为 task_collection，实际为 {dm!r}")
            if act is not None:
                return build_clarify_fallback_plan("WRITE 模式不可包含紧急控制动作")

            # 后端确定性 WRITE Evidence Gate 强校验
            expected_slots = context.get("expected_slots") if isinstance(context, dict) else None
            task_state = (
                context.get("task_state") or context.get("filled_slots")
                if isinstance(context, dict)
                else None
            )
            if not has_write_evidence(
                user_message,
                expected_slots=expected_slots,
                task_state=task_state,
                plan_candidate=data,
            ):
                return build_clarify_fallback_plan(
                    "WRITE candidate lacks deterministic write evidence",
                    reason_code="WRITE_EVIDENCE_MISSING",
                    confidence=conf_float,
                )

        elif op == "CLARIFY":
            if dm != "knowledge_qa":
                return build_clarify_fallback_plan(f"CLARIFY 模式的 dialogue_mode 必须为 knowledge_qa，实际为 {dm!r}")

        needs_clarify = bool(data.get("needs_clarification", False))
        if op == "CLARIFY":
            needs_clarify = True

        reason_code = str(data.get("reason_code") or "OK").strip()
        clarification_reason = data.get("clarification_reason")
        if clarification_reason is not None and not isinstance(clarification_reason, str):
            clarification_reason = str(clarification_reason)

        query_intent = data.get("query_intent")
        if query_intent is not None and not isinstance(query_intent, str):
            query_intent = str(query_intent)

        subject_text = data.get("subject_text")
        if subject_text is not None and not isinstance(subject_text, str):
            subject_text = str(subject_text)

        schema_ver = int(data.get("schema_version", 1))

        return InteractionPlan(
            schema_version=schema_ver,
            operation=op,  # type: ignore
            dialogue_mode=dm,  # type: ignore
            query_intent=query_intent,
            subject_type=st,  # type: ignore
            subject_text=subject_text,
            relation=rel,  # type: ignore
            source_policy=sp,  # type: ignore
            needs_clarification=needs_clarify,
            clarification_reason=clarification_reason,
            emergency_action=act if op == "CONTROL" else None,
            confidence=conf_float,
            reason_code=reason_code,
        )
    except Exception as exc:
        logger.warning("[validate_interaction_plan] 校验过程抛出异常: %s", exc)
        return build_clarify_fallback_plan(f"校验异常: {exc}")
