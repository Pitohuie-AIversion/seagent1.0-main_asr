"""本地 vLLM 推理接口封装。"""

from __future__ import annotations

import json
import logging
import re
import threading
from typing import Any

try:
    from vllm import LLM, SamplingParams
except ImportError:
    LLM = Any
    SamplingParams = None


logger = logging.getLogger(__name__)


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

    def generate_text(
        self,
        messages: list[dict],
        temperature: float = 0.7,
        max_tokens: int = 1500,
        stop: list[str] | None = None,
    ) -> str:
        """生成自然语言文本。"""
        self._validate_request(messages, max_tokens)
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
            enable_thinking=False,
        )
        sampling_params = SamplingParams(
            temperature=temperature,
            max_tokens=max_tokens,
            stop=stop or [],
        )
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
        )
        parsed = self._decode_first_json_value(raw)
        if parsed is None:
            logger.warning("LLM returned no valid JSON value")
        return parsed

    def classify_interaction(
        self,
        messages: list[dict],
        max_tokens: int = 260,
    ) -> dict | None:
        """交互性质分类协议：只解析 JSON，不处理业务字段映射。"""
        self._validate_request(messages, max_tokens)
        if self.is_mock:
            return self._mock_classify_interaction(messages)
        result = self.generate_json(messages, max_tokens=max_tokens)
        return result if isinstance(result, dict) else None

    def extract_slots(
        self,
        messages: list[dict],
        max_tokens: int = 800,
    ) -> dict | None:
        """字段候选抽取协议：返回 slot_candidates 与 unresolved。"""
        self._validate_request(messages, max_tokens)
        if self.is_mock:
            # Mock 不拥有业务词表或字段映射，安全返回空候选。
            return {"slot_candidates": [], "unresolved": []}
        result = self.generate_json(messages, max_tokens=max_tokens)
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
    ) -> str:
        return self.generate_text(messages, temperature, max_tokens, stop)

    def chat(
        self,
        messages: list[dict],
        temperature: float = 0.7,
        max_tokens: int = 1500,
    ) -> str:
        if not self.is_mock:
            return self.generate_text(messages, temperature, max_tokens)
        self._validate_request(messages, max_tokens)
        return self._mock_chat(messages)

    def extract_json(
        self,
        messages: list[dict],
        max_tokens: int = 800,
    ) -> dict | None:
        """兼容入口；仅用于字段候选抽取协议。路由分类请使用 classify_interaction。"""
        return self.extract_slots(messages, max_tokens=max_tokens)

    def filter_reply(
        self,
        reply: Any,
        temperature: float = 0.1,
        max_tokens: int = 1500,
    ) -> str:
        """保留现有回复脱敏行为。"""
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
        return self.generate_text(messages, temperature=temperature, max_tokens=max_tokens)

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
        """离线交互路由 mock；只判断交互类型，不规范化业务实体。"""
        raw_user_content = self._message_content(messages, len(messages) - 1)
        text = self._latest_user_message(messages)
        context = self._json_after_marker(raw_user_content, "【当前上下文状态】") or {}
        expected_slots = context.get("expected_slots") if isinstance(context, dict) else []
        lower = text.lower()
        is_question = bool(re.search(r"(?:什么|哪些|如何|为什么|多少|几|吗|是否|能否|有没有|怎么|[？?])", text))

        interaction_type = "AMBIGUOUS"
        write_action = "NONE"
        control_action = "NONE"
        query_intent = None
        reason = "离线模式未识别到明确交互性质"
        write_evidence: list[str] = []
        query_evidence: list[str] = []

        if re.search(r"(?:取消|放弃|终止).*(?:任务|作业)?", text):
            interaction_type, control_action, reason = "CONTROL", "CANCEL", "离线规则识别到取消控制指令"
        elif re.search(r"(?:确认|确定).*(?:发布|下发|任务|继续)?", text):
            interaction_type, control_action, reason = "CONTROL", "CONFIRM", "离线规则识别到确认控制指令"
        elif expected_slots and not is_question:
            interaction_type, write_action, reason = "WRITE", "UPDATE", "离线规则识别到用户回答 expected_slots"
            write_evidence = [text]
        elif not is_question and self._mock_looks_like_task_creation(text):
            interaction_type, write_action, reason = "WRITE", "CREATE", "离线规则识别到任务创建输入"
            write_evidence = [text]
        elif not is_question and self._mock_looks_like_slot_submission(text):
            interaction_type, write_action, reason = "WRITE", "UPDATE", "离线规则识别到参数写入输入"
            write_evidence = [text]
        elif is_question and re.search(r"(?:任务|作业).*(?:状态|进度|缺|参数)", text):
            interaction_type, query_intent, reason = "QUERY", "TASK_STATUS", "离线规则识别到任务状态查询"
            query_evidence = [text]
        elif is_question and re.search(r"(?:工具|载荷|抓手|声呐|机械臂|传感器)", text):
            interaction_type, query_intent, reason = "QUERY", "TOOL_QUERY", "离线规则识别到工具查询"
            query_evidence = [text]
        elif is_question and re.search(r"(?:类型|参数|字段|规则|模板|分类)", text):
            interaction_type, query_intent, reason = "QUERY", "KNOWLEDGE_QA", "离线规则识别到知识查询"
            query_evidence = [text]
        elif is_question and (any(word in lower for word in ("rov", "auv")) or re.search(r"(?:设备|型号|能力|最大水深|下潜)", text)):
            interaction_type, query_intent, reason = "QUERY", "DEVICE_CAPABILITY", "离线规则识别到设备能力查询"
            query_evidence = [text]
        elif any(word in lower for word in ("你好", "您好", "hello", "hi", "thanks")) or any(word in text for word in ("你是谁", "你能做什么")):
            interaction_type, reason = "CHAT", "离线规则识别到普通对话"

        return {
            "interaction_type": interaction_type,
            "write_action": write_action,
            "control_action": control_action,
            "query_intent": query_intent,
            "confidence": 0.95 if interaction_type != "AMBIGUOUS" else 0.75,
            "reason": reason,
            "write_evidence": write_evidence,
            "query_evidence": query_evidence,
        }

    @staticmethod
    def _mock_looks_like_task_creation(text: str) -> bool:
        action_pattern = r"(?:创建|新建|规划|安排|执行|开展|开始|做|进行|发起)"
        object_pattern = r"(?:任务|作业|巡检|插入|拔出|控制面板|管缆|采油树)"
        return bool(re.search(action_pattern, text) and re.search(object_pattern, text))

    @staticmethod
    def _mock_looks_like_slot_submission(text: str) -> bool:
        field_pattern = (
            r"(?:开始|结束|时间|起始|终止|坐标|经纬度|水深|深度|类型|系列|型号|编号|"
            r"设备|机器人|工具|载荷|母船|支持船|井口|油田)"
        )
        assignment_pattern = r"(?:为|是|用|使用|选择|选|改|换|携带|带|配备|搭载|编号|类型|型号)"
        return bool(re.search(field_pattern, text) and re.search(assignment_pattern, text))

    def _mock_generate_text(self, messages: list[dict]) -> str:
        system_content = self._message_content(messages, 0)
        user_message = self._latest_user_message(messages)
        if "只返回 JSON 数组" in system_content:
            return "[]"
        if "你是谁" in user_message or "自我介绍" in user_message or "介绍一下系统" in user_message:
            return "您好！我是水下多智能体任务决策大模型，能够协助进行水下任务规划与管理。"
        if "你能做什么" in user_message:
            return "我可以协助创建和管理水下作业任务、查询设备能力与工具，并进行约束检查。"
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
            return "您好！我是水下多智能体任务决策大模型。我可以协助规划水下作业任务、查询设备能力与工具，并进行可行性校验。"
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
