"""src/llm_backends/base.py — LLM 计算后端基础抽象与响应契约。

设计原则：
1. 统一解耦计算引擎（本地 vLLM、远程 HTTP API、轻量 Mock 等）；
2. 标准化 LLMResponse 与 ToolCall 数据结构，对齐 OpenAI/vLLM 原生协议；
3. 支持纯文本、JSON 结构化模式与 Function Calling 工具调用。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
import json
from typing import Any, Optional


@dataclass
class ToolCall:
    """标准 Function Calling 调用载荷。"""
    id: str
    name: str
    arguments: str  # 保持为 JSON 字符串，便于向模型回传与反序列化

    def get_arguments_dict(self) -> dict[str, Any]:
        """解析 arguments 为 dict，解析失败时返回空字典。"""
        if not self.arguments:
            return {}
        try:
            val = json.loads(self.arguments)
            return val if isinstance(val, dict) else {}
        except Exception:
            return {}


@dataclass
class LLMResponse:
    """LLM 后端统一响应载荷。"""
    text: str
    tool_calls: Optional[list[ToolCall]] = None
    finish_reason: str = "stop"
    raw_response: Any = None
    tool_traces: Optional[list[dict[str, Any]]] = None

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)


class BaseLLMBackend(ABC):
    """计算后端基类抽象接口。"""

    @abstractmethod
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
        """执行单次推理生成。

        Args:
            messages: 历史消息列表 [{"role": "system"|"user"|"assistant"|"tool", "content": ...}]
            temperature: 采样温度
            max_tokens: 最大生成 token 数
            stop: 停用词列表
            tools: 可调用的工具定义列表（标准 JSON Schema）
            response_mode: "text" 或 "json"
            response_schema: 结构化输出 JSON Schema
            enable_thinking: 是否开启思考链 (CoT)
            **kwargs: 扩展参数

        Returns:
            LLMResponse 统一响应结构
        """
        raise NotImplementedError

    def health_check(self) -> tuple[bool, str]:
        """轻量健康探测接口，默认返回可用状态。"""
        return True, "ready"
