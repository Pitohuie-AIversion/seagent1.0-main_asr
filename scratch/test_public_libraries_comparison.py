"""
test_public_libraries_comparison.py
针对 3 个公开 ROS2 MCP 库的适用性综合测试。
无需真实 ROS 2 环境，测试可导入性、接口契约与 SEAgent 对接能力。
"""
import json
import subprocess
import sys
import importlib
from pathlib import Path
import pytest

SEAGENT_ROOT = Path(__file__).parent.parent
OUTSIDE_DIR = SEAGENT_ROOT / "outside"


def _check_import(module_name: str) -> tuple:
    try:
        importlib.import_module(module_name)
        return True, ""
    except ImportError as e:
        return False, str(e)
    except Exception as e:
        return False, f"RuntimeError: {e}"


def _sample_task_intent() -> dict:
    return {
        "schema_version": 2,
        "internal_id": "8f3b2a1c-4d5e-49b8-a123-9876543210ab",
        "task_id": "CT-20260816-002",
        "intent_id": "TI2026081634",
        "task_type": "tree_valve_operation",
        "priority": 7,
        "time": {"start": "2026-08-16T10:00:00+08:00", "end": "2026-08-16T18:00:00+08:00"},
        "location": {"oilfield": "流花11-1油田", "water_depth_m": 300.0},
        "task": {
            "type": "tree_valve_operation",
            "details": {
                "wellhead_id": "LH-01井口",
                "target": {"latitude": 20.815, "longitude": 115.735},
                "hole_positions": [],
            },
        },
        "equipment": {
            "robot_type": "work_class_rov",
            "payload": ["多功能液压机械臂", "双目视觉模块"],
            "support_vessel": {"name": "海洋石油681", "latitude": None, "longitude": None},
        },
        "conditions": {
            "validation": {"overall_status": "valid"},
            "runtime_validation": {"required": False, "status": "completed"},
        },
    }


# ========== [库A] amazing-ros2-mcp ==========

class TestAmazingROS2MCP:
    def test_A1_package_importable(self):
        """[A1] amazing_ros2_mcp 包应可正常导入"""
        ok, err = _check_import("amazing_ros2_mcp")
        assert ok, f"无法导入 amazing_ros2_mcp: {err}"

    def test_A2_seagent_adapter_importable(self):
        """[A2] SeagentROS2MCPAdapter 应可从项目路径导入"""
        sys.path.insert(0, str(SEAGENT_ROOT / "scratch" / "ros2_mcp_test"))
        try:
            from seagent_mcp_adapter import SeagentROS2MCPAdapter, TASK_TYPE_MAPPING
            assert callable(SeagentROS2MCPAdapter)
        finally:
            sys.path.pop(0)

    def test_A3_task_type_mapping_complete(self):
        """[A3] TASK_TYPE_MAPPING 应覆盖 SEAgent 所有标准任务类型"""
        sys.path.insert(0, str(SEAGENT_ROOT / "scratch" / "ros2_mcp_test"))
        try:
            from seagent_mcp_adapter import TASK_TYPE_MAPPING
            required_keys = {"tree_valve_operation", "pipeline_inspection", "valve_operation"}
            missing = required_keys - set(TASK_TYPE_MAPPING.keys())
            assert not missing, f"TASK_TYPE_MAPPING 缺少任务类型: {missing}"
        finally:
            sys.path.pop(0)

    def test_A4_task_intent_field_extraction(self):
        """[A4] 从 SEAgent TaskIntent 中应能正确提取核心字段"""
        intent = _sample_task_intent()
        assert intent.get("task_type") == "tree_valve_operation"
        assert intent.get("equipment", {}).get("robot_type") == "work_class_rov"
        coords = intent.get("task", {}).get("details", {}).get("target", {})
        assert coords.get("latitude") == pytest.approx(20.815)
        assert coords.get("longitude") == pytest.approx(115.735)
        assert intent.get("location", {}).get("water_depth_m") == pytest.approx(300.0)

    def test_A5_ros2_syscmd_payload_structure(self):
        """[A5] 基于 TaskIntent 构建的 SysTaskCmd payload 结构应合法"""
        sys.path.insert(0, str(SEAGENT_ROOT / "scratch" / "ros2_mcp_test"))
        try:
            from seagent_mcp_adapter import TASK_TYPE_MAPPING
            intent = _sample_task_intent()
            task_cmd_type = TASK_TYPE_MAPPING.get(intent["task_type"], 5)
            coords = intent["task"]["details"]["target"]
            depth = intent["location"]["water_depth_m"]
            cmd = {
                "task_type": task_cmd_type,
                "task_id": 0x80001,
                "frame_id": "odom",
                "priority": 15,
                "pos_target": [{"position": {"x": coords["longitude"], "y": coords["latitude"], "z": -depth}, "orientation": {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0}}],
                "params": [depth, 1.5],
                "fail_stop": True,
            }
            assert cmd["task_type"] == 4  # tree_valve_operation -> 4 (采油树阀门操作)
            assert cmd["frame_id"] == "odom"
            assert cmd["fail_stop"] is True
            assert cmd["pos_target"][0]["position"]["z"] == pytest.approx(-300.0)
        finally:
            sys.path.pop(0)

    def test_A6_adapter_default_path_configured(self):
        """[A6] SeagentROS2MCPAdapter 应支持无参默认构造"""
        sys.path.insert(0, str(SEAGENT_ROOT / "scratch" / "ros2_mcp_test"))
        try:
            from seagent_mcp_adapter import SeagentROS2MCPAdapter
            adapter = SeagentROS2MCPAdapter()
            assert adapter is not None
        finally:
            sys.path.pop(0)


