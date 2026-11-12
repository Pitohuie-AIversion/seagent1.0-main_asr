"""Abstract base class for all robot drivers in the Robot Abstraction Layer (RAL)."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from .models import (
    CommandExecutionStatus,
    RobotCommandResult,
    RobotConnectionStatus,
    StandardTelemetrySnapshot,
)


class BaseRobotDriver(ABC):
    """Abstract base class that all Southbound robot hardware/simulation drivers must implement."""

    def __init__(self, unit_id: str, driver_name: str = "generic_driver"):
        self.unit_id = unit_id
        self.driver_name = driver_name
        self._status = RobotConnectionStatus.DISCONNECTED

    @property
    def status(self) -> RobotConnectionStatus:
        return self._status

    @abstractmethod
    def connect(self) -> bool:
        """Establish connection with the robot hardware, bridge, or simulation."""
        pass

    @abstractmethod
    def disconnect(self) -> None:
        """Close connection and clean up network or socket handles."""
        pass

    @abstractmethod
    def is_connected(self) -> bool:
        """Check if connection is currently active and healthy."""
        pass

    @abstractmethod
    def submit_task(self, intent: dict[str, Any]) -> RobotCommandResult:
        """Submit a structured TaskIntent to the robot controller."""
        pass

    @abstractmethod
    def cancel_task(self, task_id: str) -> RobotCommandResult:
        """Request immediate cancellation of a running task on the robot."""
        pass

    @abstractmethod
    def emergency_stop(self) -> RobotCommandResult:
        """Issue an immediate all-stop command to all thrusters and actuators."""
        pass

    @abstractmethod
    def poll_telemetry(self) -> StandardTelemetrySnapshot:
        """Fetch the most recent standardized telemetry snapshot."""
        pass
