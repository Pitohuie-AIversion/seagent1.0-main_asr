"""Robot Gateway (Robot Abstraction Layer) managing heterogeneous robot drivers and fleet dispatch."""
from __future__ import annotations

import hashlib
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
        self._dispatch_records: dict[str, dict[str, Any]] = {}
        self._rw_lock = threading.RLock()

    @classmethod
    def get_instance(cls) -> RobotGateway:
        """Singleton accessor for the RobotGateway."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Reset the singleton instance for testing or clean restart."""
        with cls._lock:
            if cls._instance:
                try:
                    cls._instance.emergency_stop_all()
                except Exception:
                    pass
            cls._instance = None

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
        from src.observability import trace_span

        target_unit = unit_id or intent.get("robot_id") or intent.get("equipment_unit_id")
        with trace_span(
            "seagent.robot_gateway.dispatch",
            attributes={"target_unit": str(target_unit)},
        ) as span:
            driver = self.get_driver(target_unit)
            resolved_unit = driver.unit_id if driver else target_unit
            span.set_attribute("target_unit", str(resolved_unit))
            if not driver:
                span.set_attribute("status", "FAILED")
                return RobotCommandResult(
                    command_id="cmd_err",
                    status=CommandExecutionStatus.FAILED,
                    error_message=f"No suitable driver found for robot '{target_unit}'.",
                )

            if not driver.is_connected():
                driver.connect()

            result = driver.submit_task(intent)
            span.set_attribute("status", result.status.value)
            if result.task_id:
                span.set_attribute("task_id", str(result.task_id))
            return result

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

    def is_healthy(self) -> bool:
        """Check if gateway has at least one connected driver."""
        with self._rw_lock:
            if not self._drivers:
                return False
            return any(d.is_connected() for d in self._drivers.values())

    def get_dispatch_record(self, task_intent: dict[str, Any]) -> dict[str, Any] | None:
        """Read send evidence without publishing a command (Bridge Protocol compliance)."""
        identity = task_intent.get("intent_id") or task_intent.get("task_id")
        if not identity:
            import json
            identity = hashlib.sha256(json.dumps(task_intent, sort_keys=True, default=str).encode()).hexdigest()
        with self._rw_lock:
            return self._dispatch_records.get(str(identity))

    def dispatch_intent(
        self,
        task_intent: dict[str, Any],
        task_id: int | None = None,
        **kwargs: Any,
    ) -> int:
        """Send finalized task intent and return numeric task_id (Bridge Protocol compliance)."""
        if not self.is_healthy():
            raise RuntimeError("Robot gateway has no connected drivers available.")

        identity = str(
            task_intent.get("intent_id")
            or task_intent.get("task_id")
            or hashlib.sha256(str(task_intent).encode()).hexdigest()
        )

        with self._rw_lock:
            existing = self._dispatch_records.get(identity)
            if existing and existing.get("dispatch_state") == "SENT":
                return int(existing["task_id"])

        result = self.dispatch_task(task_intent)
        if result.status != CommandExecutionStatus.SENT:
            raise RuntimeError(result.error_message or f"Gateway driver execution status: {result.status.value}")

        if task_id is not None:
            numeric_task_id = task_id
        elif result.task_id is not None:
            raw_id = str(result.task_id)
            try:
                if raw_id.startswith("0x") or raw_id.startswith("0X"):
                    numeric_task_id = int(raw_id, 16)
                else:
                    numeric_task_id = int(raw_id)
            except ValueError:
                numeric_task_id = int(hashlib.md5(raw_id.encode()).hexdigest()[:6], 16)
        else:
            numeric_task_id = int(hashlib.md5(identity.encode()).hexdigest()[:6], 16)

        with self._rw_lock:
            self._dispatch_records[identity] = {
                "dispatch_state": "SENT",
                "task_id": numeric_task_id,
                "command_id": result.command_id,
                "unit_id": task_intent.get("equipment_unit_id") or task_intent.get("robot_id"),
            }

        return numeric_task_id