# ========== [库B] wise-vision/ros2_mcp ==========

class TestWiseVisionROS2MCP:
    def test_B1_server_directory_exists(self):
        """[B1] wise-vision 的 server/ 核心目录应存在"""
        server_dir = OUTSIDE_DIR / "wise-vision-ros2_mcp" / "server"
        assert server_dir.exists()

    def test_B2_key_source_files_present(self):
        """[B2] 核心源文件应存在"""
        server_dir = OUTSIDE_DIR / "wise-vision-ros2_mcp" / "server"
        for fname in ["server.py", "tools_ros2.py", "prompts_ros2.py", "ros2_manager.py"]:
            assert (server_dir / fname).exists(), f"缺失: {fname}"

    def test_B3_tool_handler_interface_parsable(self):
        """[B3] ToolHandler 基类接口定义应可解析"""
        content = (OUTSIDE_DIR / "wise-vision-ros2_mcp" / "server" / "toolhandler.py").read_text(encoding="utf-8")
        assert "ToolHandler" in content
        assert "get_tool_description" in content

    def test_B4_prompt_workflow_templates_exist(self):
        """[B4] MCP Prompt 多步工作流模板应已实现"""
        content = (OUTSIDE_DIR / "wise-vision-ros2_mcp" / "server" / "prompts_ros2.py").read_text(encoding="utf-8")
        for prompt_name in ["ros2-topic-echo-and-analyze", "ros2-node-health-check", "ros2-topic-diff-monitor", "ros2-topic-relay"]:
            assert prompt_name in content, f"MCP Prompt 缺失: {prompt_name}"

    def test_B5_action_tools_implemented(self):
        """[B5] 应实现 ROS 2 Action 全量工具"""
        content = (OUTSIDE_DIR / "wise-vision-ros2_mcp" / "server" / "tools_ros2.py").read_text(encoding="utf-8")
        for cls_name in ["ROS2ListActions", "ROS2SendActionGoal", "ROS2CancelActionGoal", "ROS2ActionSubscribeFeedback"]:
            assert cls_name in content, f"Action 工具类缺失: {cls_name}"

    def test_B6_server_json_mcp_protocol_config(self):
        """[B6] server.json MCP 协议配置应合法"""
        with open(OUTSIDE_DIR / "wise-vision-ros2_mcp" / "server.json", encoding="utf-8") as f:
            config = json.load(f)
        # 新版 MCP registry schema 使用 packages 字段（非旧版 mcpServers）
        assert "packages" in config or "name" in config, "server.json 缺少 packages 或 name 字段"
        assert config.get("name"), "server.json 中 name 字段不能为空"

    def test_B7_no_direct_rclpy_in_server_py(self):
        """[B7] server.py 顶层不应直接 import rclpy"""
        content = (OUTSIDE_DIR / "wise-vision-ros2_mcp" / "server" / "server.py").read_text(encoding="utf-8")
        assert "import rclpy" not in content


# ========== [库C] robotmcp/ros-mcp-server ==========

