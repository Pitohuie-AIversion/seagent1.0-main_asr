"""测试专用的显式模型协议夹具。

这些夹具只按测试预先给定的队列返回结果，不读取用户原句，也不模拟关键词
分类器。自然语言语义正确性应由真实模型评测负责；单元测试只验证协议与副作用。
"""

from __future__ import annotations

from collections import deque
from copy import deepcopy
from typing import Any, Iterable

from src.llm_client import LLMClient


def make_plan(
    operation: str,
    *,
    query_intent: str | None = None,
    confidence: float = 0.95,
    emergency_action: str | None = None,
    pending_action: str | None = None,
    warning_action: str | None = None,
    subject_type: str | None = None,
    subject_text: str | None = None,
    relation: str | None = None,
    source_policy: str | None = None,
    needs_clarification: bool | None = None,
    clarification_reason: str | None = None,
    reason_code: str = "TEST_SCRIPTED_PLAN",
) -> dict[str, Any]:
    """创建完整、可覆盖的 InteractionPlan 测试数据。"""
    operation = operation.upper()
    modes = {
        "READ": "knowledge_qa",
        "WRITE": "task_collection",
        "CONTROL": "emergency_intervention",
        "CLARIFY": "knowledge_qa",
    }
    if operation not in modes:
        raise ValueError(f"unsupported test operation: {operation}")

    if query_intent is None:
        query_intent = {
            "READ": "GENERAL_CHAT",
            "CLARIFY": "CLARIFICATION",
        }.get(operation)

    if subject_type is None:
        subject_type = "task" if operation in {"WRITE", "CONTROL"} else "general_concept"
    if relation is None:
        relation = "procedure" if operation == "CONTROL" else "unknown"
    if source_policy is None:
        source_policy = "session_state" if operation in {"WRITE", "CONTROL"} else "general_domain"
    if needs_clarification is None:
        needs_clarification = operation == "CLARIFY"
    if operation == "CLARIFY" and clarification_reason is None:
        clarification_reason = "测试脚本要求澄清"

    return {
        "schema_version": 1,
        "operation": operation,
        "dialogue_mode": modes[operation],
        "query_intent": query_intent,
        "subject_type": subject_type,
        "subject_text": subject_text,
        "relation": relation,
        "source_policy": source_policy,
        "needs_clarification": needs_clarification,
        "clarification_reason": clarification_reason,
        "emergency_action": emergency_action,
        "pending_action": pending_action,
        "warning_action": warning_action,
        "confidence": confidence,
        "reason_code": reason_code,
    }


def empty_extraction(*, unresolved: Iterable[str] = ()) -> dict[str, Any]:
    return {
        "slot_candidates": [],
        "list_mutations": [],
        "unresolved": list(unresolved),
    }


def slot_candidate(
    key: str,
    value: Any,
    *,
    raw_value: Any | None = None,
    raw_key: str | None = None,
    confidence: float = 0.99,
) -> dict[str, Any]:
    """创建符合当前 Extractor 五字段契约的候选值。"""
    return {
        "raw_key": raw_key or key,
        "canonical_key": key,
        "raw_value": value if raw_value is None else raw_value,
        "normalized_value": value,
        "confidence": confidence,
    }


def extraction_result(
    *candidates: dict[str, Any],
    unresolved: Iterable[str] = (),
    list_mutations: Iterable[dict[str, Any]] = (),
) -> dict[str, Any]:
    """创建完整抽取响应，避免测试遗漏协议字段后悄悄空跑。"""
    return {
        "slot_candidates": [deepcopy(item) for item in candidates],
        "list_mutations": [deepcopy(item) for item in list_mutations],
        "unresolved": list(unresolved),
    }


class ScriptedLLM(LLMClient):
    """严格按队列返回模型结果的测试替身；绝不解释用户文本。"""

    def __init__(
        self,
        *,
        plans: Iterable[dict[str, Any]] = (),
        extractions: Iterable[dict[str, Any]] = (),
        replies: Iterable[str] = (),
        default_plan: dict[str, Any] | None = None,
        default_extraction: dict[str, Any] | None = None,
        default_reply: str = "测试回复",
    ) -> None:
        super().__init__(None, None)
        self.plans = deque(deepcopy(list(plans)))
        self.extractions = deque(deepcopy(list(extractions)))
        self.replies = deque(replies)
        self.default_plan = deepcopy(default_plan or make_plan("CLARIFY"))
        self.default_extraction = deepcopy(default_extraction or empty_extraction())
        self.default_reply = default_reply
        self.classify_calls: list[list[dict]] = []
        self.extract_calls: list[list[dict]] = []
        self.chat_calls: list[list[dict]] = []

    def queue_plan(self, plan: dict[str, Any]) -> None:
        self.plans.append(deepcopy(plan))

    def queue_extraction(self, result: dict[str, Any]) -> None:
        self.extractions.append(deepcopy(result))

    def classify_interaction(
        self,
        messages: list[dict],
        max_tokens: int = 480,
        role: Any = None,
    ) -> dict[str, Any]:
        del max_tokens, role
        self.classify_calls.append(deepcopy(messages))
        result = self.plans.popleft() if self.plans else self.default_plan
        return deepcopy(result)

    def extract_json(
        self,
        messages: list[dict],
        max_tokens: int = 800,
        role: Any = None,
        json_schema: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        del max_tokens, role, json_schema
        self.extract_calls.append(deepcopy(messages))
        result = self.extractions.popleft() if self.extractions else self.default_extraction
        return deepcopy(result)

    def chat(
        self,
        messages: list[dict],
        temperature: float = 0.7,
        max_tokens: int = 1500,
        role: Any = None,
    ) -> str:
        del temperature, max_tokens, role
        self.chat_calls.append(deepcopy(messages))
        return self.replies.popleft() if self.replies else self.default_reply

    def filter_reply(self, reply: Any, role: Any = None) -> str:
        del role
        return "" if reply is None else str(reply)
