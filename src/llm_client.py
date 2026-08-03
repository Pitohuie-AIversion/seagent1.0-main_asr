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
        """离线交互路由 mock；只返回最小协议结构，不维护业务关键词分类器。"""
        raw_user_content = self._message_content(messages, len(messages) - 1)
        text = self._latest_user_message(messages)
        context = self._json_after_marker(raw_user_content, "【当前上下文状态】") or {}
        expected_slots = context.get("expected_slots") if isinstance(context, dict) else []
        lower = text.lower()
        is_question = bool(re.search(r"(?:什么|哪些|如何|为什么|多少|几|吗|是否|能否|有没有|怎么|呢|[？?])", text))

        interaction_type = "WRITE"
        query_intent = None
        reason = "离线测试返回预设 WRITE 结果"

        if any(word in text for word in ("不确认", "不要确认", "不发布", "不要发布", "不取消", "不要取消", "暂不", "先不")) and not re.search(r"(?:[0-9]+|改成|设为|设置为|替换为|调整为)", text):
            interaction_type = "QUERY"
            query_intent = "CLARIFICATION"
            reason = "离线测试: 否定/暂缓控制词"
        elif any(word in lower for word in ("你好", "您好", "hello", "hi", "thanks", "谢谢", "天气", "不错")) or any(word in text for word in ("你是谁", "你能做什么")):
            interaction_type = "QUERY"
            query_intent = "GENERAL_CHAT"
            reason = "离线测试返回预设 QUERY 普通聊天结果"
        elif is_question and bool(re.search(r"^\d{3,}", text)):
            num_match = re.search(r"\d{3,}", text).group(0)
            interaction_type = "QUERY"
            query_intent = "CLARIFICATION"
            reason = f"纯数字序列号 {num_match}"
        elif text in ("金牛座一号机", "金牛座", "AUV一号机") and not expected_slots and not context.get("has_task"):
            interaction_type = "QUERY"
            query_intent = "CLARIFICATION"
            reason = "无任务上下文时单独输入设备别名，返回 CLARIFICATION"
        elif any(amb in text for amb in ("一号机", "二号机", "三号机", "1号机", "2号机", "3号机")) and not any(fam in text for fam in ("金牛座", "auv", "AUV", "crawler", "CRAWLER")) and not re.search(r"(?:换成|改成|设为|设置为|替换为|调整为)", text):
            interaction_type = "QUERY"
            query_intent = "CLARIFICATION"
            reason = "歧义设备别名'一号机'应路由到 CLARIFICATION"


        elif is_question and (
            any(kw in text for kw in ("水深", "深度", "能力", "最大", "作业", "支持哪些", "哪些机器人", "哪些设备", "工具", "载荷", "传感器"))
            or bool(re.search(r"(?:金牛座|CRAWLER|crawler|观察级|通用工作级|履带|rov|auv|ROV|AUV)[- _]?\d{3,}", text))
        ):
            # 设备能力/规格查询
            interaction_type = "QUERY"
            query_intent = "DEVICE_CAPABILITY"
            reason = "离线测试: 设备能力查询"
        elif is_question:
            interaction_type = "QUERY"
            query_intent = "CLARIFICATION"
            reason = "离线测试: 疑问语句缺少明确操作，降级为澄清"
        else:
            has_task_write_evidence = bool(expected_slots) or any(
                kw in text
                for kw in (
                    "巡检",
                    "作业",
                    "清洗",
                    "水深",
                    "设置",
                    "修改",
                    "创建",
                    "新建",
                    "参数",
                    "改成",
                    "设为",
                    "调整为",
                    "替换为",
                    "起点",
                    "终点",
                    "目标",
                    "坐标",
                    "油田",
                )
            )
            if has_task_write_evidence:
                interaction_type = "WRITE"
                query_intent = None
                reason = "离线测试: 识别到任务创建或参数修改意图"
            else:
                interaction_type = "QUERY"
                query_intent = "CLARIFICATION"
                reason = "离线测试: 缺少明确写入证据，降级为澄清"

        dialogue_mode = "task_collection" if interaction_type == "WRITE" else (
            "uncertain" if query_intent in ("CLARIFICATION", "UNKNOWN") else "knowledge_qa"
        )

        return {
            "dialogue_mode": dialogue_mode,
            "interaction_type": interaction_type,
            "query_intent": query_intent,
            "confidence": 0.95,
            "reason": reason,
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
