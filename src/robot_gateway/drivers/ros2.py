"""ROS 2 Hardware/Bridge Driver wrapping mcp/ros-mcp RosbridgeClient and TaskStatusTracker."""
from __future__ import annotations

import logging
import uuid
from typing import Any
from datetime import datetime, timezone

from ..driver import BaseRobotDriver
from ..models import (
    CommandExecutionStatus,
    RobotCommandResult,
    RobotConnectionStatus,
    StandardTelemetrySnapshot,
)

logger = logging.getLogger(__name__)


class ROS2BridgeDriver(BaseRobotDriver):
    """Production driver connecting to ROS 2 through Rosbridge WebSocket."""

    def __init__(
        self,
        unit_id: str = "ROS2-ROV-001",
        host: str = "127.0.0.1",
        port: int = 9090,
    ):
        super().__init__(unit_id=unit_id, driver_name="ros2_driver")
        self.host = host
        self.port = port
        self._client: Any = None
        self._tracker: Any = None

    def connect(self) -> bool:
        try:
            from mcp.ros_mcp.core.rosbridge_client import RosbridgeClient
            from mcp.ros_mcp.core.task_status_tracker import TaskStatusTracker

            self._client = RosbridgeClient(host=self.host, port=self.port)
            connected = self._client.connect()
            if connected:
                self._tracker = TaskStatusTracker(self._client)
                self._tracker.start()
                self._status = RobotConnectionStatus.CONNECTED
                return True
            else:
                self._status = RobotConnectionStatus.ERROR
                return False
        except Exception as exc:
            logger.warning("[ROS2BridgeDriver] Connect failed: %s", exc)
            self._status = RobotConnectionStatus.ERROR
            return False

    def disconnect(self) -> None:
        if self._tracker:
            try:
                self._tracker.stop()
            except Exception:
                pass
            self._tracker = None

        if self._client:
            try:
                self._client.disconnect()
            except Exception:
                pass
            self._client = None

        self._status = RobotConnectionStatus.DISCONNECTED

    def is_connected(self) -> bool:
        if self._client and hasattr(self._client, "is_connected"):
            return bool(self._client.is_connected())
        return self._status == RobotConnectionStatus.CONNECTED

    def submit_task(self, intent: dict[str, Any]) -> RobotCommandResult:
        cmd_id = f"cmd_{uuid.uuid4().hex[:8]}"
        if not self.is_connected() or not self._client:
            return RobotCommandResult(
                command_id=cmd_id,
                status=CommandExecutionStatus.FAILED,
                error_message="ROS 2 client is not connected.",
            )

        try:
            task_id = self._client.publish_task_cmd(intent)
            if task_id:
                return RobotCommandResult(
                    command_id=cmd_id,
                    status=CommandExecutionStatus.SENT,
                    task_id=str(task_id),
                    task_id_hex=f"0x{task_id:X}" if isinstance(task_id, int) else str(task_id),
                    message="Task published to ROS 2 topic successfully.",
                )
            else:
                return RobotCommandResult(
                    command_id=cmd_id,
                    status=CommandExecutionStatus.FAILED,
                    error_message="publish_task_cmd returned empty task_id.",
                )
        except Exception as exc:
            return RobotCommandResult(
                command_id=cmd_id,
                status=CommandExecutionStatus.FAILED,
                error_message=str(exc),
            )

    def cancel_task(self, task_id: str) -> RobotCommandResult:
        cmd_id = f"cmd_{uuid.uuid4().hex[:8]}"
        if not self.is_connected() or not self._client:
            return RobotCommandResult(
                command_id=cmd_id,
                status=CommandExecutionStatus.FAILED,
                error_message="ROS 2 client is not connected.",
            )

        try:
            numeric_id = int(task_id, 16) if task_id.startswith("0x") else int(task_id)
            cancelled = self._client.cancel_task(numeric_id)
            return RobotCommandResult(
                command_id=cmd_id,
                status=CommandExecutionStatus.ACCEPTED if cancelled else CommandExecutionStatus.FAILED,
                task_id=task_id,
                message="Cancel task command sent to ROS 2." if cancelled else "Failed to send cancel.",
            )
        except Exception as exc:
            return RobotCommandResult(
                command_id=cmd_id,
                status=CommandExecutionStatus.FAILED,
                error_message=str(exc),
            )

    def emergency_stop(self) -> RobotCommandResult:
        cmd_id = f"cmd_{uuid.uuid4().hex[:8]}"
        if not self.is_connected() or not self._client:
            return RobotCommandResult(
                command_id=cmd_id,
                status=CommandExecutionStatus.FAILED,
                error_message="ROS 2 client is not connected.",
            )

        try:
            stopped = self._client.emergency_stop()
            return RobotCommandResult(
                command_id=cmd_id,
                status=CommandExecutionStatus.ACCEPTED if stopped else CommandExecutionStatus.FAILED,
                message="Emergency stop published to ROS 2." if stopped else "Emergency stop publish failed.",
            )
        except Exception as exc:
            return RobotCommandResult(
                command_id=cmd_id,
                status=CommandExecutionStatus.FAILED,
                error_message=str(exc),
            )

    def poll_telemetry(self) -> StandardTelemetrySnapshot:
        if self._tracker and hasattr(self._tracker, "latest_telemetry"):
            telemetry = self._tracker.latest_telemetry
            if telemetry:
                return StandardTelemetrySnapshot(
                    unit_id=self.unit_id,
                    timestamp_iso=getattr(telemetry, "received_at", "") or datetime.now(timezone.utc).isoformat(),
                    pose_x=getattr(telemetry, "pose_x", 0.0),
                    pose_y=getattr(telemetry, "pose_y", 0.0),
                    pose_z=getattr(telemetry, "pose_z", 0.0),
                    vel_x=getattr(telemetry, "vel_x", 0.0),
                    vel_y=getattr(telemetry, "vel_y", 0.0),
                    vel_z=getattr(telemetry, "vel_z", 0.0),
                    altitude_m=getattr(telemetry, "altitude", 0.0),
                    control_mode=getattr(telemetry, "ctr_mode", "AUTO"),
                    health_status=getattr(telemetry, "health", "HEALTHY"),
                    raw_data=getattr(telemetry, "raw_msg", {}),
                )

        return StandardTelemetrySnapshot(
            unit_id=self.unit_id,
            timestamp_iso=datetime.now(timezone.utc).isoformat(),
            health_status="DISCONNECTED" if not self.is_connected() else "STANDBY",
        )
