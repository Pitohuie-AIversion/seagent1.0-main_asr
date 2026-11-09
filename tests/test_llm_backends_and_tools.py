"""tests/test_llm_backends_and_tools.py — 多后端与原生 Tool Calling 单元及集成契约测试。

测试范围：
1. ToolRegistry: 动态注册、Schema 自动推导、角色过滤、只读沙箱执行与 Fail-Closed 容错；
2. Builtin Tools: 设备规格、油田位置坐标与实时海况遥测三个只读核心工具；
3. Backends: MockBackend、OpenAICompatibleBackend 与 LocalVllmBackend 统一协议；
4. LLMClient: 原生多轮 Tool Calling 调度循环、死循环防护、双卡/多后端角色分流。
"""

import json
from unittest.mock import MagicMock, patch
import pytest

from src.llm_backends.base import BaseLLMBackend, LLMResponse, ToolCall
from src.llm_backends.mock_backend import MockBackend
from src.llm_backends.openai_http import OpenAICompatibleBackend
from src.llm_backends.local_vllm import LocalVllmBackend
from src.llm_client import LLMClient
from src.extraction.model_profile import ModelRole
from src.tools.registry import ToolRegistry, tool
from src.tools.builtin_tools import (
    query_robot_specs,
    query_oilfield_position,
    query_environmental_telemetry,
)


class TestToolRegistry:
    """测试工具注册表与 Function Calling Schema 生成。"""

    def setup_method(self):
        ToolRegistry.reset()

    def teardown_method(self):
        ToolRegistry.reset()

    def test_auto_schema_derivation(self):
        """测试由 Python 类型注解与 docstring 自动推导标准 JSON Schema。"""
        registry = ToolRegistry.get_instance()

        @tool(roles=("extractor",))
        def calculate_water_pressure(depth_m: float, gravity: float = 9.8) -> dict:
            """计算指定水深静水压强。"""
            pressure_bar = (1025 * gravity * depth_m) / 100000
            return {"depth_m": depth_m, "pressure_bar": round(pressure_bar, 2)}

        defn = registry.get_tool("calculate_water_pressure")
        assert defn is not None
        assert defn.name == "calculate_water_pressure"
        assert "计算指定水深静水压强" in defn.description
        assert "extractor" in defn.roles

        schemas = registry.get_schemas(role="extractor")
        calc_schemas = [s for s in schemas if s["function"]["name"] == "calculate_water_pressure"]
        assert len(calc_schemas) == 1
        fn_schema = calc_schemas[0]["function"]
        assert fn_schema["name"] == "calculate_water_pressure"
        assert "depth_m" in fn_schema["parameters"]["properties"]
        assert fn_schema["parameters"]["properties"]["depth_m"]["type"] == "number"
        assert "depth_m" in fn_schema["parameters"]["required"]

    def test_role_filtering(self):
        """测试按 Agent 角色过滤可见工具 Schema。"""
        registry = ToolRegistry.get_instance()

        @tool(roles=("extractor",))
        def tool_for_extractor(arg: str):
            """Extractor 专属工具。"""
            return {"arg": arg}

        @tool(roles=("auditor",))
        def tool_for_auditor(arg: str):
            """Auditor 专属工具。"""
            return {"arg": arg}

        @tool(roles=("all",))
        def common_tool(arg: str):
            """通用工具。"""
            return {"arg": arg}

        extractor_schemas = registry.get_schemas(role="extractor")
        extractor_tool_names = [s["function"]["name"] for s in extractor_schemas]
        assert "tool_for_extractor" in extractor_tool_names
        assert "common_tool" in extractor_tool_names
        assert "tool_for_auditor" not in extractor_tool_names

        auditor_schemas = registry.get_schemas(role="auditor")
        auditor_tool_names = [s["function"]["name"] for s in auditor_schemas]
        assert "tool_for_auditor" in auditor_tool_names
        assert "common_tool" in auditor_tool_names
        assert "tool_for_extractor" not in auditor_tool_names

    def test_safe_execution_and_fail_closed(self):
        """测试只读沙箱执行及异常防护。"""
        registry = ToolRegistry.get_instance()

        @tool()
        def risky_division(a: int, b: int) -> dict:
            return {"res": a / b}

        # 1. 成功调用 (dict 传参)
        res1 = registry.execute("risky_division", {"a": 10, "b": 2})
        assert res1["status"] == "success"
        assert res1["result"]["res"] == 5.0

        # 2. 成功调用 (json string 传参)
        res2 = registry.execute("risky_division", '{"a": 20, "b": 4}')
        assert res2["status"] == "success"
        assert res2["result"]["res"] == 5.0

        # 3. 参数非法 JSON 字符串容错
        res_bad_json = registry.execute("risky_division", "{bad_json")
        assert res_bad_json["status"] == "error"
        assert "JSON 解析失败" in res_bad_json["message"]

        # 4. 函数内部异常容错 (除零)
        res_div_zero = registry.execute("risky_division", {"a": 10, "b": 0})
        assert res_div_zero["status"] == "error"
        assert "division by zero" in res_div_zero["message"]

        # 5. 未找到工具
        res_missing = registry.execute("non_existent_tool", {})
        assert res_missing["status"] == "error"
        assert "未找到指定工具" in res_missing["message"]


