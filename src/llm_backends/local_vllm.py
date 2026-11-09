"""src/llm_backends/local_vllm.py — 本地单卡/显卡 vLLM 推理后端。

封装 GPU 0 上的本地 vLLM Engine 与 Tokenizer，包含并发锁保护、上下文预算管理与 Structured Outputs。
"""

from __future__ import annotations

import json
import logging
import re
import threading
from typing import Any

from src.llm_backends.base import BaseLLMBackend, LLMResponse, ToolCall

try:
    from vllm import SamplingParams
    from vllm.sampling_params import StructuredOutputsParams
except ImportError:
    SamplingParams = None
    StructuredOutputsParams = None

logger = logging.getLogger(__name__)


class LocalVllmBackend(BaseLLMBackend):
    """基于本地 vLLM 实例与 Tokenizer 的极速计算后端。"""

    def __init__(self, llm_engine: Any, tokenizer: Any, lock: threading.Lock | None = None) -> None:
        self.engine = llm_engine
        self.tokenizer = tokenizer
        self.lock = lock or threading.Lock()

    def generate(
        self,
        messages: list[dict[str, Any]],
        temperature: float = 0.7,
        max_tokens: int = 1500,
        stop: list[str] | tuple[str, ...] | None = None,
        tools: list[dict[str, Any]] | None = None,
        response_mode: str = "text",
        response_schema: dict[str, Any] | None = None,
        enable_thinking: bool = False,
        **kwargs: Any,
    ) -> LLMResponse:
        if self.engine is None or self.tokenizer is None:
            raise RuntimeError("LocalVllmBackend 引擎或 Tokenizer 未初始化")
        if SamplingParams is None:
            raise RuntimeError("未安装 vllm 依赖")

        # 1. 渲染聊天 Prompt
        prompt = self.tokenizer.apply_chat_template(
            messages,
            tokenize=False,
            add_generation_prompt=True,
            enable_thinking=enable_thinking,
        )

        sampling_kwargs: dict[str, Any] = {
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stop": list(stop or []),
        }

        # 2. 结构化 JSON 模式
        if response_mode == "json":
            if StructuredOutputsParams is None:
                raise RuntimeError("当前 vLLM 版本缺少 StructuredOutputsParams 支持")
            if response_schema is None:
                sampling_kwargs["structured_outputs"] = StructuredOutputsParams(json_object=True)
            else:
                sampling_kwargs["structured_outputs"] = StructuredOutputsParams(json=response_schema)

        sampling_params = SamplingParams(**sampling_kwargs)

        with self.lock:
            outputs = self.engine.generate([prompt], sampling_params)

        if not outputs or not getattr(outputs[0], "outputs", None):
            raise RuntimeError("vLLM 未返回有效输出")

        raw_text = getattr(outputs[0].outputs[0], "text", "")
        if not isinstance(raw_text, str):
            raise RuntimeError("vLLM 返回了非文本生成结果")

        text = raw_text.strip()
        tool_calls: list[ToolCall] | None = None

        # 3. 检查是否有模型输出的文本形式 Function Calling (如 Qwen 风格的 <tool_call>)
        if tools and "<tool_call>" in text:
            tool_calls = self._extract_text_tool_calls(text)

        return LLMResponse(
            text=text,
            tool_calls=tool_calls,
            finish_reason="tool_calls" if tool_calls else "stop",
            raw_response=outputs,
        )

    @staticmethod
    def _extract_text_tool_calls(text: str) -> list[ToolCall]:
        """从模型生成的文本中解析 <tool_call> 标签。"""
        calls: list[ToolCall] = []
        pattern = r"<tool_call>\s*(.*?)\s*</tool_call>"
        matches = re.findall(pattern, text, flags=re.DOTALL)
        for i, m in enumerate(matches):
            try:
                data = json.loads(m)
                if isinstance(data, dict):
                    name = data.get("name")
                    args = data.get("arguments", {})
                    args_str = json.dumps(args, ensure_ascii=False) if isinstance(args, dict) else str(args)
                    if name:
                        calls.append(ToolCall(id=f"vllm_call_{i}", name=name, arguments=args_str))
            except Exception:
                continue
        return calls
