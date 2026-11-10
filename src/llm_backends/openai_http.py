"""src/llm_backends/openai_http.py — 标准 OpenAI/vLLM HTTP API 计算后端。

用于连接：
1. 本地 GPU 1 上启动的独立 vLLM OpenAI API 服务（如 Qwen3.8-27B-FP8）；
2. 局域网/远程 Ollama、vLLM、TGI 等服务节点；
3. 外部商用 API（如 Qwen-Max、DeepSeek 等）。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from src.llm_backends.base import BaseLLMBackend, LLMResponse, ToolCall

logger = logging.getLogger(__name__)


class OpenAICompatibleBackend(BaseLLMBackend):
    """OpenAI 协议兼容的 HTTP 计算后端。"""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8001/v1",
        api_key: str = "EMPTY",
        model_name: str = "default",
        timeout: float = 60.0,
        extra_client_kwargs: dict[str, Any] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model_name = model_name
        self.timeout = timeout
        self._extra_client_kwargs = extra_client_kwargs or {}
        self._client: Any = None

    def _get_client(self) -> Any:
        if self._client is None:
            try:
                from openai import OpenAI
                self._client = OpenAI(
                    base_url=self.base_url,
                    api_key=self.api_key,
                    timeout=self.timeout,
                    **self._extra_client_kwargs,
                )
            except ImportError as exc:
                raise RuntimeError(
                    "未安装 openai 依赖包，请运行 `pip install openai` 以使用 OpenAICompatibleBackend"
                ) from exc
        return self._client

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
        client = self._get_client()

        call_kwargs: dict[str, Any] = {
            "model": self.model_name,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        if stop:
            call_kwargs["stop"] = list(stop)

        if response_mode == "json":
            if response_schema is not None:
                # 尝试标准 json_schema 约束
                call_kwargs["response_format"] = {
                    "type": "json_object"
                }
            else:
                call_kwargs["response_format"] = {"type": "json_object"}

        if tools:
            # 格式化 tools 列表
            call_kwargs["tools"] = tools

        # 传递思考链开关（若后端支持特定参数）
        if "extra_body" in kwargs:
            call_kwargs["extra_body"] = kwargs["extra_body"]
        elif enable_thinking is not None:
            # 部分端点如 vLLM/DeepSeek 支持 extra_body
            pass

        try:
            resp = client.chat.completions.create(**call_kwargs)
        except Exception as exc:
            logger.error(
                "[OpenAICompatibleBackend] HTTP 请求失败 (base_url=%s, model=%s): %s",
                self.base_url,
                self.model_name,
                exc,
            )
            raise RuntimeError(f"OpenAICompatibleBackend 请求失败: {exc}") from exc

        if not resp.choices:
            raise RuntimeError("OpenAICompatibleBackend 返回空的 choices")

        choice = resp.choices[0]
        msg = choice.message
        text_content = msg.content or ""
        finish_reason = getattr(choice, "finish_reason", "stop") or "stop"

        parsed_tool_calls: list[ToolCall] | None = None
        if hasattr(msg, "tool_calls") and msg.tool_calls:
            parsed_tool_calls = []
            for i, tc in enumerate(msg.tool_calls):
                tc_id = getattr(tc, "id", f"call_{i}")
                func = getattr(tc, "function", None)
                if func:
                    fn_name = getattr(func, "name", "")
                    fn_args = getattr(func, "arguments", "{}")
                    parsed_tool_calls.append(
                        ToolCall(id=tc_id, name=fn_name, arguments=fn_args)
                    )

        return LLMResponse(
            text=text_content.strip(),
            tool_calls=parsed_tool_calls,
            finish_reason=finish_reason,
            raw_response=resp,
        )

    def health_check(self) -> tuple[bool, str]:
        """轻量健康探测接口，探测远程端点连通性。"""
        import urllib.request
        import urllib.error
        try:
            req = urllib.request.Request(
                f"{self.base_url}/models",
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
            with urllib.request.urlopen(req, timeout=1.5) as resp:
                if resp.status == 200:
                    return True, "online"
                return False, f"HTTP {resp.status}"
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403, 404):
                return True, f"online (HTTP {exc.code})"
            return False, f"HTTP {exc.code}"
        except Exception as exc:
            return False, f"unreachable ({type(exc).__name__})"

