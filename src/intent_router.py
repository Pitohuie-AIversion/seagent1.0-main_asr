"""模型驱动的交互路由协议适配器。

IntentRouter 只负责把统一会话上下文交给模型，并验证模型返回的
InteractionPlan。它不读取用户原句重新猜测 READ/WRITE/CONTROL/CLARIFY。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from .interaction_plan import (
    build_clarify_fallback_plan,
    validate_interaction_plan,
)
from .llm_client import LLMClient
from .model_profile import ModelRole, _is_unsupported_role_keyword_error
from .types import (  # noqa: F401  (re-export backwards compat)
    InteractionType,
    DialogueMode,
    VALID_INTERACTION_TYPES,
    VALID_DIALOGUE_MODES,
    VALID_QUERY_INTENTS,
    VALID_EMERGENCY_ACTIONS,
    IntentRoutingError,
    IntentRouteResult,
)

logger = logging.getLogger(__name__)


INTENT_ROUTER_SYSTEM = """\
你是 SEAgent 的 TurnPlanner。请结合最新输入、历史消息和当前任务状态，判断本轮
用户真正想做的事情，并只输出一个 schema_version=1 的 JSON object。

operation 只能是：
- READ：只回答问题或聊天，不修改任务；
- WRITE：创建任务、补充或修改任务信息；即使同时要求解释，也选择 WRITE；
- CONTROL：请求停止、暂停、终止或取消运行控制；
- CLARIFY：上下文仍不足以安全判断。

不要依赖固定句式。需要理解省略、指代、对上一轮建议的接受、任务中途闲聊，以及
同一句中的问答和修改。已有任务或 expected_slots 不代表本轮一定要写入；反过来，
自然表达没有出现字段名也不代表不能写入。
用户可能使用 expected_slot_options 中 allowed_values 的别名、代号或简称（例如 alias_mappings 中收录的“天鹰座”、“金牛座”等系列代号，或“天鹰座001”等单机代号）。当用户明确表达使用/选择某个合法别称或代号（如“我要使用天鹰座”、“选金牛座”、“用天鹰座001”）时，属于明确指定任务参数，必须判定为 WRITE。
当当前待填字段不包含 equipment_class，而用户输入精确命中 expected_slot_options 中某个
allowed_values 或 alias_mappings 时，应按该待填字段候选处理为 WRITE；不要因措辞
像“ROV 类别”就退回 device_class 澄清。
询问推荐本身属于 READ，不得因为问题中出现任务字段或“选择”语义就修改任务；
接受上一轮助手明确给出的单一推荐才属于 WRITE。若上一轮只是并列介绍多个候选、
没有明确推荐，且用户本轮也未指明选择，必须 CLARIFY，不能替用户猜测。
用户用“第三个/选2/最后一个”等序号选择时，只有紧邻上一条助手消息明确展示了
有序候选才属于 WRITE；不得使用 expected_slot_options 的后台顺序替用户选择。

只强制输出 operation。其余字段是可选的语义增强信息：有把握时输出，没有把握可
省略；代码会从 operation 推导 dialogue_mode 和 needs_clarification。推荐、项目
知识检索或控制动作需要相应信息时，应尽量输出相关字段：
{
  "schema_version": 1,
  "operation": "READ|WRITE|CONTROL|CLARIFY",
  "dialogue_mode": "task_collection|knowledge_qa|emergency_intervention",
  "query_intent": "TASK_STATUS|TOOL_QUERY|DEVICE_CAPABILITY|DEVICE_STATUS|ENVIRONMENT_QUERY|KNOWLEDGE_QA|GENERAL_CHAT|CLARIFICATION|null",
  "subject_type": "general_concept|system_rule|task|device|device_class|device_family|payload|environment|realtime_state|unknown",
  "subject_text": "string|null",
  "relation": "definition|describe|list|compare|supports|belongs_to|capabilities|limitations|status|missing_fields|filled_fields|procedure|recommend|unknown",
  "source_policy": "project_kb|session_state|realtime_state|general_domain|hybrid|none",
  "needs_clarification": false,
  "clarification_reason": "string|null",
  "emergency_action": "stop|pause|abort|cancel|null",
  "pending_action": "confirm|reject|null",
  "warning_action": "acknowledge|null",
  "confidence": 0.0,
  "reason_code": "short_machine_readable_code"
}

