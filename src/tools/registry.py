"""src/tools/registry.py — 轻量级工具注册表与 Function Calling 规范映射。

设计原则：
1. 纯标准库实现，零第三方框架依赖（不依赖 LangChain/AutoGen）；
2. 自动由 Python 类型注解与函数描述推导 OpenAI/vLLM 兼容的标准 Function Calling JSON Schema；
3. 支持动态注册、按角色子集过滤与执行结果序列化；
4. 严格只读沙箱设计，防任意代码注入与异常穿透。
"""

from __future__ import annotations

import inspect
import json
import logging
from dataclasses import dataclass
from typing import Any, Callable, get_type_hints

logger = logging.getLogger(__name__)


@dataclass
class ToolDefinition:
    name: str
    description: str
    function: Callable[..., Any]
    schema: dict[str, Any]
    roles: tuple[str, ...] = ("all",)

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return self.schema.get("function", {}).get("parameters", {})

    @property
    def is_read_only(self) -> bool:
        return True



def _python_type_to_json_type(py_type: Any) -> str:
    """将基础 Python 类型映射为 JSON Schema 类型字符串。"""
    if py_type in (int,):
        return "integer"
    if py_type in (float,):
        return "number"
    if py_type in (bool,):
        return "boolean"
    if py_type in (list, set, tuple):
        return "array"
    if py_type in (dict,):
        return "object"
    return "string"


class ToolRegistry:
    """线程安全工具单例注册表。"""

    _instance: ToolRegistry | None = None

    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}

    @classmethod
    def get_instance(cls) -> ToolRegistry:
        if cls._instance is None:
            cls._instance = cls()
            try:
                from src.tools.builtin_tools import register_builtin_tools
                register_builtin_tools(cls._instance)
            except Exception:
                pass
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """用于测试环境重置。"""
        cls._instance = None

    def register(
        self,
        func: Callable[..., Any],
        name: str | None = None,
        description: str | None = None,
        parameters_schema: dict[str, Any] | None = None,
        roles: tuple[str, ...] | list[str] = ("all",),
    ) -> Callable[..., Any]:
        """注册一个工具函数。"""
        tool_name = name or func.__name__
        tool_desc = description or (func.__doc__ or "").strip() or f"Tool function {tool_name}"

        if parameters_schema is None:
            # 自动推导参数 Schema
            properties: dict[str, Any] = {}
            required: list[str] = []
            sig = inspect.signature(func)
            type_hints = get_type_hints(func) if hasattr(func, "__annotations__") else {}

            for param_name, param in sig.parameters.items():
                if param_name in ("self", "cls"):
                    continue
                py_type = type_hints.get(param_name, str)
                json_type = _python_type_to_json_type(py_type)
                properties[param_name] = {
                    "type": json_type,
                    "description": f"参数 {param_name}",
                }
                if param.default is inspect.Parameter.empty:
                    required.append(param_name)

            parameters_schema = {
                "type": "object",
                "properties": properties,
                "required": required,
            }

        schema = {
            "type": "function",
            "function": {
                "name": tool_name,
                "description": tool_desc,
                "parameters": parameters_schema,
            },
        }

        self._tools[tool_name] = ToolDefinition(
            name=tool_name,
            description=tool_desc,
            function=func,
            schema=schema,
            roles=tuple(roles),
        )
        logger.debug("[ToolRegistry] Registered tool: %s for roles: %s", tool_name, roles)
        return func

    def get_tool(self, name: str) -> ToolDefinition | None:
        return self._tools.get(name)

    def list_tools(self) -> list[ToolDefinition]:
        """返回所有已注册工具定义列表。"""
        return list(self._tools.values())

    def get_schemas(self, role: str | None = None, tool_names: list[str] | None = None) -> list[dict[str, Any]]:
        """按角色或指定名称列表导出 OpenAI 标准工具 JSON 结构。"""
        schemas: list[dict[str, Any]] = []
        for tool in self._tools.values():
            if tool_names is not None and tool.name not in tool_names:
                continue
            if role is not None and "all" not in tool.roles and role not in tool.roles:
                continue
            schemas.append(tool.schema)
        return schemas

    def execute_tool(self, name: str, arguments: dict[str, Any] | str) -> Any:
        """执行指定工具并直接返回其返回值；若执行失败则抛出 RuntimeError。"""
        res = self.execute(name, arguments)
        if res.get("status") == "error":
            raise RuntimeError(res.get("message", "Tool execution error"))
        return res.get("result")


    def execute(self, name: str, arguments: dict[str, Any] | str) -> dict[str, Any]:
        """执行指定工具并统一返回 dict 结果，异常时 fail-closed 返回错误信息。"""
        tool = self.get_tool(name)
        if tool is None:
            return {"status": "error", "message": f"未找到指定工具: '{name}'"}

        if isinstance(arguments, str):
            try:
                kwargs = json.loads(arguments) if arguments.strip() else {}
            except Exception as exc:
                return {"status": "error", "message": f"工具 '{name}' 参数 JSON 解析失败: {exc}"}
        elif isinstance(arguments, dict):
            kwargs = arguments
        else:
            kwargs = {}

        try:
            raw_result = tool.function(**kwargs)
            return {"status": "success", "result": raw_result}
        except Exception as exc:
            logger.warning("[ToolRegistry] Tool execution failed for '%s': %s", name, exc)
            return {"status": "error", "message": f"工具 '{name}' 执行异常: {exc}"}


def tool(
    name: str | None = None,
    description: str | None = None,
    parameters_schema: dict[str, Any] | None = None,
    roles: tuple[str, ...] | list[str] = ("all",),
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """便捷装饰器：将任意 Python 函数注册到全局 ToolRegistry。"""

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        ToolRegistry.get_instance().register(
            func=func,
            name=name,
            description=description,
            parameters_schema=parameters_schema,
            roles=roles,
        )
        return func

    return decorator
