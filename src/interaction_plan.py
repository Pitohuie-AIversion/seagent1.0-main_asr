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
    后端确定性 WRITE Evidence Gate 纯函数。

    用于验证 LLM 提出的 WRITE 候选 Plan 是否在当前用户输入和 session context 中存在真实写入任务状态的证据。
    至少满足以下确定性证据之一才允许判定为 WRITE：
    A. 明确创建任务 ("创建一个管缆巡检任务", "我想做管缆巡检")
    B. 明确修改/填写任务参数 ("水深改成500米", "水深300米", "支持船换成XXX")
    C. 明确增删替换任务字段 ("增加高清摄像机", "移除摄像机", "把机器人替换为天鹰座", "使用天鹰座")
    D. expected_slot 追问的直接回答 ("海底油气管道")
    E. 明确确认候选修改 ("确认这个修改", "使用这个值", "确认发布")

    对于单纯询问/疑问句（"介绍一下新型号X"、"ROV可以在500米工作吗"、"哪些机器人支持机械臂"），
    即使包含数字、设备名或 payload，均不计为 WRITE evidence。
    """
    msg = (user_message or "").strip()
    if not msg:
        return False

    # 1. 纯条件句与问句拦截
    is_conditional_question = any(
        cond in msg for cond in ("如果", "要是", "假如", "假使", "若", "假设", "万一")
    ) and any(
        q in msg for q in ("会怎样", "有什么影响", "有何影响", "结果如何", "怎么办", "如何", "怎么", "吗", "呢", "？", "?")
    )
    if is_conditional_question:
        return False

    has_write_verb = any(
        kw in msg for kw in (
            "改成", "设为", "设置为", "替换为", "调整为", "切换为", "换成", "指定为",
            "创建一个", "新建一个", "发起一个", "帮我发起", "我要执行", "增加高清摄像机", "添加机械臂",
            "确认发布", "确认开始", "确认无误", "去检查", "去巡检", "去操作", "去埋设", "让", "执行"
        )
    )

    is_pure_query = (
        bool(re.search(r"[呢吗？?]$", msg))
        or any(
            q in msg
            for q in (
                "介绍", "说明", "什么是", "为何", "为什么", "含义", "概念", "区别", "差异", "不同",
                "有哪些", "属于哪个", "属于", "搭载哪些", "支持哪些", "适合作业", "工作吗", "作业吗",
                "能做什么", "可以做什么", "最大水深是", "最大水深多少", "有什么影响", "会发生什么"
            )
        )
    ) and not has_write_verb

    if is_pure_query:
        return False

    # 2. Expected Slot 追问回答校验
    if expected_slots and len(expected_slots) > 0:
        is_negation_or_control = any(
            k in msg for k in ("不确认", "不发布", "不要", "暂不", "取消", "停止", "暂停", "终止")
        )
        is_meta_query = any(
            k in msg for k in ("为什么", "什么是", "凭什么", "哪些", "介绍", "帮助", "规则")
        )
        if not is_negation_or_control and not is_meta_query:
            return True

    # 3. A. 明确任务创建证据
    has_creation = bool(
        re.search(
            r"(?:我想|想|要|准备|帮我|请)?(?:创建|新建|发起|做|执行|进行|规划)\s*(?:一个|一条)?\s*(?:管缆|管道|油气|水下|ROV|AUV)?\s*(?:巡检|埋设|采集|勘探|作业|任务)",
            msg,
        )
    ) or any(
        phrase in msg
        for phrase in (
            "创建", "新建", "发起", "我要执行", "帮我发起", "开始规划", "新建巡检",
            "创建任务", "新建任务", "发起任务", "做个巡检", "做一个巡检", "进行巡检", "去巡检", "去埋设",
            "让观察级机器人", "让工作级机器人", "让机器人", "去检查", "去操作", "执行"
        )
    )
    if has_creation:
        return True

    # 4. B. 明确任务参数修改/填写证据
    has_explicit_modify_verb = bool(
        re.search(r"(?:改成|设为|设置为|替换为|调整为|切换为|换成|指定为|为\s*[0-9]+)", msg)
    )
    if has_explicit_modify_verb:
        return True

    has_param_assignment = bool(
        re.search(
            r"(?:水深|深度|开始时间|结束时间|管缆位置|管缆类型|支持船|机器人|设备|工具|载荷|井口|油田)\s*[:：等于为是\s]*[\u4e00-\u9fa5A-Za-z0-9_\-\.\:]+",
            msg,
        )
    )
    if has_param_assignment:
        return True

    has_device_use_cmd = bool(
        re.search(
            r"(?:使用|选用|选择|采用|搭载|配备)\s*(?:[A-Za-z0-9_\-]+|金牛座|天鹰座|水蛟|海马|CRAWLER|LROV|1600|WORK|观察级|工作级|亚特兰蒂斯|OBSROV-\d+|机器人|设备)",
            msg,
        )
    )
    if has_device_use_cmd:
        return True

    # 5. C. 明确增删替换任务字段证据
    has_add_delete_replace = (
        any(v in msg for v in ("增加", "添加", "加上", "带上", "配备", "搭载"))
        and any(n in msg for n in ("摄像机", "机械臂", "声呐", "声纳", "抓手", "传感器", "工具", "载荷", "payload"))
    ) or (
        any(v in msg for v in ("删除", "移除", "去掉", "取消"))
        and any(n in msg for n in ("侧扫声呐", "摄像机", "机械臂", "抓手", "传感器", "工具", "载荷", "payload"))
    ) or (
        any(v in msg for v in ("替换", "换成", "更换"))
        and any(n in msg for n in ("天鹰座", "金牛座", "水蛟", "海马", "机器人", "设备", "支持船", "A", "B", "C"))
    )
    if has_add_delete_replace:
        return True

    # 6. E. 明确候选/确认发布证据
    if any(k in msg for k in ("确认这个修改", "使用这个值", "改为这个型号", "确认修改", "确认使用", "确认发布", "确认开始", "确认无误", "确认")):
        return True

    return False


def validate_interaction_plan(
    plan_candidate: Any,
    user_message: str = "",
    context: dict | None = None,
) -> InteractionPlan:
    """
    后端确定性校验器（含 WRITE Evidence Gate 强校验）。

    校验项包括：
    1. 字段存在性与数据类型（避免 LLM 输出缺失/类型错误）
    2. 数值有限性 (Confidence 无 NaN / Inf / 越界)
    3. 枚举合法性 (operation, dialogue_mode, subject_type, relation, source_policy)
    4. 逻辑一致性：
       - CONTROL 必须有合法的 emergency_action ('stop', 'pause', 'abort', 'cancel')
       - READ 严禁包含可执行紧急控制动作
       - WRITE 必须匹配 task_collection，不可与 knowledge_qa 冲突；且必须通过 WRITE Evidence Gate 校验
       - CLARIFY 必须为 knowledge_qa 且 needs_clarification = True
    5. 置信度校验（< 0.6 安全降级）

    所有不合法的 Plan 统一 Fail-Closed 降级至 CLARIFY。
    """
    if isinstance(plan_candidate, InteractionPlan):
        data = plan_candidate.to_dict()
    elif isinstance(plan_candidate, dict):
        data = dict(plan_candidate)
    else:
        logger.warning("[validate_interaction_plan] 候选 Plan 类型非法: %r", type(plan_candidate))
        return build_clarify_fallback_plan("候选 Plan 非 dict 或 InteractionPlan 结构")

    # 1. 基础 schema 字段检查
    try:
        conf = data.get("confidence")
        if conf is None or isinstance(conf, bool) or not isinstance(conf, (int, float)):
            return build_clarify_fallback_plan("confidence 缺失或类型非法")
        conf_float = float(conf)
        if not math.isfinite(conf_float) or not (0.0 <= conf_float <= 1.0):
            return build_clarify_fallback_plan(f"confidence 数值非法: {conf_float}")
        if conf_float < 0.6:
            return build_clarify_fallback_plan(f"置信度过低({conf_float:.2f})")

        op = data.get("operation")
        if not isinstance(op, str) or op not in VALID_OPERATIONS:
            return build_clarify_fallback_plan(f"operation 非法: {op!r}")

        dm = data.get("dialogue_mode")
        if not isinstance(dm, str) or dm not in VALID_DIALOGUE_MODES:
            return build_clarify_fallback_plan(f"dialogue_mode 非法: {dm!r}")

        st = data.get("subject_type")
        if st is not None and (not isinstance(st, str) or st not in VALID_SUBJECT_TYPES):
            return build_clarify_fallback_plan(f"subject_type 非法: {st!r}")

        rel = data.get("relation")
        if rel is not None and (not isinstance(rel, str) or rel not in VALID_RELATIONS):
            return build_clarify_fallback_plan(f"relation 非法: {rel!r}")

        sp = data.get("source_policy")
        if not isinstance(sp, str) or sp not in VALID_SOURCE_POLICIES:
            return build_clarify_fallback_plan(f"source_policy 非法: {sp!r}")

        act = data.get("emergency_action")
        if act is not None and (not isinstance(act, str) or act not in VALID_EMERGENCY_ACTIONS):
            return build_clarify_fallback_plan(f"emergency_action 非法: {act!r}")

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