CONTROL 必须给出 emergency_action；涉及写入或控制且语义不确定时应选择 CLARIFY。
当用户要求系统从当前待填字段的合法候选中推荐一个时，READ 且 relation=recommend；
subject_type 应对应被推荐字段。subject_text 可以是用户的自然描述、候选别名或你
根据候选证据推荐的标准值；执行器会再用 expected_slot_options 的 allowed_values
做受约束语义消歧，最终不得写入候选域外值，也不能把类别、系列、型号或单机编号
混为一层。
当用户仅接受紧邻上一条助手给出的单一推荐时，WRITE 且仍使用
relation=recommend、相同 subject_type 和相同 subject_text。代码会核验该值确实是
上一轮推荐且仍属于当前合法候选；不得额外推导其他机器人层级。
当上下文含 pending_oilfield 时，确认或拒绝该候选都属于 WRITE，并分别设置
pending_action=confirm 或 reject；其他轮次必须为 null。候选选择写入 subject_text，
代码只执行经过协议校验的动作。
当 phase=blocked_soft 且用户明确表示理解并接受当前软警告时，使用 WRITE 并设置
warning_action=acknowledge；这不是紧急控制指令，此时 dialogue_mode 必须为
task_collection，emergency_action 必须为 null，pending_action 必须为 null。
“忽略软警告”“接受风险后继续”绝不能解释成 stop、pause、abort 或 cancel。
例如 blocked_soft 下“忽略当前全部软警告”的关键字段必须是：
{"operation":"WRITE","dialogue_mode":"task_collection",
 "emergency_action":null,"pending_action":null,"warning_action":"acknowledge"}。