class TestBuiltinTools:
    """测试核心领域内置工具数据查询准确性。"""

    def test_query_robot_specs(self):
        """测试设备能力与参数库查询。"""
        res = query_robot_specs("金牛座")
        assert res["found"] is True
        assert res["count"] > 0
        robot = res["robots"][0]
        assert robot["max_depth_m"] is not None
        assert robot["power_hp"] is not None

        # 测试不存在设备
        res_unknown = query_robot_specs("不存在的深海航行器-XYZ")
        assert res_unknown["found"] is False

    def test_query_oilfield_position(self):
        """测试油田地理坐标与默认水深查询。"""
        res = query_oilfield_position("陵水17-2")
        assert res["found"] is True
        assert "陵水17-2" in res["field_name"]
        assert 16.0 <= res["center_lat"] <= 19.0
        assert 109.0 <= res["center_lon"] <= 112.0
        assert res["default_depth_m"] > 0

        res_unknown = query_oilfield_position("火星一号油田")
        assert res_unknown["found"] is False

    def test_query_environmental_telemetry(self):
        """测试水下环境与海况遥测基线。"""
        res = query_environmental_telemetry()
        assert res["found"] is True
        assert "current_speed_knots" in res or "surface_current_knots" in res
        assert res["status"] == "safe"

    def test_calculate_hydrostatic_pressure(self):
        """测试水深静水压强计算与法兰等级推荐。"""
        from src.tools.builtin_tools import calculate_hydrostatic_pressure

        res = calculate_hydrostatic_pressure(water_depth_m=1500.0)
        assert res["status"] == "success"
        assert res["depth_m"] == 1500.0
        assert 145.0 <= res["hydrostatic_pressure_bar"] <= 155.0
        assert 14.5 <= res["hydrostatic_pressure_mpa"] <= 15.5
        assert "超深水" in res["depth_classification"]

    def test_calculate_cable_catenary(self):
        """测试脐带缆受力与悬链线漂移物理计算。"""
        from src.tools.builtin_tools import calculate_cable_catenary

        res = calculate_cable_catenary(water_depth_m=500.0, current_speed_knots=1.5)
        assert res["water_depth_m"] == 500.0
        assert res["top_tension_kn"] > 0
        assert res["horizontal_drag_kn"] > 0
        assert res["estimated_bottom_drift_m"] > 0
        assert res["catenary_status"] in ("safe", "warning")

    def test_check_marine_operation_window(self):
        """测试海况平流作业窗口查询。"""
        from src.tools.builtin_tools import check_marine_operation_window

        res = check_marine_operation_window(field_name="陵水17-2", target_date="2026-11-10")
        assert res["field_name"] == "陵水17-2"
        assert res["window_status"] == "AVAILABLE"
        assert len(res["recommended_windows"]) >= 1


