"""Runtime bridge between finalized SEAgent tasks and a rosbridge gateway.

本服务的任务闭环严格使用 `sealien_ctrlpilot_llmbridge` 的主协议：
`/task_cmd`（SysTaskCmd）/`/task/sys_config`（SysConfig）/`/task/system_status`（SysStatus）。
`msgmanagement` 消息（如 Keypoints、Heartbeat、ThrusterStatus、机械臂等）属于可选辅助通道，不参与主流程判定。
"""

from __future__ import annotations

import hashlib
import json
import logging
from contextlib import contextmanager
import threading
import os
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from .rosbridge_client import (
    PilotMode,
    RosbridgeClient,
    SEAGENT_TO_ROS2_TASK_TYPE,
    generate_task_id,
)
from .task_status_tracker import ROVTelemetry, TaskStatusItem, TaskStatusTracker
from .runtime_config import (
    GatewayConfig,
    Ros2RuntimeConfig,
    Ros2RuntimeConfigError,
    SubscriptionConfig,
    load_ros2_runtime_config,
)


logger = logging.getLogger(__name__)


class SEAgentMCPBridgeService:
    """Owns the live rosbridge connection, dispatch idempotency and telemetry."""

    TELEMETRY_MAX_AGE_SECONDS = 5.0
    DISPATCH_RECORD_FILE = ".mcp_dispatch_records.json"
    DISPATCH_RECORD_LOCK_FILE = ".mcp_dispatch_records.lock"

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 9090,
        state_info: Optional[Any] = None,
        connect_timeout: float = 5.0,
        runtime_config_path: Optional[str | Path] = None,
        protocol_config_path: Optional[str | Path] = None,
        dispatch_records_dir: Optional[str | Path] = None,
        clear_records: bool = False,
    ):
        self._runtime_config_path = (
            Path(runtime_config_path) if runtime_config_path is not None else None
        )
        self._protocol_config_path = (
            Path(protocol_config_path)
            if protocol_config_path is not None
            else Path(__file__).resolve().parents[2] / "config" / "ros2_protocol_spec.yaml"
        )
        self._runtime_config: Optional[Ros2RuntimeConfig] = None
        if self._runtime_config_path is not None:
            self._runtime_config = load_ros2_runtime_config(
                self._runtime_config_path, self._protocol_config_path
            )
            if host == "127.0.0.1" and port == 9090:
                host = self._runtime_config.gateway.host
                port = self._runtime_config.gateway.port
        self.host = host
        self.port = port
        self.state_info = state_info
        self.connect_timeout = connect_timeout
        self.client = RosbridgeClient(host=host, port=port, connect_timeout=connect_timeout)
        self.tracker = self._new_tracker(self.client, self._runtime_config)
        self._running = False
        self._lock = threading.RLock()
        self._dispatch_lock = threading.Lock()
        self._dynamic_lock = threading.Lock()
        self._dispatch_records: Dict[str, Dict[str, Any]] = {}
        self._dynamic_callbacks: Dict[str, Any] = {}
        self._dynamic_messages: Dict[str, Dict[str, Any]] = {}
        self._last_error: Optional[str] = None
        self._runtime_config_error: Optional[str] = None
        self._runtime_generation = 1 if self._runtime_config is not None else 0
        self._runtime_loaded_at = self._now()
        self._runtime_config_mtime_ns = self._runtime_mtime()
        self._watcher_stop = threading.Event()
        self._watcher_thread: Optional[threading.Thread] = None
        self._dispatch_records_path: Optional[Path] = None
        self._dispatch_lock_path: Optional[Path] = None
        self._dispatch_records_dir = (
            Path(dispatch_records_dir) if dispatch_records_dir is not None else None
        )
        self._init_dispatch_record_paths()
        if clear_records:
            self.clear_dispatch_records()
        else:
            self._load_dispatch_records()

    def clear_dispatch_records(self) -> None:
        """Clear in-memory and persistent dispatch records."""
        with self._dispatch_lock:
            self._dispatch_records.clear()
            if self._dispatch_records_path is not None and self._dispatch_records_path.exists():
                try:
                    self._dispatch_records_path.unlink()
                except OSError:
                    pass

    def _init_dispatch_record_paths(self) -> None:
        if self._dispatch_records_dir is not None:
            base = self._dispatch_records_dir
        else:
            dispatch_dir = os.environ.get("SEAGENT_MCP_DISPATCH_DIR") or os.environ.get(
                "SEAGENT_ROS2_ID_DIR"
            )
            if not dispatch_dir:
                return
            base = Path(dispatch_dir)
        self._dispatch_records_path = base / self.DISPATCH_RECORD_FILE
        self._dispatch_lock_path = base / self.DISPATCH_RECORD_LOCK_FILE

    @staticmethod
    def _is_safe_json_record(entry: dict) -> bool:
        return (
            isinstance(entry.get("task_id"), int)
            and isinstance(entry.get("intent_id"), str)
            and isinstance(entry.get("dispatch_state"), str)
        )

    def _load_dispatch_records(self) -> None:
        if self._dispatch_records_path is None:
            return
        if self._dispatch_records_path.exists():
            try:
                raw_data = self._dispatch_records_path.read_text(encoding="utf-8")
                loaded = json.loads(raw_data)
            except (OSError, ValueError) as exc:
                logger.error(
                    "[MCPBridgeService] 读取任务下发记录文件失败: %s", exc
                )
                self._dispatch_records = {}
                return
        else:
            loaded = {}

        if not isinstance(loaded, dict):
            logger.error(
                "[MCPBridgeService] 下发记录文件格式错误：%s",
                self._dispatch_records_path,
            )
            self._dispatch_records = {}
            return

        sanitized: Dict[str, Dict[str, Any]] = {}
        for identity, entry in loaded.items():
            if not isinstance(identity, str) or not isinstance(entry, dict):
                continue
            if self._is_safe_json_record(entry):
                sanitized[identity] = dict(entry)
        self._dispatch_records = sanitized

    @contextmanager
    def _with_dispatch_file_lock(self):
        if self._dispatch_lock_path is None:
            yield None
            return

        lock_path = self._dispatch_lock_path
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        import fcntl

        with open(lock_path, "a+", encoding="utf-8") as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            try:
                yield lock_handle
            finally:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)

    def _persist_dispatch_records(self) -> None:
        if self._dispatch_records_path is None:
            return

        self._dispatch_records_path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self._dispatch_records, ensure_ascii=False, sort_keys=True)
        temporary_path = self._dispatch_records_path.with_name(
            f"{self._dispatch_records_path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        with open(temporary_path, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary_path, self._dispatch_records_path)
        directory_fd = os.open(
            self._dispatch_records_path.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def _record_dispatch(self, identity: str, record: Dict[str, Any]) -> None:
        self._dispatch_records[identity] = dict(record)
        self._persist_dispatch_records()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="milliseconds")

    def _runtime_mtime(self) -> Optional[int]:
        if self._runtime_config_path is None:
            return None
        try:
            return self._runtime_config_path.stat().st_mtime_ns
        except OSError:
            return None

    @staticmethod
    def _new_tracker(
        client: RosbridgeClient, config: Optional[Ros2RuntimeConfig]
    ) -> TaskStatusTracker:
        if config is None:
            return TaskStatusTracker(client)
        status = config.system_status
        return TaskStatusTracker(
            client,
            status_topic=status.topic,
            status_message_type=status.message_type,
        )

    def _record_dynamic_message(
        self, subscription: SubscriptionConfig, message: dict
    ) -> None:
        with self._dynamic_lock:
            previous = self._dynamic_messages.get(subscription.id, {})
            self._dynamic_messages[subscription.id] = {
                "message": dict(message),
                "received_at": self._now(),
                "message_count": int(previous.get("message_count", 0)) + 1,
            }

    def _register_subscriptions(
        self,
        client: RosbridgeClient,
        tracker: TaskStatusTracker,
        config: Optional[Ros2RuntimeConfig],
    ) -> Dict[str, Any]:
        tracker.start()
        callbacks: Dict[str, Any] = {}
        if config is None:
            return callbacks
        for subscription in config.enabled_subscriptions:
            if subscription.parser == "system_status":
                continue

            def callback(message, spec=subscription):
                self._record_dynamic_message(spec, message)

            client.subscribe(subscription.topic, subscription.message_type, callback)
            callbacks[subscription.id] = callback
        return callbacks

    def _start_watcher(self) -> None:
        config = self._runtime_config
        if (
            self._runtime_config_path is None
            or config is None
            or config.reload.mode != "automatic"
            or self._watcher_thread is not None
        ):
            return
        self._watcher_stop.clear()
        self._watcher_thread = threading.Thread(
            target=self._watch_runtime_config,
            daemon=True,
            name="ros2-runtime-config-watcher",
        )
        self._watcher_thread.start()

    def _stop_watcher(self) -> None:
        self._watcher_stop.set()
        thread = self._watcher_thread
        self._watcher_thread = None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)

    def _watch_runtime_config(self) -> None:
        while not self._watcher_stop.is_set():
            with self._lock:
                config = self._runtime_config
            interval = config.reload.check_interval_seconds if config else 1.0
            if self._watcher_stop.wait(interval):
                return
            current_mtime = self._runtime_mtime()
            if current_mtime == self._runtime_config_mtime_ns:
                continue
            self._runtime_config_mtime_ns = current_mtime
            try:
                self.reload_runtime_config()
            except Exception as exc:
                logger.error("[MCPBridgeService] ROS2 运行配置热加载失败: %s", exc)

    def start(self) -> None:
        """Connect and subscribe to robot telemetry."""
        with self._lock:
            if self._running:
                return
            self.client.connect()
            try:
                self._dynamic_callbacks = self._register_subscriptions(
                    self.client, self.tracker, self._runtime_config
                )
            except Exception:
                self.client.disconnect()
                raise
            self._running = True
            self._last_error = None
        self._start_watcher()
        logger.info("[MCPBridgeService] 服务已启动 (ws://%s:%s)", self.host, self.port)

    def stop(self) -> None:
        """Stop telemetry tracking and disconnect."""
        self._stop_watcher()
        with self._lock:
            if not self._running:
                return
            self._running = False
            self.tracker.stop()
            self.client.disconnect()
            self._dynamic_callbacks = {}
        logger.info("[MCPBridgeService] 服务已停止")

    def _replace_connection(
        self,
        host: str,
        port: int,
        config: Optional[Ros2RuntimeConfig],
    ) -> None:
        """Prepare a fully subscribed replacement before dropping the old link."""
        replacement_client = RosbridgeClient(
            host=host, port=port, connect_timeout=self.connect_timeout
        )
        replacement_tracker = self._new_tracker(replacement_client, config)
        try:
            replacement_client.connect()
            replacement_callbacks = self._register_subscriptions(
                replacement_client, replacement_tracker, config
            )
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
            self._runtime_config = config
            self._dynamic_callbacks = replacement_callbacks
            self._running = True
            self._last_error = None
        with self._dynamic_lock:
            self._dynamic_messages = {}
        try:
            old_tracker.stop()
        finally:
            old_client.disconnect()

    def reconnect(self, host: str, port: int, mode: Optional[str] = None) -> None:
        """Switch gateways only after the replacement connection is ready."""
        config = self._runtime_config
        if config is not None:
            gateway = GatewayConfig(
                host=host,
                port=port,
                mode=mode or config.gateway.mode,
            )
            config = replace(config, gateway=gateway)
        self._replace_connection(host, port, config)
        logger.info("[MCPBridgeService] 已切换网关至 ws://%s:%s", host, port)

    @property
    def gateway_mode(self) -> str:
        config = self._runtime_config
        if config is not None:
            return config.gateway.mode
        return "real" if self.port == 9090 else "mock"

    def reload_runtime_config(self) -> bool:
        """Apply a new valid runtime config while preserving the last good link."""
        if self._runtime_config_path is None:
            raise Ros2RuntimeConfigError("未配置 ros2_runtime.yaml 路径")
        try:
            candidate = load_ros2_runtime_config(
                self._runtime_config_path, self._protocol_config_path
            )
        except Ros2RuntimeConfigError as exc:
            self._runtime_config_error = str(exc)
            raise

        current = self._runtime_config
        if candidate == current:
            self._runtime_config_error = None
            self._runtime_loaded_at = self._now()
            self._runtime_config_mtime_ns = self._runtime_mtime()
            return False

        if self._running:
            self._replace_connection(
                candidate.gateway.host, candidate.gateway.port, candidate
            )
        else:
            self._runtime_config = candidate
            self.host = candidate.gateway.host
            self.port = candidate.gateway.port
            self.client = RosbridgeClient(
                host=self.host, port=self.port, connect_timeout=self.connect_timeout
            )
            self.tracker = self._new_tracker(self.client, candidate)
        self._runtime_generation += 1
        self._runtime_config_error = None
        self._runtime_loaded_at = self._now()
        self._runtime_config_mtime_ns = self._runtime_mtime()
        return True

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
        """Send one finalized intent at most once per intent identity.

        A failed transport attempt keeps the same ROS task ID for an explicit retry.
        When persistent dispatch records are enabled, idempotency also spans
        process restarts.
        """
        if not self.is_healthy():
            raise RuntimeError(
                f"MCPBridgeService 未连接到支持船网关 (ws://{self.host}:{self.port})"
            )
        identity = self._intent_identity(task_intent)

        with self._dispatch_lock:
            existing = self._dispatch_records.get(identity)
            if self._dispatch_records_path is not None:
                with self._with_dispatch_file_lock():
                    self._load_dispatch_records()
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
                    self._record_dispatch(identity, record)

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
                        self._record_dispatch(identity, record)
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
                    self._record_dispatch(identity, record)

                logger.info(
                    "[MCPBridgeService] TaskIntent 已写入 ROS 2 传输: intent_id=%s task_id=0x%X",
                    identity,
                    assigned_task_id,
                )
                return assigned_task_id

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
                self._dispatch_records[identity] = record
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
            self._dispatch_records[identity] = record
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
            "dynamic_subscriptions": self._dynamic_subscription_views(telemetry),
        }

    def _dynamic_subscription_views(
        self, telemetry: Optional[ROVTelemetry]
    ) -> list[dict[str, Any]]:
        config = self._runtime_config
        if config is None:
            return []
        with self._dynamic_lock:
            dynamic_messages = {
                key: dict(value) for key, value in self._dynamic_messages.items()
            }

        views = []
        for subscription in config.enabled_subscriptions:
            if subscription.parser == "system_status":
                message = telemetry.raw_msg if telemetry is not None else None
                received_at = telemetry.received_at if telemetry is not None else None
                message_count = self.tracker.message_count
            else:
                record = dynamic_messages.get(subscription.id, {})
                message = record.get("message")
                received_at = record.get("received_at")
                message_count = int(record.get("message_count", 0))
            views.append(subscription.build_view(
                message,
                received_at=received_at,
                message_count=message_count,
            ))
        return views

    def runtime_config_payload(self) -> Dict[str, Any]:
        config = self._runtime_config
        if config is None:
            return {
                "path": None,
                "generation": 0,
                "loaded_at": None,
                "last_error": self._runtime_config_error,
                "reload": None,
                "dashboard": {"refresh_interval_ms": 1000},
            }
        return {
            "path": str(self._runtime_config_path),
            "generation": self._runtime_generation,
            "loaded_at": self._runtime_loaded_at,
            "last_error": self._runtime_config_error,
            "reload": {
                "mode": config.reload.mode,
                "check_interval_seconds": config.reload.check_interval_seconds,
            },
            "dashboard": {
                "refresh_interval_ms": config.dashboard.refresh_interval_ms,
                "max_raw_message_bytes": config.dashboard.max_raw_message_bytes,
            },
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
            "runtime_config": self.runtime_config_payload(),
        }