询问、解释、比较风险不等于接受风险；“顺便说说风险”“有什么风险”都必须令
warning_action=null。同一轮只要还包含任何任务参数新增、修改或删除，也必须令
warning_action=null，先按普通 WRITE 抽取并校验修改；因为参数变化会使旧警告及
其确认指纹失效。只有用户本轮唯一任务副作用是接受当前已展示的软警告时，才设置
warning_action=acknowledge。
不处于 blocked_soft 或未明确接受风险时 warning_action 必须为 null。
只能输出 JSON。
"""


class IntentRouter:
    """每轮调用一次模型规划；任何失败均返回无副作用澄清计划。"""

    def __init__(self, llm: LLMClient):
        self.llm = llm

    @staticmethod
    def _normalize_selector_text(value: Any) -> str:
        text = str(value or "").strip().lower()
        text = re.sub(r"[\s，。！？!?、；;：:,.]+", "", text)
        return text

    @classmethod
    def _expected_slot_aliases(
        cls,
        expected_slot_options: list[dict[str, Any]],
    ) -> set[str]:
        aliases: set[str] = set()
        for opt in expected_slot_options:
            aliases.update(opt.get("allowed_values") or [])
            alias_map = opt.get("alias_mappings") or {}
            aliases.update(alias_map.keys())
            aliases.update(alias_map.values())

        aliases.update([
            "金牛座", "天鹰座", "御夫座", "凤凰座",
            "金牛座001", "金牛座1号机", "金牛座一号机",
            "天鹰座001", "天鹰座1号机", "天鹰座一号机",
            "御夫座001", "御夫座1号机", "御夫座一号机",
            "OBSROV-75-001", "CRAWLER-1600-001", "WROV-250-001", "LROV-150-001",
        ])
        return {alias for alias in aliases if alias}

    @classmethod
    def _matches_expected_slot_alias(
        cls,
        user_message: str,
        expected_slot_options: list[dict[str, Any]],
    ) -> bool:
        user_norm = cls._normalize_selector_text(user_message)
        if not user_norm:
            return False
        return any(
            user_norm == cls._normalize_selector_text(alias)
            for alias in cls._expected_slot_aliases(expected_slot_options)
        )

    @classmethod
    def _matches_expected_list_selection(
        cls,
        user_message: str,
        expected_slot_options: list[dict[str, Any]],
    ) -> bool:
        if cls._looks_like_read_question(user_message):
            return False
        user_norm = cls._normalize_selector_text(user_message)
        if not user_norm:
            return False

        for opt in expected_slot_options:
            if opt.get("type") != "list":
                continue
            allowed_values = [v for v in opt.get("allowed_values") or [] if v]
            if not allowed_values:
                continue

            matched_values = {
                value
                for value in allowed_values
                if cls._normalize_selector_text(value) in user_norm
            }
            alias_map = opt.get("alias_mappings") or {}
            for alias, canonical in alias_map.items():
                if (
                    canonical in allowed_values
                    and cls._normalize_selector_text(alias) in user_norm
                ):
                    matched_values.add(canonical)

            if len(matched_values) >= 2:
                return True
        return False

    @staticmethod
    def _looks_like_read_question(user_message: str) -> bool:
        return any(
            token in user_message
            for token in [
                "?",
                "？",
                "哪个",
                "哪一个",
                "推荐",
                "适合",
                "区别",
                "比较",
                "能否",
                "可以",
                "是什么",
                "怎么",
                "为什么",
            ]
        )

    def route(
        self,
        user_message: str,
        conversation_history: list[dict],
        task_state: dict,
        phase: str = "collecting",
        expected_slots: list[str] | None = None,
        expected_slot_options: list[dict[str, Any]] | None = None,
    ) -> IntentRouteResult:
        message = (user_message or "").strip()
        if not message:
            raise IntentRoutingError("用户输入为空")

        try:
            return self._call_llm_router(
                user_message=message,
                conversation_history=conversation_history,
                task_state=task_state,
                phase=phase,
                expected_slots=expected_slots or [],
                expected_slot_options=expected_slot_options or [],
            )
        except IntentRoutingError as exc:
            logger.warning(
                "[IntentRouter] 模型规划失败，安全降级为澄清: %s",
                exc,
            )
            return build_clarify_fallback_plan(
                reason=f"暂时无法可靠理解本轮意图，请用户澄清: {exc}",
                reason_code="LLM_ROUTE_FAILURE_CLARIFY",
                confidence=0.5,
            ).to_intent_route_result()

    def _call_llm_router(
        self,
        user_message: str,
        conversation_history: list[dict],
        task_state: dict,
        phase: str,
        expected_slots: list[str],
        expected_slot_options: list[dict[str, Any]],
    ) -> IntentRouteResult:
        classify = getattr(self.llm, "classify_interaction", None)
        if not callable(classify):
            raise IntentRoutingError("LLMClient 缺少 classify_interaction 协议")

        context = {
            "phase": phase,
            "has_task": bool(task_state.get("task_type_key")),
            "task_type": task_state.get("task_type"),
            "task_type_key": task_state.get("task_type_key"),
            "expected_slots": expected_slots,
            "expected_slot_options": expected_slot_options,
            "pending_oilfield": {
                "name": task_state.get("pending_oilfield_name"),
                "candidates": [
                    item.get("name")
                    for item in task_state.get("pending_oilfield_candidates", [])
                    if isinstance(item, dict) and item.get("name")
                ],
            }
            if task_state.get("pending_oilfield_name")
            else None,
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
            *conversation_history[-8:],
            {
                "role": "user",
                "content": (
                    f"【当前上下文状态】{json.dumps(context, ensure_ascii=False)}\n"
                    f"【最新用户输入】{user_message}"
                ),
            },
        ]

        try:
            try:
                candidate = classify(
                    messages,
                    max_tokens=480,
                    role=ModelRole.ROUTER,
                )
            except TypeError as exc:
                if not _is_unsupported_role_keyword_error(exc):
                    raise
                candidate = classify(messages, max_tokens=480)
        except Exception as exc:
            raise IntentRoutingError(f"LLM 调用失败: {exc}") from exc

        if not isinstance(candidate, dict):
            raise IntentRoutingError("LLM 路由结果不是合法 JSON object")

        plan = validate_interaction_plan(candidate)

        # 针对明确包含设备选择/使用意图（如“我要选择金牛座”、“选择金牛座001”）
        # 或短输入精确命中当前待填候选别名的口语修正，防止误判为 READ/CLARIFY。
        if plan.operation in ("READ", "CLARIFY"):
            user_msg_strip = user_message.strip()
            has_select_verb = any(
                v in user_msg_strip
                for v in [
                    "选择",
                    "选",
                    "使用",
                    "用",
                    "配",
                    "配备",
                    "切换",
                    "换成",
                    "安排",
                ]
            )
            all_known_aliases = self._expected_slot_aliases(expected_slot_options)
            explicit_selection = has_select_verb and any(
                alias in user_msg_strip for alias in all_known_aliases
            )
            bare_expected_alias = (
                not self._looks_like_read_question(user_msg_strip)
                and self._matches_expected_slot_alias(
                    user_msg_strip,
                    expected_slot_options,
                )
            )
            expected_list_selection = self._matches_expected_list_selection(
                user_msg_strip,
                expected_slot_options,
            )

            if explicit_selection or bare_expected_alias or expected_list_selection:
                logger.info(
                    "[IntentRouter] Correcting route to WRITE because user "
                    "selected an expected slot alias in: %s",
                    user_message,
                )
                candidate["operation"] = "WRITE"
                candidate["dialogue_mode"] = "task_collection"
                candidate["query_intent"] = None
                candidate["needs_clarification"] = False
                candidate["clarification_reason"] = None
                candidate["reason_code"] = (
                    "EXPECTED_LIST_SELECTION_WRITE_CORRECTION"
                    if expected_list_selection
                    else "EXPECTED_SLOT_ALIAS_WRITE_CORRECTION"
                )
                plan = validate_interaction_plan(candidate)

        if plan.reason_code == "VALIDATION_FALLBACK_CLARIFY":
            logger.warning(
                "[IntentRouter] InteractionPlan 协议非法: %s",
                plan.clarification_reason,
            )
        return plan.to_intent_route_result()
