"""Data contracts and models for the Robot Abstraction Layer (RAL)."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any
from datetime import datetime, timezone


class RobotConnectionStatus(str, Enum):
    """Connection status of a robot driver."""
    CONNECTED = "CONNECTED"
    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    ERROR = "ERROR"


class CommandExecutionStatus(str, Enum):
    """Command outcome status returned by robot drivers."""
    ACCEPTED = "ACCEPTED"
    SENT = "SENT"
    REJECTED = "REJECTED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


@dataclass
class RobotCommandResult:
    """Result of dispatching a command or task intent to a robot."""
    command_id: str
    status: CommandExecutionStatus
    task_id: str | None = None
    task_id_hex: str | None = None
    message: str = ""
    error_message: str | None = None
    raw_response: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "status": self.status.value,
            "task_id": self.task_id,
            "task_id_hex": self.task_id_hex,
            "message": self.message,
            "error_message": self.error_message,
            "raw_response": self.raw_response,
            "timestamp": self.timestamp,
        }


@dataclass
class StandardTelemetrySnapshot:
    """Standardized subsea robot telemetry snapshot across heterogeneous models/vendors."""
    unit_id: str
    timestamp_iso: str
    pose_x: float = 0.0
    pose_y: float = 0.0
    pose_z: float = 0.0            # Negative meters under sea surface
    vel_x: float = 0.0
    vel_y: float = 0.0
    vel_z: float = 0.0
    heading_deg: float = 0.0
    altitude_m: float = 0.0
    battery_pct: float | None = None
    control_mode: str | int = "AUTO"
    health_status: str | int = "HEALTHY"
    active_task_id: str | None = None
    active_task_status: str | None = None
    raw_data: dict[str, Any] = field(default_factory=dict)

    @property
    def water_depth_m(self) -> float:
        """Absolute water depth in positive meters."""
        return abs(self.pose_z)

    def to_dict(self) -> dict[str, Any]:
        return {
            "unit_id": self.unit_id,
            "timestamp_iso": self.timestamp_iso,
            "water_depth_m": self.water_depth_m,
            "pose_x": self.pose_x,
            "pose_y": self.pose_y,
            "pose_z": self.pose_z,
            "vel_x": self.vel_x,
            "vel_y": self.vel_y,
            "vel_z": self.vel_z,
            "heading_deg": self.heading_deg,
            "altitude_m": self.altitude_m,
            "battery_pct": self.battery_pct,
            "control_mode": self.control_mode,
            "health_status": self.health_status,
            "active_task_id": self.active_task_id,
            "active_task_status": self.active_task_status,
            "raw_data": self.raw_data,
        }
