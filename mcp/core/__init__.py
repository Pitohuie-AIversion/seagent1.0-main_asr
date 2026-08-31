"""
mcp.core
========
SEAgent 生产级底层驱动核心包
"""

import sys
from pathlib import Path

CORE_DIR = Path(__file__).resolve().parent
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

from .sealien_protocol import (
    LocalOrigin,
    TaskMessageGuard,
    RequestIdGuard,
    geodetic_to_enu,
    geodetic_to_odom_position,
    yaw_between,
    pose,
)
from .rosbridge_client import (
    RosbridgeClient,
    TaskType,
    PilotMode,
    TaskManageAction,
    TaskStatus,
    TaskStatusItem,
    SysTaskCmd,
    Pose,
    SEAGENT_TO_ROS2_TASK_TYPE,
    intent_to_syscmd,
    build_task_manage_cmd,
    generate_task_id,
)
from .task_status_tracker import (
    TaskStatusTracker,
    ROVTelemetry,
)
from .bridge_service import (
    SEAgentMCPBridgeService,
)
from .dialogue_mcp_integration import (
    attach_mcp_bridge,
    dispatch_dialogue_result,
)

__all__ = [
    "LocalOrigin",
    "TaskMessageGuard",
    "RequestIdGuard",
    "geodetic_to_enu",
    "geodetic_to_odom_position",
    "yaw_between",
    "pose",
    "RosbridgeClient",
    "TaskType",
    "PilotMode",
    "TaskManageAction",
    "TaskStatus",
    "TaskStatusItem",
    "SysTaskCmd",
    "Pose",
    "SEAGENT_TO_ROS2_TASK_TYPE",
    "intent_to_syscmd",
    "build_task_manage_cmd",
    "generate_task_id",
    "TaskStatusTracker",
    "ROVTelemetry",
    "SEAgentMCPBridgeService",
    "attach_mcp_bridge",
    "dispatch_dialogue_result",
]
