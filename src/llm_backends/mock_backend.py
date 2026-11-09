"""src/llm_backends/mock_backend.py — 离线与单元测试 Mock 计算后端。

提供零硬件依赖、确定性断言的 Mock 推理与 Function Calling 模拟。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from src.llm_backends.base import BaseLLMBackend, LLMResponse, ToolCall

logger = logging.getLogger(__name__)


class MockBackend(BaseLLMBackend):
    """离线与测试 Mock 后端。"""

    def __init__(self, canned_responses: dict[str, Any] | None = None) -> None:
        self.canned_responses = canned_responses or {}

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
        system_content = ""
        user_content = ""
        has_tool_result = False
        last_tool_content = ""

        for m in messages:
            role = m.get("role")
            content = str(m.get("content") or "")
            if role == "system" and not system_content:
                system_content = content
            elif role == "user":
                user_content = content
            elif role == "tool":
                has_tool_result = True
                last_tool_content = content

        # 1. 如果带有自定义预置响应
        for pattern, canned in self.canned_responses.items():
            if pattern in user_content or pattern in system_content:
                if isinstance(canned, LLMResponse):
                    return canned
                if isinstance(canned, dict):
                    return LLMResponse(text=json.dumps(canned, ensure_ascii=False))
                return LLMResponse(text=str(canned))

        # 2. 如果之前已有工具调用结果回传，生成基于工具结果的总结
        if has_tool_result and last_tool_content:
            try:
                tool_data = json.loads(last_tool_content)
                return LLMResponse(
                    text=f"根据工具查询返回的事实数据：{json.dumps(tool_data, ensure_ascii=False)}，已确认相关规格参数并更新任务规划。",
                    finish_reason="stop",
                )
            except Exception:
                return LLMResponse(text="根据工具返回信息，已完成参数获取与校验。")

        # 3. 如果提供了 tools 且尚未调用过工具，模拟触发 Tool Calling
        if tools and ("金牛座" in user_content or "ROV" in user_content or "规格" in user_content or "参数" in user_content):
            # 找到匹配的工具
            for t in tools:
                fn = t.get("function", {})
                name = fn.get("name")
                if name == "query_robot_specs":
                    return LLMResponse(
                        text="",
                        tool_calls=[
                            ToolCall(
                                id="call_mock_specs_01",
                                name="query_robot_specs",
                                arguments=json.dumps({"robot_name": "金牛座"}, ensure_ascii=False),
                            )
                        ],
                        finish_reason="tool_calls",
                    )
                elif name == "query_oilfield_position":
                    return LLMResponse(
                        text="",
                        tool_calls=[
                            ToolCall(
                                id="call_mock_pos_01",
                                name="query_oilfield_position",
                                arguments=json.dumps({"field_name": "陵水17-2"}, ensure_ascii=False),
                            )
                        ],
                        finish_reason="tool_calls",
                    )

        # 4. JSON 结构化模式响应
        if response_mode == "json":
            if "只返回 JSON 数组" in system_content:
                return LLMResponse(text="[]")
            if "slot_candidates" in system_content or (response_schema and "slot_candidates" in str(response_schema)):
                return LLMResponse(text=json.dumps({"slot_candidates": [], "unresolved": []}))
            return LLMResponse(text="{}")

        # 5. 常规自然语言响应
        if any(w in user_content for w in ("你是谁", "自我介绍", "介绍一下系统", "你能做什么")):
            intro = (
                "您好，SEAgent 水下多智能体任务决策系统已就绪。\n\n"
                "系统提供以下两类核心交互能力，并将根据您的输入自动识别需求并进入相应处理流程：\n\n"
                "1. 知识与状态查询\n"
                "用于查询机器人能力与设备参数、载荷与工具信息、任务流程、系统功能及相关状态信息。查询过程为只读模式，不会创建、修改或发布任务。\n"
                "示例：“金牛座一号机的最大作业水深是多少？”\n\n"
                "2. 任务创建与准入\n"
                "根据作业需求收集任务目标、时间、位置、环境条件、执行机器人及载荷配置等关键信息，并进行任务完整性与约束校验。满足准入条件后，系统将生成待确认任务，经您确认后方可发布。\n"
                "示例：“执行海底油气管道巡检，起点115.2°E 20.1°N，水深300米，使用观察级深海机器人。”\n\n"
                "请直接描述您的作业需求，或提出需要查询的问题。"
            )
            return LLMResponse(text=intro)

        return LLMResponse(text="您好，SEAgent 水下多智能体任务决策系统已就绪。请直接描述您的作业需求，或提出需要查询的问题。")
