"""
SEAgent ROS 2 MCP 适配器 (MCP Client Adapter)
提供两大核心能力：
1. 遥测同步：调用 MCP read_topic('/task/system_status')，原子更新 RobotStateInfo
2. 任务下发：将落盘的 TaskIntent JSON 转换为 SysTaskCmd 并调用 MCP publish_topic('/task_cmd')
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# 映射 TaskIntent task_type 到 SysTaskCmd 枚举编号（参考 UI接口协议.md）
TASK_TYPE_MAPPING = {
    "pipeline_inspection": 2,  # SEARCH_CABLE 巡缆/巡线
    "cable_burial": 1,         # CLAMP_CABLE 夹缆/埋设
    "valve_operation": 4,      # INSERT_PLUG / 阀门操作
    "tree_valve_operation": 4, # 插拔/采油树操作
    "underwater_move": 5,      # MOVE_TASK 移动任务
}


class SeagentROS2MCPAdapter:
    def __init__(self, server_script_path: Optional[Path | str] = None):
        if server_script_path is None:
            server_script_path = Path(__file__).resolve().parent / "mock_ros2_mcp_server.py"
        self.server_script_path = str(server_script_path)

    def _get_server_params(self) -> StdioServerParameters:
        # 使用 seagent conda 环境的 python 启动 mock server
        python_bin = "/root/miniconda3/envs/seagent/bin/python"
        return StdioServerParameters(
            command=python_bin,
            args=[self.server_script_path],
            env=None
        )

    async def fetch_and_sync_telemetry(self, state_info) -> Dict[str, Any]:
        """通过 MCP 从 ROS 2 获取实时遥测，并同步到 SEAgent 的 StateInfo"""
        server_params = self._get_server_params()
        async with stdio_client(server_params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                # 调用 MCP 工具 read_topic
                response = await session.call_tool("read_topic", {"topic": "/task/system_status"})
                raw_text = response.content[0].text
                result = json.loads(raw_text)

                if result.get("status") != "success":
                    raise RuntimeError(f"Failed to read topic: {result.get('message')}")

                telemetry_data = result.get("data", {})
                return telemetry_data

    async def dispatch_task_intent(self, task_intent: Dict[str, Any]) -> Dict[str, Any]:
        """将 TaskIntent 字典解析并作为 SysTaskCmd 发布到 ROS 2 /task_cmd

        兼容两种结构：
        - v2: task.details.target.{latitude,longitude} + location.water_depth_m
        - legacy: target.coordinates.{latitude,longitude} + target.depth
        """
        task_type_str = task_intent.get("task_type", "")
        task_cmd_type = TASK_TYPE_MAPPING.get(task_type_str, 5)  # 默认 MOVE_TASK

        unit_id = task_intent.get("equipment", {}).get("robot_unit_id", "WROV-250-001")

        # --- 坐标提取（v2 优先，fallback 到 legacy）---
        coords_v2 = (
            task_intent.get("task", {})
            .get("details", {})
            .get("target", {})
        )
        coords_legacy = task_intent.get("target", {}).get("coordinates", {})
        # v2 结构 target 含 latitude/longitude；latitude 不为 None 则认为是 v2
        coords = coords_v2 if coords_v2.get("latitude") is not None else coords_legacy

        # --- 水深提取（v2 优先，fallback 到 legacy）---
        depth_v2 = task_intent.get("location", {}).get("water_depth_m")
        depth_legacy = task_intent.get("target", {}).get("depth")
        depth = float(depth_v2 if depth_v2 is not None else (depth_legacy or 0.0))

        # 组装符合 SysTaskCmd.msg 契约的 payload
        ros2_task_cmd = {
            "task_type": task_cmd_type,
            "task_id": 0x80001,  # AI 生成的任务 ID 前缀
            "frame_id": "odom",
            "priority": 15,
            "pos_target": [
                {
                    "position": {
                        "x": coords.get("longitude", 0.0),
                        "y": coords.get("latitude", 0.0),
                        "z": -depth,
                    },
                    "orientation": {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0},
                }
            ],
            "params": [
                depth,
                1.5,  # 默认作业速度 1.5 m/s
            ],
            "fail_stop": True,
        }

        server_params = self._get_server_params()
        async with stdio_client(server_params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                # 调用 MCP 工具 publish_topic
                response = await session.call_tool("publish_topic", {
                    "topic": "/task_cmd",
                    "message": ros2_task_cmd
                })
                raw_text = response.content[0].text
                return json.loads(raw_text)

    async def get_received_commands(self) -> Dict[str, Any]:
        """查询 ROS 2 端实际收到的指令列表"""
        server_params = self._get_server_params()
        async with stdio_client(server_params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                response = await session.call_tool("get_received_commands", {})
                return json.loads(response.content[0].text)
