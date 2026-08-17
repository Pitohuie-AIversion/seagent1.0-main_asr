"""
端到端 ROS 2 MCP 集成测试 (E2E Integration Test)
验证流程：
1. 启动并连接 Mock ROS 2 MCP Server
2. 测试遥测同步：调用 MCP 读取 /task/system_status 并原子写入 SEAgent RobotStateInfo
3. 测试意图下发：模拟生成一个 TaskIntent，通过 MCP 下发给 /task_cmd
4. 验证 Mock ROS 2 服务端成功收到符合 SysTaskCmd 格式的控制消息
"""

import asyncio
import json
import pytest
import sys
from pathlib import Path

# 将项目 src 目录加入 Python 搜索路径
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.state_info import RobotStateInfo
from scratch.ros2_mcp_test.seagent_mcp_adapter import SeagentROS2MCPAdapter


@pytest.fixture(autouse=True)
def cleanup_temp_storage():
    tmp_file = Path("/tmp/mock_ros2_received_cmds.json")
    if tmp_file.exists():
        tmp_file.unlink()
    yield
    if tmp_file.exists():
        tmp_file.unlink()


@pytest.fixture
def mcp_adapter():
    server_script = Path(__file__).parent / "mock_ros2_mcp_server.py"
    return SeagentROS2MCPAdapter(server_script)


@pytest.fixture
def state_info(tmp_path):
    # 使用临时文件测试 RobotStateInfo，不污染项目正式配置文件
    state_file = tmp_path / "test_state.yaml"
    fleet_file = PROJECT_ROOT / "config" / "robot_fleet.yaml"
    
    # 初始化空的测试 state 文件
    initial_content = "store_version: 0\nrobots: {}\n"
    state_file.write_text(initial_content, encoding="utf-8")
    
    return RobotStateInfo(state_file=state_file, fleet_file=fleet_file)


def test_01_telemetry_sync_from_ros2_mcp(mcp_adapter, state_info):
    """测试场景 1：通过 MCP 从 ROS 2 读取遥测并同步至 SEAgent 状态中心"""
    async def _run():
        print("\n--- [Test 1] 正在通过 MCP 读取 ROS 2 实时遥测 ---")
        telemetry = await mcp_adapter.fetch_and_sync_telemetry(state_info)
        
        assert "WROV-250-001" in telemetry
        assert telemetry["WROV-250-001"]["current_depth"] == 312.4
        assert telemetry["WROV-250-001"]["battery_percentage"] == 94.5

        # 验证 SEAgent 的 RobotStateInfo 已经成功原子持久化了该机器人的最新状态
        unit_snapshot = state_info.get_unit_state_snapshot("WROV-250-001")
        assert unit_snapshot["unit_id"] == "WROV-250-001"
        assert unit_snapshot["state"]["water_depth"] == 312.4
        assert unit_snapshot["state"]["battery_level"] == 94.5
        assert unit_snapshot["state"]["status"] == "online"
        print("✅ 遥测成功通过 MCP 写入 SEAgent StateInfo 并通过 get_unit_state_snapshot 校验!")

    asyncio.run(_run())


def test_02_dispatch_task_intent_via_mcp(mcp_adapter):
    """测试场景 2：将 SEAgent 生成的 TaskIntent 通过 MCP 下发为 ROS 2 SysTaskCmd 消息"""
    async def _run():
        print("\n--- [Test 2] 正在通过 MCP 下发 TaskIntent 至 ROS 2 /task_cmd ---")
        
        # 模拟 SEAgent 规划好的 TaskIntent
        mock_task_intent = {
            "intent_id": "TI20260816_001",
            "task_type": "pipeline_inspection",
            "equipment": {
                "robot_type": "work_class_rov",
                "robot_unit_id": "WROV-250-001"
            },
            "target": {
                "oilfield": "流花油田",
                "depth": 300.0,
                "coordinates": {
                    "longitude": 115.3421,
                    "latitude": 20.8912
                }
            },
            "time": {
                "scheduled_start_time": "2026-08-17T08:00:00+08:00"
            }
        }

        # 执行 MCP 下发
        result = await mcp_adapter.dispatch_task_intent(mock_task_intent)
        assert result.get("status") == "success"
        print(f"✅ 下发结果: {result.get('message')}")

        # 验证 ROS 2 服务端收到的数据
        commands_info = await mcp_adapter.get_received_commands()
        assert commands_info["total"] >= 1
        last_cmd = commands_info["commands"][-1]["payload"]
        
        # 验证消息字段是否符合 SysTaskCmd.msg
        assert last_cmd["task_type"] == 2 # SEARCH_CABLE 巡缆/巡检
        assert last_cmd["pos_target"][0]["position"]["z"] == -300.0
        assert last_cmd["params"][0] == 300.0
        print("✅ ROS 2 服务端成功接收并校验了 SysTaskCmd 指令!")

    asyncio.run(_run())


if __name__ == "__main__":
    pytest.main([__file__, "-s", "-v"])
