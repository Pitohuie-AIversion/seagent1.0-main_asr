"""本地 vLLM 推理接口封装。"""

from __future__ import annotations

import json
import logging
import re
import threading
from typing import Any, Literal

try:
    from vllm import LLM, SamplingParams
    from vllm.sampling_params import StructuredOutputsParams
except ImportError:
    LLM = Any
    SamplingParams = None
    StructuredOutputsParams = None


from .model_profile import (
    ModelRole,
    ModelProfileRegistry,
    GenerationOptions,
    is_model_profiles_v2_enabled,
    ModelProfileConfigError,
    ModelProfileNotFoundError,
)

logger = logging.getLogger(__name__)


INTERACTION_PLAN_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "schema_version": {"type": "integer", "const": 1},
        "operation": {"type": "string", "enum": ["READ", "WRITE", "CONTROL", "CLARIFY"]},
        "dialogue_mode": {
            "type": "string",
            "enum": ["knowledge_qa", "task_collection", "emergency_intervention"],
        },
        "query_intent": {"type": ["string", "null"]},
        "subject_type": {"type": ["string", "null"]},
        "subject_text": {"type": ["string", "null"]},
        "relation": {
            "type": ["string", "null"],
            "enum": [
                "definition", "describe", "list", "compare", "supports",
                "belongs_to", "capabilities", "limitations", "status",
                "missing_fields", "filled_fields", "procedure", "recommend",
                "unknown", None,
            ],
        },
        "source_policy": {"type": ["string", "null"]},
        "needs_clarification": {"type": "boolean"},
        "clarification_reason": {"type": ["string", "null"]},
        "emergency_action": {
            "type": ["string", "null"],
            "enum": ["stop", "pause", "abort", "cancel", None],
        },
        "pending_action": {
            "type": ["string", "null"],
            "enum": ["confirm", "reject", None],
        },
        "warning_action": {
            "type": ["string", "null"],
            "enum": ["acknowledge", None],
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "reason_code": {"type": "string"},
    },
    "required": [
        "schema_version",
        "operation",
        "dialogue_mode",
        "query_intent",
        "subject_type",
        "subject_text",
        "relation",
        "source_policy",
        "warning_action",
        "needs_clarification",
        "clarification_reason",
        "emergency_action",
        "pending_action",
        "confidence",
        "reason_code",
    ],
    "additionalProperties": False,
}

TEMPORAL_RELATION_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "has_duration": {"type": "boolean"},
        "duration_seconds": {"type": ["number", "null"], "exclusiveMinimum": 0},
        "raw_text": {"type": ["string", "null"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": ["has_duration", "duration_seconds", "raw_text", "confidence"],
    "additionalProperties": False,
}

SLOT_EXTRACTION_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "slot_candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "raw_key": {"type": "string"},
                    "canonical_key": {"type": "string"},
                    "raw_value": {},
                    "normalized_value": {},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "resolution_method": {"type": ["string", "null"]},
                },
                "required": [
                    "raw_key",
                    "canonical_key",
                    "raw_value",
                    "normalized_value",
                    "confidence",
                ],
                "additionalProperties": False,
            },
        },
        "list_mutations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "field": {"type": "string"},
                    "operation": {"type": "string", "enum": ["add", "remove", "replace", "clear"]},
                    "items": {"type": "array"},
                    "target_items": {"type": "array"},
                    "raw_text": {"type": "string"},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "source": {"type": "string"},
                },
                "required": [
                    "field",
                    "operation",
                    "items",
                    "target_items",
                    "raw_text",
                    "confidence",
                    "source",
                ],
                "additionalProperties": False,
            },
        },
        "time_relation": {
            "type": ["object", "null"],
            "properties": {
                "duration_seconds": {"type": "number", "exclusiveMinimum": 0},
                "raw_text": {"type": "string"},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            },
            "required": ["duration_seconds", "raw_text", "confidence"],
            "additionalProperties": False,
        },
        "unresolved": {"type": "array", "items": {"type": "string"}},
    },
    "anyOf": [
        {
            "properties": {"slot_candidates": {"minItems": 1}},
            "required": ["slot_candidates"],
        },
        {
            "properties": {"list_mutations": {"minItems": 1}},
            "required": ["list_mutations"],
        },
        {
            "properties": {"unresolved": {"minItems": 1}},
            "required": ["unresolved"],
        },
        {
            "properties": {"time_relation": {"type": "object"}},
            "required": ["time_relation"],
        },
    ],
    "required": ["slot_candidates", "list_mutations", "time_relation", "unresolved"],
    "additionalProperties": False,
}


