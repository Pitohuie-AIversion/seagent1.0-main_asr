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
            from mcp.core.rosbridge_client import RosbridgeClient
            from mcp.core.task_status_tracker import TaskStatusTracker

            client = RosbridgeClient(host=self.host, port=self.port)
            self._client = client
            connect_result = client.connect()
            if connect_result is False:
                connected = False
            elif connect_result is True:
                connected = True
            elif connect_result is None:
                connected = bool(client.is_connected()) if hasattr(client, "is_connected") else True
            else:
                connected = bool(connect_result)

            if connected:
                self._tracker = TaskStatusTracker(client)
                self._tracker.start()
                self._status = RobotConnectionStatus.CONNECTED
                return True

            self._cleanup_failed_connect()
            self._status = RobotConnectionStatus.ERROR
            return False
        except Exception as exc:
            logger.warning("[ROS2BridgeDriver] Connect failed: %s", exc)
            self._cleanup_failed_connect()
            self._status = RobotConnectionStatus.ERROR
            return False

    def _cleanup_failed_connect(self) -> None:
        if self._tracker is not None:
            try:
                self._tracker.stop()
            except Exception as exc:
                logger.debug("[ROS2BridgeDriver] Tracker stop during cleanup failed: %s", exc)
            self._tracker = None

        if self._client is not None:
            try:
                if hasattr(self._client, "disconnect"):
                    self._client.disconnect()
                elif hasattr(self._client, "close"):
                    self._client.close()
            except Exception as exc:
                logger.debug("[ROS2BridgeDriver] Client disconnect during cleanup failed: %s", exc)
            self._client = None

    def disconnect(self) -> None:
        if self._tracker:
            try:
                self._tracker.stop()
            except Exception as exc:
                logger.debug("[ROS2BridgeDriver] Tracker stop failed during disconnect: %s", exc)
            self._tracker = None

        if self._client:
            try:
                if hasattr(self._client, "disconnect"):
                    self._client.disconnect()
                elif hasattr(self._client, "close"):
                    self._client.close()
            except Exception as exc:
                logger.debug("[ROS2BridgeDriver] Client disconnect failed during disconnect: %s", exc)
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
            raw_id = str(task_id).strip()
            if not raw_id:
                raise ValueError("task_id must not be empty.")
            numeric_id = int(raw_id, 16) if raw_id.lower().startswith("0x") else int(raw_id)
            if numeric_id < 0:
                raise ValueError("task_id must be non-negative.")

            if hasattr(self._client, "delete_task"):
                cancelled = self._client.delete_task(numeric_id)
            elif hasattr(self._client, "cancel_task"):
                cancelled = self._client.cancel_task(numeric_id)
            else:
                raise AttributeError("ROS 2 client supports neither delete_task nor cancel_task.")

            ok = bool(cancelled)
            return RobotCommandResult(
                command_id=cmd_id,
                status=CommandExecutionStatus.ACCEPTED if ok else CommandExecutionStatus.FAILED,
                task_id=raw_id,
                task_id_hex=(
                    f"0x{cancelled:X}"
                    if isinstance(cancelled, int) and not isinstance(cancelled, bool) and ok
                    else None
                ),
                message="Cancel/delete task command sent to ROS 2." if ok else "Failed to send cancel.",
                error_message=None if ok else "Cancel/delete task command returned empty/false result.",
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
            if hasattr(self._client, "suspend_all"):
                stopped = self._client.suspend_all()
            elif hasattr(self._client, "emergency_stop"):
                stopped = self._client.emergency_stop()
            else:
                raise AttributeError("ROS 2 client supports neither suspend_all nor emergency_stop.")

            ok = bool(stopped)
            return RobotCommandResult(
                command_id=cmd_id,
                status=CommandExecutionStatus.ACCEPTED if ok else CommandExecutionStatus.FAILED,
                task_id_hex=(
                    f"0x{stopped:X}"
                    if isinstance(stopped, int) and not isinstance(stopped, bool) and ok
                    else None
                ),
                message=(
                    "Suspend-all (emergency stop) command published to ROS 2."
                    if ok
                    else "Suspend-all (emergency stop) publish failed."
                ),
                error_message=None if ok else "Suspend-all command returned empty/false result.",
            )
        except Exception as exc:
            return RobotCommandResult(
                command_id=cmd_id,
                status=CommandExecutionStatus.FAILED,
                error_message=str(exc),
            )

    def poll_telemetry(self) -> StandardTelemetrySnapshot:
        if self._tracker and hasattr(self._tracker, "latest_telemetry"):
            latest_attr = self._tracker.latest_telemetry
            telemetry = latest_attr() if callable(latest_attr) else latest_attr
            if telemetry is not None:
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
                    raw_data=getattr(telemetry, "raw_msg", {}) or {},
                )

        return StandardTelemetrySnapshot(
            unit_id=self.unit_id,
            timestamp_iso=datetime.now(timezone.utc).isoformat(),
            health_status="DISCONNECTED" if not self.is_connected() else "STANDBY",
        )
