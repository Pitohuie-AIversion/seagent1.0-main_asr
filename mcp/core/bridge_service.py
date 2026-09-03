"""Runtime bridge between finalized SEAgent tasks and a rosbridge gateway.

本服务的任务闭环严格使用 `sealien_ctrlpilot_llmbridge` 的主协议：
`/task_cmd`（SysTaskCmd）/`/task/sys_config`（SysConfig）/`/task/system_status`（SysStatus）。
`msgmanagement` 消息（如 Keypoints、Heartbeat、ThrusterStatus、机械臂等）属于可选辅助通道，不参与主流程判定。
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from .rosbridge_client import (
    PilotMode,
    RosbridgeClient,
    SEAGENT_TO_ROS2_TASK_TYPE,
    generate_task_id,
)
from .task_status_tracker import ROVTelemetry, TaskStatusItem, TaskStatusTracker


logger = logging.getLogger(__name__)


class SEAgentMCPBridgeService:
    """Owns the live rosbridge connection, dispatch idempotency and telemetry."""

    TELEMETRY_MAX_AGE_SECONDS = 5.0

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 9090,
        state_info: Optional[Any] = None,
        connect_timeout: float = 5.0,
    ):
        self.host = host
        self.port = port
        self.state_info = state_info
        self.connect_timeout = connect_timeout
        self.client = RosbridgeClient(host=host, port=port, connect_timeout=connect_timeout)
        self.tracker = TaskStatusTracker(self.client)
        self._running = False
        self._lock = threading.RLock()
        self._dispatch_lock = threading.Lock()
        self._dispatch_records: Dict[str, Dict[str, Any]] = {}
        self._last_error: Optional[str] = None

    def start(self) -> None:
        """Connect and subscribe to robot telemetry."""
        with self._lock:
            if self._running:
                return
            self.client.connect()
            try:
                # Register all enabled auxiliary channels from the shared YAML
                # catalog before attaching the task-status callback.
                self.client.subscribe_from_config()
                self.tracker.start()
            except Exception:
                self.client.disconnect()
                raise
            self._running = True
            self._last_error = None
        logger.info("[MCPBridgeService] 服务已启动 (ws://%s:%s)", self.host, self.port)

    def stop(self) -> None:
        """Stop telemetry tracking and disconnect."""
        with self._lock:
            if not self._running:
                return
            self._running = False
            self.tracker.stop()
            self.client.disconnect()
        logger.info("[MCPBridgeService] 服务已停止")

    def reconnect(self, host: str, port: int) -> None:
        """Switch gateways only after the replacement connection is ready."""
        replacement_client = RosbridgeClient(
            host=host, port=port, connect_timeout=self.connect_timeout
        )
        replacement_tracker = TaskStatusTracker(replacement_client)
        try:
            replacement_client.connect()
            replacement_client.subscribe_from_config()
            replacement_tracker.start()
        except Exception:
            replacement_client.disconnect()
            raise

        with self._lock:
            old_client = self.client
            old_tracker = self.tracker
            self.client = replacement_client
            self.tracker = replacement_tracker
            self.host = host
            self.port = port
            self._running = True
            self._last_error = None
        old_tracker.stop()
        old_client.disconnect()
        logger.info("[MCPBridgeService] 已切换网关至 ws://%s:%s", host, port)

    def is_healthy(self) -> bool:
        """Return transport health; telemetry freshness is reported separately."""
        with self._lock:
            return self._running and self.client.is_connected()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *_):
        self.stop()

    @staticmethod
    def _intent_identity(task_intent: Dict[str, Any]) -> str:
        identity = task_intent.get("intent_id") or task_intent.get("task_id")
        if isinstance(identity, str) and identity.strip():
            return identity.strip()
        try:
            canonical = json.dumps(
                task_intent,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("TaskIntent 缺少稳定 ID 且无法生成内容指纹") from exc
        return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"

    @staticmethod
    def _task_type_code(task_intent: Dict[str, Any]) -> int:
        key = task_intent.get("task_type_key") or task_intent.get("task_type")
        mapped = SEAGENT_TO_ROS2_TASK_TYPE.get(key)
        if mapped is None:
            raise ValueError(f"不支持的 SEAgent task_type: {key}")
        return int(mapped)

    def dispatch_intent(
        self,
        task_intent: Dict[str, Any],
        task_id: Optional[int] = None,
        use_geodetic: bool = False,
        origin: Optional[Any] = None,
    ) -> int:
        """Send one finalized intent at most once per bridge process.

        A failed transport attempt keeps the same ROS task ID for an explicit retry.
        """
        if not self.is_healthy():
            raise RuntimeError(
                f"MCPBridgeService 未连接到支持船网关 (ws://{self.host}:{self.port})"
            )
        identity = self._intent_identity(task_intent)

        with self._dispatch_lock:
            existing = self._dispatch_records.get(identity)
            if existing and existing["dispatch_state"] != "FAILED":
                return int(existing["task_id"])

            assigned_task_id = (
                int(existing["task_id"])
                if existing is not None
                else (int(task_id) if task_id is not None else generate_task_id())
            )
            record = {
                "task_id": assigned_task_id,
                "intent_id": identity,
                "task_type": self._task_type_code(task_intent),
                "dispatch_state": "SENDING",
                "dispatched_at": None,
                "error": None,
            }
            self._dispatch_records[identity] = record
            try:
                self.client.publish_task_cmd(
                    task_intent,
                    task_id=assigned_task_id,
                    use_geodetic=use_geodetic,
                    origin=origin,
                )
            except Exception as exc:
                record["dispatch_state"] = "FAILED"
                record["error"] = str(exc)
                self._last_error = str(exc)
                logger.error(
                    "[MCPBridgeService] TaskIntent 下发失败 intent_id=%s: %s",
                    identity,
                    exc,
                )
                raise

            record["dispatch_state"] = "SENT"
            record["dispatched_at"] = datetime.now(timezone.utc).isoformat(
                timespec="milliseconds"
            )
            self._last_error = None
            logger.info(
                "[MCPBridgeService] TaskIntent 已写入 ROS 2 传输: intent_id=%s task_id=0x%X",
                identity,
                assigned_task_id,
            )
            return assigned_task_id

    def suspend_task(self, task_id: int) -> int:
        return self.client.suspend_task(task_id)

    def resume_task(self, task_id: int) -> int:
        return self.client.resume_task(task_id)

    def delete_task(self, task_id: int) -> int:
        return self.client.delete_task(task_id)

    def emergency_clear_block(self) -> int:
        return self.client.clear_block()

    def control_device(self, device_id: int, value: float) -> int:
        return self.client.ctrl_task(device_id=device_id, value=value)

    def wait_for_task_finish(
        self, task_id: int, timeout: float = 120.0
    ) -> Optional[TaskStatusItem]:
        return self.tracker.wait_for_finish(task_id=task_id, timeout=timeout)

    def get_task_status(self, task_id: int) -> Optional[TaskStatusItem]:
        return self.tracker.get_task_status(task_id)

    @staticmethod
    def _status_progress(status_name: str) -> float:
        return {
            "SENT": 0.0,
            "READY": 5.0,
            "PLAN": 15.0,
            "ENTER": 30.0,
            "ONGOING": 60.0,
            "EXIT": 90.0,
            "FINISH": 100.0,
            "PAUSE": 60.0,
            "FAIL": 100.0,
        }.get(status_name, 0.0)

    @staticmethod
    def _format_pilot_mode(ctr_mode: int) -> str:
        try:
            return f"{PilotMode(ctr_mode).name} ({int(ctr_mode)})"
        except Exception:
            return str(ctr_mode)

    @classmethod
    def _telemetry_is_fresh(cls, telemetry: Optional[ROVTelemetry]) -> bool:
        if telemetry is None or not telemetry.received_at:
            return False
        try:
            received = datetime.fromisoformat(telemetry.received_at)
            age = (datetime.now(timezone.utc) - received).total_seconds()
            return 0.0 <= age <= cls.TELEMETRY_MAX_AGE_SECONDS
        except (TypeError, ValueError):
            return False

    def runtime_snapshot(self) -> Dict[str, Any]:
        """Build the dashboard view solely from dispatch memory and ROS telemetry."""
        telemetry = self.tracker.latest_telemetry()
        with self._dispatch_lock:
            records = [dict(record) for record in self._dispatch_records.values()]

        by_task_id = {int(record["task_id"]): record for record in records}
        tasks = []
        if telemetry is not None:
            for item in telemetry.task_list:
                record = by_task_id.pop(item.task_id, {})
                status_name = item.status_name or f"UNKNOWN({item.status})"
                tasks.append({
                    "task_id": f"0x{item.task_id:X}",
                    "intent_id": record.get("intent_id", ""),
                    "task_type": item.task_type,
                    "status": status_name,
                    "status_code": item.status,
                    "progress": self._status_progress(status_name),
                    "error": record.get("error"),
                })
        for record in by_task_id.values():
            state = record["dispatch_state"]
            tasks.append({
                "task_id": f"0x{int(record['task_id']):X}",
                "intent_id": record["intent_id"],
                "task_type": record["task_type"],
                "status": state,
                "status_code": None,
                "progress": self._status_progress(state),
                "error": record.get("error"),
            })

        return {
            "last_update": telemetry.received_at if telemetry else None,
            "telemetry_fresh": self._telemetry_is_fresh(telemetry),
            "water_depth_m": telemetry.water_depth if telemetry else None,
            "altitude_m": telemetry.altitude if telemetry else None,
            "ctr_mode": self._format_pilot_mode(telemetry.ctr_mode) if telemetry else None,
            "health": telemetry.health if telemetry else None,
            "current_pose": {
                "x_m": telemetry.pose_x,
                "y_m": telemetry.pose_y,
                "z_m": telemetry.pose_z,
            } if telemetry else None,
            "active_tasks_count": len(tasks),
            "active_tasks": tasks,
        }

    def status_payload(self) -> Dict[str, Any]:
        snapshot = self.runtime_snapshot()
        return {
            "mcp_connected": self.is_healthy(),
            "host": self.host,
            "port": self.port,
            "ws_url": f"ws://{self.host}:{self.port}",
            "telemetry_fresh": snapshot["telemetry_fresh"],
            "last_error": self._last_error or self.client.last_transport_error,
            "snapshot": snapshot,
        }