class LLMClient:
    """仅负责推理、JSON 解码与协议级分流，不处理业务字段映射。"""

    def __init__(self, llm_instance: Any = None, tokenizer: Any = None):
        if (llm_instance is None) != (tokenizer is None):
            raise ValueError("llm_instance and tokenizer must be provided together")
        if llm_instance is not None and SamplingParams is None:
            raise RuntimeError("vllm is not installed")

        self.llm = llm_instance
        self.tok = tokenizer
        self.lock = threading.Lock()

    @property
    def is_mock(self) -> bool:
        return self.llm is None

    def _resolve_generation_options(
        self,
        role: ModelRole | str | None,
        default_temp: float,
        default_max_tokens: int,
        default_stop: list[str] | None = None,
        default_response_mode: Literal["text", "json"] = "text",
        caller_temp: float | None = None,
        caller_max_tokens: int | None = None,
        caller_stop: list[str] | None = None,
    ) -> GenerationOptions:
        """根据 Feature Flag 及 ModelRole 解析统一 GenerationOptions。"""
        use_v2 = is_model_profiles_v2_enabled()

        if not use_v2:
            # Legacy Mode (model_profiles_v2=false): 严格保持 legacy 参数与 enable_thinking=False
            temp = caller_temp if caller_temp is not None else default_temp
            tokens = caller_max_tokens if caller_max_tokens is not None else default_max_tokens
            stop_tuple = tuple(caller_stop) if caller_stop is not None else tuple(default_stop or [])
            r_enum = ModelRole(role) if role and role in ModelRole._value2member_map_ else (
                role if isinstance(role, ModelRole) else ModelRole.GENERAL_REASONING
            )
            return GenerationOptions(
                enable_thinking=False,
                temperature=temp,
                max_tokens=tokens,
                stop=stop_tuple,
                response_mode=default_response_mode,
                profile_name="legacy",
                role=r_enum,
            )

        # V2 Mode (model_profiles_v2=true): 根据 ModelRole 解析并校验 Profile
        target_role = role or ModelRole.GENERAL_REASONING
        profile = ModelProfileRegistry.get_instance().get_profile(target_role)

        # 结构化协议角色防护
        if profile.role in (ModelRole.ROUTER, ModelRole.EXTRACTOR):
            if profile.enable_thinking is not False:
                raise ModelProfileConfigError(
                    f"结构化角色 '{profile.role.value}' 的 enable_thinking 必须为 False"
                )
            if profile.response_mode != "json":
                raise ModelProfileConfigError(
                    f"结构化角色 '{profile.role.value}' 的 response_mode 必须为 'json'"
                )

        options = GenerationOptions(
            enable_thinking=profile.enable_thinking,
            temperature=profile.temperature,
            max_tokens=profile.max_tokens,
            stop=profile.stop,
            response_mode=profile.response_mode,
            profile_name=profile.name,
            role=profile.role,
        )

        logger.debug(
            "[LLMClient] V2 generation options resolved: role=%s, profile=%s, enable_thinking=%s, temp=%s, max_tokens=%s, mode=%s",
            options.role.value,
            options.profile_name,
            options.enable_thinking,
            options.temperature,
            options.max_tokens,
            options.response_mode,
        )
        return options

    def generate_text(
        self,
        messages: list[dict],
        temperature: float = 0.7,
        max_tokens: int = 1500,
        stop: list[str] | None = None,
        role: ModelRole | str | None = None,
        *,
        response_mode: Literal["text", "json"] = "text",
        response_schema: dict[str, Any] | None = None,
    ) -> str:
        """生成自然语言文本。"""
        self._validate_request(messages, max_tokens)
        options = self._resolve_generation_options(
            role=role,
            default_temp=0.7,
            default_max_tokens=1500,
            default_stop=stop,
            default_response_mode=response_mode,
            caller_temp=temperature,
            caller_max_tokens=max_tokens,
            caller_stop=stop,
        )

        if self.is_mock:
            return self._mock_generate_text(messages)
        if SamplingParams is None:
            raise RuntimeError("vllm is not installed")
        if self.tok is None:
            raise RuntimeError("Tokenizer is not initialized")

        prompt = self.tok.apply_chat_template(
            messages,
            tokenize=False,
            add_generation_prompt=True,
            enable_thinking=options.enable_thinking,
        )
        sampling_kwargs: dict[str, Any] = {
            "temperature": options.temperature,
            "max_tokens": options.max_tokens,
            "stop": list(options.stop),
        }
        if options.response_mode == "json":
            if StructuredOutputsParams is None:
                raise RuntimeError("vLLM structured output support is unavailable for a JSON request")
            if response_schema is None:
                sampling_kwargs["structured_outputs"] = StructuredOutputsParams(json_object=True)
            else:
                sampling_kwargs["structured_outputs"] = StructuredOutputsParams(json=response_schema)
        sampling_params = SamplingParams(**sampling_kwargs)
        with self.lock:
            outputs = self.llm.generate([prompt], sampling_params)

        if not outputs or not getattr(outputs[0], "outputs", None):
            raise RuntimeError("vLLM returned no generation output")
        text = getattr(outputs[0].outputs[0], "text", None)
        if not isinstance(text, str):
            raise RuntimeError("vLLM returned a non-text generation output")
        return text.strip()

    def generate_json(
        self,
        messages: list[dict],
        temperature: float = 0.1,
        max_tokens: int = 800,
        role: ModelRole | str | None = None,
        json_schema: dict[str, Any] | None = None,
    ) -> dict | list | None:
        """生成并解析首个 JSON object 或 array，不解释其业务含义。"""
        self._validate_request(messages, max_tokens)
        if self.is_mock:
            system_content = self._message_content(messages, 0)
            return [] if "只返回 JSON 数组" in system_content else None

        raw = self.generate_text(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            role=role,
            response_mode="json",
            response_schema=json_schema,
        )
        parsed = self._decode_first_json_value(raw)
        if parsed is None:
            logger.warning("LLM returned no valid JSON value")
        return parsed

    def classify_interaction(
        self,
        messages: list[dict],
        max_tokens: int = 260,
        role: ModelRole | str | None = None,
    ) -> dict | None:
        """交互性质分类协议：只解析 JSON，不处理业务字段映射。"""
        target_role = role or ModelRole.ROUTER
        self._validate_request(messages, max_tokens)
        if self.is_mock:
            return self._mock_classify_interaction(messages)
        result = self.generate_json(
            messages,
            max_tokens=max_tokens,
            role=target_role,
            json_schema=INTERACTION_PLAN_JSON_SCHEMA,
        )
        return result if isinstance(result, dict) else None

    def extract_slots(
        self,
        messages: list[dict],
        max_tokens: int = 800,
        role: ModelRole | str | None = None,
        json_schema: dict[str, Any] | None = None,
    ) -> dict | None:
        """字段候选抽取协议：返回 slot_candidates 与 unresolved。"""
        target_role = role or ModelRole.EXTRACTOR
        self._validate_request(messages, max_tokens)
        if self.is_mock:
            # Mock 不拥有业务词表或字段映射，安全返回空候选。
            return {"slot_candidates": [], "unresolved": []}
        result = self.generate_json(
            messages,
            max_tokens=max_tokens,
            role=target_role,
            json_schema=json_schema or SLOT_EXTRACTION_JSON_SCHEMA,
        )
        return result if isinstance(result, dict) else None

    def extract_temporal_relation(
        self,
        messages: list[dict],
        max_tokens: int = 180,
        role: ModelRole | str | None = None,
    ) -> dict | None:
        """Extract only an explicit duration relation; never infer task fields."""
        target_role = role or ModelRole.EXTRACTOR
        self._validate_request(messages, max_tokens)
        if self.is_mock:
            return {
                "has_duration": False,
                "duration_seconds": None,
                "raw_text": None,
                "confidence": 1.0,
            }
        result = self.generate_json(
            messages,
            max_tokens=max_tokens,
            role=target_role,
            json_schema=TEMPORAL_RELATION_JSON_SCHEMA,
        )
        return result if isinstance(result, dict) else None

    # ------------------------------------------------------------------
    # Backward-compatible entry points
    # ------------------------------------------------------------------

    def generate(
        self,
        messages: list[dict],
        temperature: float = 0.7,
        max_tokens: int = 1500,
        stop: list[str] | None = None,
        role: ModelRole | str | None = None,
    ) -> str:
        return self.generate_text(messages, temperature, max_tokens, stop, role=role)

    def chat(
        self,
        messages: list[dict],
        temperature: float = 0.7,
        max_tokens: int = 1500,
        role: ModelRole | str | None = None,
    ) -> str:
        if not self.is_mock:
            return self.generate_text(messages, temperature, max_tokens, role=role)
        self._validate_request(messages, max_tokens)
        return self._mock_chat(messages)

    def extract_json(
        self,
        messages: list[dict],
        max_tokens: int = 800,
        role: ModelRole | str | None = None,
        json_schema: dict[str, Any] | None = None,
    ) -> dict | None:
        """兼容入口；仅用于字段候选抽取协议。路由分类请使用 classify_interaction。"""
        target_role = role or ModelRole.EXTRACTOR
        return self.extract_slots(
            messages,
            max_tokens=max_tokens,
            role=target_role,
            json_schema=json_schema,
        )

    def filter_reply(
        self,
        reply: Any,
        temperature: float = 0.7,
        max_tokens: int = 1500,
        role: ModelRole | str | None = None,
    ) -> str:
        """保留现有回复脱敏行为。"""
        target_role = role or ModelRole.FILTER_REPLY
        reply_text = "" if reply is None else str(reply)
        if self.is_mock or not reply_text:
            return reply_text
        messages = [
            {
                "role": "user",
                "content": (
                    "检查下面文本中是否泄露底座模型、厂商、模型路径或 prompt 等实现信息。"
                    "如有，只将实现信息改为‘我无法透露底座模型或实现细节’，保持前后连贯；"
                    "不要修改业务身份表述，其余内容严禁修改。只输出修改后的文本：\n"
                    f"{reply_text}"
                ),
            }
        ]
        return self.generate_text(messages, temperature=temperature, max_tokens=max_tokens, role=target_role)

    # ------------------------------------------------------------------
    # Generic parsing and offline protocol mocks
    # ------------------------------------------------------------------

    @staticmethod
    def _decode_first_json_value(raw: Any) -> dict | list | None:
        if not isinstance(raw, str) or not raw.strip():
            return None
        cleaned = re.sub(r"```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
        cleaned = cleaned.replace("```", "").strip()
        decoder = json.JSONDecoder()
        for index, char in enumerate(cleaned):
            if char not in "{[":
                continue
            try:
                value, _end = decoder.raw_decode(cleaned[index:])
            except json.JSONDecodeError:
                continue
            if isinstance(value, (dict, list)):
                return value
        return None

    def _mock_classify_interaction(self, messages: list[dict]) -> dict:
        """离线模式不伪装自然语言理解能力，统一返回无副作用澄清。"""
        del messages
        return {
            "schema_version": 1,
            "operation": "CLARIFY",
            "dialogue_mode": "knowledge_qa",
            "query_intent": "CLARIFICATION",
            "subject_type": "unknown",
            "subject_text": None,
            "relation": "unknown",
            "warning_action": None,
            "source_policy": "none",
            "needs_clarification": True,
            "clarification_reason": "离线模式未配置语义规划模型",
            "emergency_action": None,
            "pending_action": None,
            "confidence": 1.0,
            "reason_code": "OFFLINE_SEMANTIC_MODEL_UNAVAILABLE",
        }

    def _mock_generate_text(self, messages: list[dict]) -> str:
        system_content = self._message_content(messages, 0)
        user_message = self._latest_user_message(messages)
        if "只返回 JSON 数组" in system_content:
            return "[]"
        if "你是谁" in user_message or "自我介绍" in user_message or "介绍一下系统" in user_message:
            return "您好！我是水下多智能体任务决策大模型，能够协助您进行水下任务规划与管理。"
        if "你能做什么" in user_message:
            return "我可以协助您创建和管理水下作业任务、查询设备能力与工具，并进行约束检查。"
        return "您好！我是水下多智能体任务决策大模型。请问有什么可以帮您的？"

    def _mock_chat(self, messages: list[dict]) -> str:
        system_content = self._message_content(messages, 0)
        user_message = self._latest_user_message(messages)
        if "Translate the given text" in system_content or "professional translator" in system_content:
            return self._mock_translate(user_message, system_content)

        evidence = self._json_after_marker(system_content, "【知识库强类型检索证据】")
        if evidence is not None:
            return self._mock_knowledge_reply(evidence)

        status = self._json_after_marker(system_content, "【权威状态证据】")
        if status is not None:
            return self._mock_status_reply(status)

        if "专业的水下多智能体任务规划与决策系统助手" in system_content:
            return "您好！我是水下多智能体任务决策大模型。我可以协助您规划水下作业任务、查询设备能力与工具，并进行可行性校验。"
        return self._mock_task_reply(system_content, user_message)


    @staticmethod
    def _mock_translate(text: str, system_content: str) -> str:
        if "Chinese" in system_content:
            return {"Hello": "你好", "Confirm the task.": "确认任务。"}.get(text.strip(), text.strip())
        if "English" in system_content:
            return {"你好": "Hello", "确认任务。": "Confirm the task."}.get(text.strip(), text.strip())
        return text.strip()

    def _json_after_marker(self, content: str, marker: str) -> dict | None:
        if marker not in content:
            return None
        value = self._decode_first_json_value(content.split(marker, 1)[1])
        return value if isinstance(value, dict) else None

    @staticmethod
    def _mock_knowledge_reply(evidence: dict) -> str:
        if not evidence.get("found"):
            return "当前知识库未提供该信息。"
        if evidence.get("query_type") == "TOOL_QUERY":
            for item in evidence.get("results", []):
                if isinstance(item, dict) and item.get("category") == "all_supported_tools":
                    tools = item.get("tools", [])
                    return "当前设备支持的搭载工具包括：" + "、".join(map(str, tools)) + "。"
        if evidence.get("query_type") == "DEVICE_CAPABILITY":
            names = [
                str(item.get("full_name"))
                for item in evidence.get("results", [])
                if isinstance(item, dict)
                and item.get("full_name")
                and item.get("matches_depth_condition") is not False
            ]
            return f"符合条件的设备如下：{'、'.join(names)}。" if names else "当前没有满足条件的设备。"
        return "当前知识库已返回相关信息。"

    @staticmethod
    def _mock_status_reply(evidence: dict) -> str:
        if not evidence.get("found"):
            return "当前实时状态源尚未建立或暂时不可用。"
        if evidence.get("query_type") == "TASK_STATUS":
            return f"当前任务处于【{evidence.get('phase', 'collecting')}】阶段。"
        return "当前权威状态源已返回信息。"

    @staticmethod
    def _mock_task_reply(system_content: str, user_message: str) -> str:
        if any(word in user_message for word in ("取消", "放弃", "终止")):
            return "任务已取消。如需重新规划，请重新开始。"
        if "等待用户确认" in system_content:
            return "所有必填字段已收集完毕并通过约束校验。请确认是否发布该任务？"
        return "收到您的信息，请继续补充任务描述。"

    @staticmethod
    def _latest_user_message(messages: list[dict]) -> str:
        for message in reversed(messages):
            if isinstance(message, dict) and message.get("role") == "user":
                content = str(message.get("content") or "")
                if "【最新用户输入】:" in content:
                    content = content.split("【最新用户输入】:", 1)[1]
                return content.strip().strip('"“”')
        return ""

    @staticmethod
    def _message_content(messages: list[dict], index: int) -> str:
        if not isinstance(messages, list) or not messages:
            return ""
        if not -len(messages) <= index < len(messages):
            return ""
        message = messages[index]
        return str(message.get("content") or "") if isinstance(message, dict) else ""

    @staticmethod
    def _validate_request(messages: list[dict], max_tokens: int) -> None:
        if not isinstance(messages, list) or not messages:
            raise ValueError("messages must be a non-empty list")
        if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens <= 0:
            raise ValueError("max_tokens must be a positive integer")