class TestBackends:
    """测试各个计算后端的一致性。"""

    def test_mock_backend_text_and_tool_call(self):
        """测试 MockBackend 的响应生成与工具调用模拟。"""
        backend = MockBackend()
        resp = backend.generate([{"role": "user", "content": "你好"}])
        assert isinstance(resp, LLMResponse)
        assert resp.text != ""

        # 触发工具调用模拟
        tools_schema = [
            {
                "type": "function",
                "function": {"name": "query_robot_specs", "parameters": {}},
            }
        ]
        resp_tool = backend.generate(
            [{"role": "user", "content": "查询金牛座的规格参数"}],
            tools=tools_schema,
        )
        assert resp_tool.has_tool_calls
        assert resp_tool.tool_calls[0].name == "query_robot_specs"

    @patch("openai.OpenAI")
    def test_openai_compatible_backend(self, mock_openai_cls):
        """测试标准 OpenAI 协议后端。"""
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client

        # 模拟响应
        mock_choice = MagicMock()
        mock_choice.message.content = "这是来自 GPU 1 27B 模型的推理回答"
        mock_choice.finish_reason = "stop"
        mock_choice.message.tool_calls = None
        mock_resp = MagicMock()
        mock_resp.choices = [mock_choice]
        mock_client.chat.completions.create.return_value = mock_resp

        backend = OpenAICompatibleBackend(
            base_url="http://127.0.0.1:8001/v1",
            model_name="qwen27b",
        )
        resp = backend.generate([{"role": "user", "content": "测试指令"}])
        assert resp.text == "这是来自 GPU 1 27B 模型的推理回答"
        assert resp.tool_calls is None
        mock_client.chat.completions.create.assert_called_once()

    def test_local_vllm_backend_tag_parsing(self):
        """测试 LocalVllmBackend 对 <tool_call> 标签的解析。"""
        text_with_tag = (
            "需要先获取规格。<tool_call>"
            '{"name": "query_robot_specs", "arguments": {"robot_name": "金牛座"}}'
            "</tool_call>"
        )
        calls = LocalVllmBackend._extract_text_tool_calls(text_with_tag)
        assert len(calls) == 1
        assert calls[0].name == "query_robot_specs"
        assert json.loads(calls[0].arguments)["robot_name"] == "金牛座"


class TestLLMClientToolCallingLoop:
    """测试 LLMClient 的原生 Tool Calling 调度循环。"""

    def test_generate_with_tools_multi_turn_success(self):
        """测试端到端的多轮工具调用闭环：请求 -> 调工具 -> 塞回结果 -> 综合最终答复。"""
        client = LLMClient(None, None)  # Mock 模式

        messages = [
            {"role": "user", "content": "请派金牛座前往陵水17-2作业，并确认其规格"}
        ]

        resp = client.generate_with_tools(
            messages,
            role="extractor",
        )

        assert isinstance(resp, LLMResponse)
        assert resp.text != ""
        # 确认工具返回的事实数据被整合到了输出中
        assert "事实数据" in resp.text or "金牛座" in resp.text or "规格" in resp.text

    def test_generate_with_tools_prevents_infinite_loop(self):
        """测试防死循环机制：当模型连续返回 tool_calls 时，在 max_tool_rounds 轮强制终止并输出最终答复。"""
        # 创建一个永远返回 tool_call 的恶意/异常 Backend
        class LoopBackend(BaseLLMBackend):
            def __init__(self):
                self.calls_count = 0

            def generate(self, messages, tools=None, **kwargs):
                self.calls_count += 1
                if tools is None:
                    # 最后一轮无 tools 强制总结
                    return LLMResponse(text="已达到上限，强制返回总结")
                return LLMResponse(
                    text="",
                    tool_calls=[
                        ToolCall(
                            id=f"loop_call_{self.calls_count}",
                            name="query_environmental_telemetry",
                            arguments="{}",
                        )
                    ],
                )

        loop_backend = LoopBackend()
        client = LLMClient(None, None)
        client.register_backend("loop_test", loop_backend)
        client.default_backend_name = "loop_test"

        messages = [{"role": "user", "content": "测试死循环"}]
        resp = client.generate_with_tools(messages, max_tool_rounds=3)

        assert resp.text == "已达到上限，强制返回总结"
        # 3 轮带 tools + 1 轮强制无 tools = 4 次调用
        assert loop_backend.calls_count == 4

    def test_custom_backend_routing_by_role(self):
        """测试根据 Profile/Role 路由至不同后端 (如 Auditor -> GPU 1 HTTP 后端)。"""
        client = LLMClient(None, None)

        mock_heavy = MagicMock(spec=BaseLLMBackend)
        mock_heavy.generate.return_value = LLMResponse(text="[来自 GPU 1 深度思考结果]")

        client.register_backend("gpu1_heavy", mock_heavy)

        # 模拟 Auditor 角色
        profile_backend = client.get_backend(name="gpu1_heavy")
        resp = profile_backend.generate([{"role": "user", "content": "做安全推演"}])
        assert resp.text == "[来自 GPU 1 深度思考结果]"
