"""模型驱动的交互路由协议适配器。

IntentRouter 只负责把统一会话上下文交给模型，并验证模型返回的
InteractionPlan。它不读取用户原句重新猜测 READ/WRITE/CONTROL/CLARIFY。
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from typing import Any, Literal

from .interaction_plan import (
    InteractionPlan,
    build_clarify_fallback_plan,
    validate_interaction_plan,
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
VALID_EMERGENCY_ACTIONS = {"stop", "pause", "abort", "cancel"}


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


class IntentRoutingError(Exception):
    """模型调用或 InteractionPlan 协议识别失败。"""


@dataclass(frozen=True)
class IntentRouteResult:
    """DialogueManager 兼容路由结果。

    READ、CLARIFY 和 CONTROL 仍映射为旧接口的 QUERY；真正的 operation 保留在
    ``interaction_plan``，CONTROL 通过 emergency_intervention 单独执行。
    """

    interaction_type: InteractionType
    confidence: float
    reason: str
    query_intent: str | None = None
    dialogue_mode: DialogueMode = "task_collection"
    source: str = "interaction_plan"
    emergency_action: str | None = None
    interaction_plan: InteractionPlan | None = None

    def __post_init__(self) -> None:
        interaction_type = str(self.interaction_type).strip().upper()
        dialogue_mode = str(self.dialogue_mode).strip().lower()
        query_intent = (
            str(self.query_intent).strip().upper() if self.query_intent else None
        )

        if interaction_type not in VALID_INTERACTION_TYPES:
            raise ValueError(f"非法 interaction_type: {self.interaction_type}")
        if dialogue_mode not in VALID_DIALOGUE_MODES:
            raise ValueError(f"非法 dialogue_mode: {self.dialogue_mode}")
        if (
            isinstance(self.confidence, bool)
            or not isinstance(self.confidence, (int, float))
            or not math.isfinite(float(self.confidence))
            or not 0.0 <= float(self.confidence) <= 1.0
        ):
            raise ValueError(f"非法 confidence: {self.confidence!r}")

        action = self.emergency_action
        invalid_action = action is not None and action not in VALID_EMERGENCY_ACTIONS
        contradictory_action = (
            action in VALID_EMERGENCY_ACTIONS
            and dialogue_mode != "emergency_intervention"
        )
        missing_action = (
            dialogue_mode == "emergency_intervention"
            and action not in VALID_EMERGENCY_ACTIONS
        )
        if invalid_action or contradictory_action or missing_action:
            # 兼容调用方可能直接构造旧路由结果；协议矛盾只允许安全降级，不能
            # 因异常绕过 DialogueManager 的只读路径。
            dialogue_mode = "knowledge_qa"
            interaction_type = "QUERY"
            query_intent = "CLARIFICATION"
            action = None
            object.__setattr__(self, "emergency_action", None)

        if dialogue_mode == "emergency_intervention":
            interaction_type = "QUERY"
            query_intent = None
        elif dialogue_mode == "task_collection":
            interaction_type = "WRITE"
            query_intent = None
        else:
            interaction_type = "QUERY"
            if query_intent not in VALID_QUERY_INTENTS:
                query_intent = "KNOWLEDGE_QA"

        object.__setattr__(self, "interaction_type", interaction_type)
        object.__setattr__(self, "dialogue_mode", dialogue_mode)
        object.__setattr__(self, "query_intent", query_intent)
        object.__setattr__(self, "confidence", float(self.confidence))

    def to_dict(self) -> dict[str, Any]:
        result = {
            "interaction_type": self.interaction_type,
            "confidence": self.confidence,
            "reason": self.reason,
            "query_intent": self.query_intent,
            "dialogue_mode": self.dialogue_mode,
            "source": self.source,
            "emergency_action": self.emergency_action,
        }
        if self.interaction_plan is not None:
            result["interaction_plan"] = self.interaction_plan.to_dict()
        return result

    @property
    def intent(self) -> str | None:
        if self.dialogue_mode == "task_collection":
            return "TASK_UPDATE"
        if self.dialogue_mode == "emergency_intervention":
            return "EMERGENCY_INTERVENTION"
        return self.query_intent or self.interaction_type

    @property
    def is_query(self) -> bool:
        return self.dialogue_mode != "task_collection"

    @property
    def should_update_slots(self) -> bool:
        return self.dialogue_mode == "task_collection"


class IntentRouter:
    """每轮调用一次模型规划；任何失败均返回无副作用澄清计划。"""

    def __init__(self, llm: LLMClient):
        self.llm = llm

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
        if plan.reason_code == "VALIDATION_FALLBACK_CLARIFY":
            logger.warning(
                "[IntentRouter] InteractionPlan 协议非法: %s",
                plan.clarification_reason,
            )
        return plan.to_intent_route_result()
