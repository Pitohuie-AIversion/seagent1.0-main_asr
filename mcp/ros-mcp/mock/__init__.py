"""
mcp.mock
========
SEAgent 本地仿真服务器与适配器包
"""

import sys
from pathlib import Path

MOCK_DIR = Path(__file__).resolve().parent
if str(MOCK_DIR) not in sys.path:
    sys.path.insert(0, str(MOCK_DIR))

from .mock_rosbridge_server import MockRosbridgeServer
try:
    from .mock_ros2_mcp_server import mcp as mock_ros2_mcp_app
except Exception:
    mock_ros2_mcp_app = None
from .seagent_mcp_adapter import SeagentROS2MCPAdapter, TASK_TYPE_MAPPING
from .run_mcp_bridge import main as run_mcp_bridge_main

__all__ = [
    "MockRosbridgeServer",
    "mock_ros2_mcp_app",
    "SeagentROS2MCPAdapter",
    "TASK_TYPE_MAPPING",
    "run_mcp_bridge_main",
]
