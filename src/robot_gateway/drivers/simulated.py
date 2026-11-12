"""In-memory simulated robot driver for testing, CI, and virtual marine operations."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from ..driver import BaseRobotDriver
from ..models import (
    CommandExecutionStatus,
    RobotCommandResult,
    RobotConnectionStatus,
    StandardTelemetrySnapshot,
)


class SimulatedRobotDriver(BaseRobotDriver):
    """High-fidelity simulated robot driver tracking navigation state, descent, and task lifecycle."""

    def __init__(
        self,
        unit_id: str = "SIM-ROV-001",
        initial_depth_m: float = 120.0,
        max_depth_m: float = 3000.0,
    ):
        super().__init__(unit_id=unit_id, driver_name="simulated_driver")
        self.initial_depth_m = initial_depth_m
        self.max_depth_m = max_depth_m

        # Internal kinematic state
        self.x = 0.0
        self.y = 0.0
        self.z = -abs(initial_depth_m)
        self.heading = 90.0
        self.battery = 98.5
        self.control_mode = "AUTO"
        self.health = "HEALTHY"

        # Task state
        self.active_task_id: str | None = None
        self.active_task_status: str | None = None
        self.active_task_intent: dict[str, Any] | None = None

        # Fault injection flags
        self._fault_injected: str | None = None

    def connect(self) -> bool:
        if self._fault_injected == "connection_error":
            self._status = RobotConnectionStatus.ERROR
            return False
        self._status = RobotConnectionStatus.CONNECTED
        return True

    def disconnect(self) -> None:
        self._status = RobotConnectionStatus.DISCONNECTED

    def is_connected(self) -> bool:
        return self._status == RobotConnectionStatus.CONNECTED

    def inject_fault(self, fault_type: str | None) -> None:
        """Inject simulation fault: 'connection_error', 'hardware_fault', 'overdepth_fault', None."""
        self._fault_injected = fault_type
        if fault_type == "hardware_fault":
            self.health = "CRITICAL_FAULT"
        elif fault_type is None:
            self.health = "HEALTHY"

    def submit_task(self, intent: dict[str, Any]) -> RobotCommandResult:
        cmd_id = f"cmd_{uuid.uuid4().hex[:8]}"
        if not self.is_connected():
            return RobotCommandResult(
                command_id=cmd_id,
                status=CommandExecutionStatus.FAILED,
                error_message="Robot driver is not connected.",
            )

        if self._fault_injected == "hardware_fault":
            return RobotCommandResult(
                command_id=cmd_id,
                status=CommandExecutionStatus.REJECTED,
                error_message="Robot hardware fault detected, task rejected.",
            )

        task_id = intent.get("task_id") or intent.get("intent_id") or f"0x{uuid.uuid4().hex[:6]}"
        self.active_task_id = str(task_id)
        self.active_task_status = "ONGOING"
        self.active_task_intent = intent

        # Adjust target depth if provided in task parameters
        target_depth = intent.get("water_depth") or intent.get("parameters", {}).get("water_depth")
        if target_depth is not None:
            try:
                depth_val = float(target_depth)
                self.z = -min(depth_val, self.max_depth_m)
            except (ValueError, TypeError):
                pass

        return RobotCommandResult(
            command_id=cmd_id,
            status=CommandExecutionStatus.SENT,
            task_id=self.active_task_id,
            message="Task successfully written to simulated telemetry.",
        )

    def cancel_task(self, task_id: str) -> RobotCommandResult:
        cmd_id = f"cmd_{uuid.uuid4().hex[:8]}"
        if not self.is_connected():
            return RobotCommandResult(
                command_id=cmd_id,
                status=CommandExecutionStatus.FAILED,
                error_message="Robot driver is not connected.",
            )

        if self.active_task_id == task_id or not task_id:
            self.active_task_status = "CANCELLED"
            cancelled_id = self.active_task_id
            self.active_task_id = None
            return RobotCommandResult(
                command_id=cmd_id,
                status=CommandExecutionStatus.ACCEPTED,
                task_id=cancelled_id,
                message="Task successfully cancelled.",
            )

        return RobotCommandResult(
            command_id=cmd_id,
            status=CommandExecutionStatus.REJECTED,
            error_message=f"No matching active task '{task_id}' found to cancel.",
        )

    def emergency_stop(self) -> RobotCommandResult:
        cmd_id = f"cmd_{uuid.uuid4().hex[:8]}"
        self.control_mode = "EMERGENCY_STOP"
        if self.active_task_id:
            self.active_task_status = "ABORTED"
        return RobotCommandResult(
            command_id=cmd_id,
            status=CommandExecutionStatus.ACCEPTED,
            message="Emergency all-stop executed immediately.",
        )

    def complete_active_task(self) -> None:
        """Helper to mark current active task as completed."""
        if self.active_task_id:
            self.active_task_status = "FINISH"

    def fail_active_task(self, reason: str = "execution_error") -> None:
        """Helper to mark current active task as failed."""
        if self.active_task_id:
            self.active_task_status = "FAIL"

    def poll_telemetry(self) -> StandardTelemetrySnapshot:
        return StandardTelemetrySnapshot(
            unit_id=self.unit_id,
            timestamp_iso=datetime.now(timezone.utc).isoformat(),
            pose_x=self.x,
            pose_y=self.y,
            pose_z=self.z,
            vel_x=0.2,
            vel_y=0.0,
            vel_z=0.0,
            heading_deg=self.heading,
            altitude_m=max(0.0, 10.0 + self.z / 50.0),
            battery_pct=self.battery,
            control_mode=self.control_mode,
            health_status=self.health,
            active_task_id=self.active_task_id,
            active_task_status=self.active_task_status,
            raw_data={"simulated": True, "fault": self._fault_injected},
        )
