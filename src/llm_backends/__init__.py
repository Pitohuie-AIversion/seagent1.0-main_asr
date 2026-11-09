"""src/llm_backends — 多后端 LLM 计算层与 Tool Calling 抽象。"""

from src.llm_backends.base import BaseLLMBackend, LLMResponse, ToolCall
from src.llm_backends.local_vllm import LocalVllmBackend
from src.llm_backends.openai_http import OpenAICompatibleBackend
from src.llm_backends.mock_backend import MockBackend

__all__ = [
    "BaseLLMBackend",
    "LLMResponse",
    "ToolCall",
    "LocalVllmBackend",
    "OpenAICompatibleBackend",
    "MockBackend",
]
