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
    # IntentRouter 边界修复：这个 gate 只判断“是否允许进入写入/抽取流程”，不再含业务词典；
    # 为什么：任务类型语义归属必须交给 Extractor + schema catalog，避免“采油树”等词在路由层被硬编码写入。
    """
    LLM-First 架构下的 WRITE safety gate。

    这里只判断用户交互行为是否允许进入 Extractor，不判断任务类型或业务字段。
    task_type 语义识别必须交给 Extractor + Schema catalog + Normalizer。
    """
    msg = (user_message or "").strip()
    if not msg:
        return False

    has_expected_slot = bool(expected_slots)
    task_context_exists = bool(
        (task_state and (task_state.get("task_type_key") or task_state.get("task_type")))
        or has_expected_slot
    )

    is_hypothetical = any(
        marker in msg
        for marker in ("如果", "要是", "假如", "假使", "若", "假设", "万一")
    )
    is_question = bool(re.search(r"[呢吗？?]$", msg)) or any(
        q in msg
        for q in (
            "想知道",
            "想了解",
            "我要知道",
            "我要了解",
            "帮我查",
            "查一下",
            "查看",
            "查询",
            "介绍",
            "说明",
            "告诉我",
            "为什么",
            "什么是",
            "是什么",
            "怎么办",
            "如何",
            "怎么",
            "能不能",
            "可不可以",
            "是否",
            "会不会",
            "有没有",
            "有哪些",
            "多少",
            "区别",
            "差异",
            "影响",
            "后果",
            "风险",
        )
    )
    if is_hypothetical or is_question:
        return False

    if has_expected_slot:
        is_negation_or_publish_refusal = any(
            token in msg
            for token in ("不确认", "暂不确认", "不发布", "先不发布", "不要", "不用")
        )
        is_meta_question = any(
            token in msg
            for token in ("为什么", "什么是", "凭什么", "帮助", "规则")
        )
        return not (is_negation_or_publish_refusal or is_meta_question)

    explicit_write_patterns = (
        r"(?:创建|新建|发起|生成|建立|登记|新增|开)\s*(?:一个|个|一下)?",
        r"(?:我要|我想|想要|准备|打算|计划|需要|帮我|请|给我|安排)\s*(?:做|弄|搞|执行|开始|进行|完成|安排|规划)",
        r"(?:改成|改到|改为|设为|设置为|修改为|修改到|变更为|调整为|调整到|调整至|切换为|换成|替换为|指定为)",
        r"(?:增加|添加|加上|带上|配备|搭载|安装|配置|挂载|删除|移除|去掉|清空|清掉)",
        r"(?:确认发布|确认开始|确认无误|确认修改|确认使用|就这个|就这样|就用这个)",
    )
    if any(re.search(pattern, msg, flags=re.IGNORECASE) for pattern in explicit_write_patterns):
        return True

    assignment_pattern = (
        r"[\u4e00-\u9fa5A-Za-z0-9_（）()]+"
        r"\s*(?:[:：=]|等于|为|是|就用|选|使用)"
        r"\s*[\u4e00-\u9fa5A-Za-z0-9_\-\.\:/、]+"
    )
    if re.search(assignment_pattern, msg):
        return True

    if task_context_exists and re.search(r"\d+(?:\.\d+)?\s*(?:米|m|小时|分钟|点)?", msg, re.IGNORECASE):
        return True

    if plan_candidate is None:
        return False

    operation = None
    confidence = 0.0
    if isinstance(plan_candidate, InteractionPlan):
        operation = plan_candidate.operation
        confidence = plan_candidate.confidence
    elif isinstance(plan_candidate, dict):
        operation = plan_candidate.get("operation") or plan_candidate.get("interaction_type") or plan_candidate.get("intent")
        try:
            confidence = float(plan_candidate.get("confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0

    if str(operation or "").upper() in {"WRITE", "TASK_CREATE", "TASK_UPDATE", "CREATE", "UPDATE"}:
        return confidence >= 0.3

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