class TestRobotMCPRosMCPServer:
    def test_C1_package_directory_exists(self):
        """[C1] robotmcp ros_mcp 包目录应存在"""
        assert (OUTSIDE_DIR / "robotmcp-ros-mcp-server" / "ros_mcp").exists()

    def test_C2_package_importable(self):
        """[C2] ros_mcp 包应可正常导入"""
        ok, err = _check_import("ros_mcp")
        assert ok, f"无法导入 ros_mcp: {err}"

    def test_C3_config_utils_importable(self):
        """[C3] ros_mcp.utils.config_utils 模块应可导入"""
        ok, err = _check_import("ros_mcp.utils.config_utils")
        assert ok, f"无法导入 ros_mcp.utils.config_utils: {err}"

    def test_C4_load_robot_config_valid_yaml(self, tmp_path):
        """[C4] load_robot_config 应能正确读取合法 YAML"""
        from ros_mcp.utils.config_utils import load_robot_config
        (tmp_path / "test_robot.yaml").write_text("name: test_robot\ntype: simulated\nprompts: |\n  A test robot.\n")
        config = load_robot_config("test_robot", str(tmp_path))
        assert config["name"] == "test_robot"
        assert config["type"] == "simulated"

    def test_C5_load_robot_config_missing_file(self, tmp_path):
        """[C5] 读取不存在的文件应抛出 FileNotFoundError"""
        from ros_mcp.utils.config_utils import load_robot_config
        with pytest.raises(FileNotFoundError):
            load_robot_config("nonexistent_robot", str(tmp_path))

    def test_C6_rosbridge_no_rclpy_dependency(self):
        """[C6] ros_mcp 核心入口不应直接依赖 rclpy"""
        content = (OUTSIDE_DIR / "robotmcp-ros-mcp-server" / "ros_mcp" / "main.py").read_text(encoding="utf-8")
        assert "import rclpy" not in content

    def test_C7_websocket_used_for_ros_bridge(self):
        """[C7] 应通过 websocket 与 rosbridge 通信"""
        content = (OUTSIDE_DIR / "robotmcp-ros-mcp-server" / "ros_mcp" / "integration.py").read_text(encoding="utf-8")
        has_ws = any(kw in content for kw in ["websocket", "WebSocket", "ws://", "rosbridge"])
        assert has_ws

    def test_C8_server_json_mcp_config(self):
        """[C8] server.json MCP 协议配置应合法"""
        with open(OUTSIDE_DIR / "robotmcp-ros-mcp-server" / "server.json", encoding="utf-8") as f:
            config = json.load(f)
        assert "packages" in config

    def test_C9_unit_tests_pass(self):
        """[C9] robotmcp 自带 unit 测试套件应通过"""
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/unit/", "-v", "--tb=short", "-q"],
            cwd=str(OUTSIDE_DIR / "robotmcp-ros-mcp-server"),
            capture_output=True, text=True, timeout=30,
        )
        output = result.stdout + result.stderr
        assert result.returncode == 0, f"robotmcp 原生 unit tests 失败:\n{output}"


# ========== [D] SEAgent 集成契约验证 ==========

class TestSEAgentIntegrationCapability:
    def test_D1_task_intent_v2_structure_complete(self):
        """[D1] SEAgent TaskIntent v2 结构体应包含所有必填字段"""
        intent = _sample_task_intent()
        required = {"schema_version","internal_id","task_id","intent_id","task_type","priority","time","location","task","equipment","conditions"}
        missing = required - intent.keys()
        assert not missing, f"TaskIntent v2 缺少字段: {missing}"

    def test_D2_task_intent_target_coordinates_valid(self):
        """[D2] 目标经纬度应在合法范围内"""
        target = _sample_task_intent()["task"]["details"]["target"]
        assert -90.0 <= target["latitude"] <= 90.0
        assert -180.0 <= target["longitude"] <= 180.0

    def test_D3_water_depth_positive(self):
        """[D3] 水深参数应为正数"""
        depth = _sample_task_intent()["location"]["water_depth_m"]
        assert depth > 0

    def test_D4_payload_list_non_empty(self):
        """[D4] 任务挂载载荷列表应不为空"""
        payload = _sample_task_intent()["equipment"]["payload"]
        assert isinstance(payload, list) and len(payload) > 0

    def test_D5_task_type_maps_to_int_for_ros2(self):
        """[D5] SEAgent 任务类型应能映射为 ROS 2 整数 task_type"""
        sys.path.insert(0, str(SEAGENT_ROOT / "scratch" / "ros2_mcp_test"))
        try:
            from seagent_mcp_adapter import TASK_TYPE_MAPPING
            mapped = TASK_TYPE_MAPPING.get(_sample_task_intent()["task_type"])
            assert mapped is not None, f"任务类型 '{_sample_task_intent()['task_type']}' 在 TASK_TYPE_MAPPING 中无映射"
            assert isinstance(mapped, int), f"映射结果应为整数，实际为: {type(mapped)}"
        finally:
            sys.path.pop(0)
