"""Robot Gateway (Robot Abstraction Layer) managing heterogeneous robot drivers and fleet dispatch."""
from __future__ import annotations

import logging
import threading
from typing import Any

from .driver import BaseRobotDriver
from .models import (
    CommandExecutionStatus,
    RobotCommandResult,
    StandardTelemetrySnapshot,
)

logger = logging.getLogger(__name__)


class RobotGateway:
    """Central gateway routing commands and aggregating telemetry across heterogeneous robots."""

    _instance: RobotGateway | None = None
    _lock = threading.Lock()

    def __init__(self):
        self._drivers: dict[str, BaseRobotDriver] = {}
        self._default_driver_id: str | None = None
        self._rw_lock = threading.RLock()

    @classmethod
    def get_instance(cls) -> RobotGateway:
        """Singleton accessor for the RobotGateway."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def register_driver(self, driver: BaseRobotDriver, is_default: bool = False) -> None:
        """Register a robot driver instance."""
        with self._rw_lock:
            self._drivers[driver.unit_id] = driver
            if is_default or self._default_driver_id is None:
                self._default_driver_id = driver.unit_id
            logger.info("[RobotGateway] Registered driver '%s' (unit_id: %s, default=%s)",
                        driver.driver_name, driver.unit_id, is_default)

    def unregister_driver(self, unit_id: str) -> None:
        """Unregister a robot driver and disconnect it if connected."""
        with self._rw_lock:
            driver = self._drivers.pop(unit_id, None)
            if driver:
                try:
                    driver.disconnect()
                except Exception as exc:
                    logger.warning("[RobotGateway] Error disconnecting driver %s: %s", unit_id, exc)
            if self._default_driver_id == unit_id:
                self._default_driver_id = next(iter(self._drivers.keys()), None)

    def get_driver(self, unit_id: str | None = None) -> BaseRobotDriver | None:
        """Get driver for a specific unit_id, or the default driver if unit_id is None."""
        with self._rw_lock:
            if unit_id is not None:
                return self._drivers.get(unit_id)
            if self._default_driver_id and self._default_driver_id in self._drivers:
                return self._drivers[self._default_driver_id]
            return None

    def list_drivers(self) -> list[dict[str, Any]]:
        """List all registered drivers, status, and driver metadata."""
        with self._rw_lock:
            result = []
            for uid, driver in self._drivers.items():
                result.append({
                    "unit_id": uid,
                    "driver_name": driver.driver_name,
                    "is_default": (uid == self._default_driver_id),
                    "status": driver.status.value,
                    "is_connected": driver.is_connected(),
                })
            return result

    def dispatch_task(self, intent: dict[str, Any], unit_id: str | None = None) -> RobotCommandResult:
        """Route and dispatch task intent to target robot driver."""
        target_unit = unit_id or intent.get("robot_id") or intent.get("equipment_unit_id")
        driver = self.get_driver(target_unit)
        if not driver:
            return RobotCommandResult(
                command_id="cmd_err",
                status=CommandExecutionStatus.FAILED,
                error_message=f"No suitable driver found for robot '{target_unit}'.",
            )

        if not driver.is_connected():
            driver.connect()

        return driver.submit_task(intent)

    def cancel_task(self, task_id: str, unit_id: str | None = None) -> RobotCommandResult:
        """Cancel a running task on target robot or default robot."""
        driver = self.get_driver(unit_id)
        if not driver:
            return RobotCommandResult(
                command_id="cmd_err",
                status=CommandExecutionStatus.FAILED,
                error_message=f"No driver found to cancel task '{task_id}'.",
            )
        return driver.cancel_task(task_id)

    def emergency_stop_all(self) -> dict[str, RobotCommandResult]:
        """Broadcast emergency stop across all registered robots."""
        results = {}
        with self._rw_lock:
            drivers = list(self._drivers.values())
        for d in drivers:
            results[d.unit_id] = d.emergency_stop()
        return results

    def poll_fleet_telemetry(self) -> dict[str, StandardTelemetrySnapshot]:
        """Aggregate telemetry from all registered robot drivers."""
        snapshots = {}
        with self._rw_lock:
            drivers = list(self._drivers.values())
        for d in drivers:
            try:
                snapshots[d.unit_id] = d.poll_telemetry()
            except Exception as exc:
                logger.error("[RobotGateway] Error polling telemetry for %s: %s", d.unit_id, exc)
        return snapshots
