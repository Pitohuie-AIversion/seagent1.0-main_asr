"""
Mock ROS 2 MCP Server (使用 FastMCP 模拟 ROS 2 系统)
模拟 ROS 2 机器人控制器的核心功能：
1. 话题订阅与遥测广播：模拟 /task/system_status
2. 话题发布与指令接收：模拟 /task_cmd 与 /task/sys_config
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict
try:
    from fastmcp import FastMCP
    mcp = FastMCP("Mock_ROS2_Control_System")
except Exception:
    mcp = None

STORAGE_FILE = Path("/tmp/mock_ros2_received_cmds.json")

_MOCK_ROBOT_STATE = {
    "WROV-250-001": {
        "unit_id": "WROV-250-001",
        "name": "通用工作级深海机器人250HP-001",
        "online": True,
        "battery_percentage": 94.5,
        "current_depth": 312.4,
        "pose": {"x": 115.3421, "y": 20.8912, "z": -312.4, "yaw": 1.57},
        "twist": {"linear_x": 0.5, "angular_z": 0.0},
        "alt": 15.2,
        "active_tasks": [],
        "last_update": datetime.now(timezone.utc).isoformat(),
    },
    "LROV-150-001": {
        "unit_id": "LROV-150-001",
        "name": "轻型工作级深海机器人150HP-001",
        "online": True,
        "battery_percentage": 88.0,
        "current_depth": 85.0,
        "pose": {"x": 109.1234, "y": 18.5432, "z": -85.0, "yaw": 0.78},
        "twist": {"linear_x": 0.2, "angular_z": 0.0},
        "alt": 20.0,
        "active_tasks": [],
        "last_update": datetime.now(timezone.utc).isoformat(),
    }
}


def _load_commands():
    if STORAGE_FILE.exists():
        try:
            return json.loads(STORAGE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


def _save_command(record):
    cmds = _load_commands()
    cmds.append(record)
    STORAGE_FILE.write_text(json.dumps(cmds, ensure_ascii=False, indent=2), encoding="utf-8")


@mcp.tool()
def read_topic(topic: str) -> str:
    """读取指定 ROS 2 话题的最新消息（模拟 rclpy 订阅）"""
    if topic == "/task/system_status" or topic.endswith("/system_status"):
        return json.dumps({
            "status": "success",
            "topic": topic,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "data": _MOCK_ROBOT_STATE
        }, ensure_ascii=False)
    
    return json.dumps({
        "status": "error",
        "message": f"Topic '{topic}' not found or no publisher"
    }, ensure_ascii=False)


@mcp.tool()
def publish_topic(topic: str, message: Dict[str, Any]) -> str:
    """向指定 ROS 2 话题发布消息（模拟 rclpy 发布 /task_cmd）"""
    if topic == "/task_cmd":
        record = {
            "received_at": datetime.now(timezone.utc).isoformat(),
            "payload": message
        }
        _save_command(record)
        task_type = message.get("task_type", "UNKNOWN")
        task_id = message.get("task_id", 0)
        return json.dumps({
            "status": "success",
            "message": f"Successfully published to /task_cmd. Task ID: {task_id}, Type: {task_type}",
            "command_count": len(_load_commands())
        }, ensure_ascii=False)

    elif topic == "/task/sys_config":
        ctr_mode = message.get("ctr_mode", 0)
        return json.dumps({
            "status": "success",
            "message": f"Config updated: ctr_mode set to {ctr_mode}"
        }, ensure_ascii=False)

    return json.dumps({
        "status": "error",
        "message": f"Unsupported topic '{topic}'"
    }, ensure_ascii=False)


@mcp.tool()
def get_received_commands() -> str:
    """查询 Mock ROS 2 节点接收到的所有任务指令"""
    cmds = _load_commands()
    return json.dumps({
        "total": len(cmds),
        "commands": cmds
    }, ensure_ascii=False)


if __name__ == "__main__":
    mcp.run()
