"""
机器人实时状态存储。

读取和写入共享同一 selector 解析入口。更新事务在跨线程、跨进程锁内
完成 read-modify-write，并以同目录临时文件、fsync 和 os.replace 提交。
"""

from __future__ import annotations

import copy
import fcntl
import os
import logging
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

import yaml

from .exceptions import (
    StatePersistenceError,
    StateSelectorError,
    StateSnapshotValidationError,
    StateVersionConflict,
)
from src.temporal.simulated_time import get_current_datetime


_SYSTEM_OWNED_FIELDS = {
    "version",
    "store_version",
    "updated_at",
    "update_timestamp",
}

logger = logging.getLogger(__name__)

from src.state import (
    AVAILABLE_STATUSES,
    BUSY_STATUSES,
    OFFLINE_STATUSES,
    ROBOT_STATE_MAX_AGE_SECONDS,
    TELEMETRY_MAX_FUTURE_SKEW_SECONDS,
    inspect_robot_availability,
    load_fleet as _load_fleet_cfg,
    matching_family_refs as _match_family_refs,
    matching_unit_refs as _match_unit_refs,
    matching_variant_refs as _match_variant_refs,
    normalize_selector,
    parse_bool,
    resolve_status_ref_from_snapshot as _resolve_status_ref_impl,
    unit_status_ref as _unit_status_ref_impl,
)

_normalize_selector = normalize_selector
_parse_bool = parse_bool


class RobotStateInfo:
    def __init__(
        self,
        state_file: Path | str | None = None,
        fleet_file: Path | str | None = None,
    ):
        config_dir = Path(__file__).parent.parent / "config"
        env_state_file = os.environ.get("SEAGENT_STATE_FILE")
        env_fleet_file = os.environ.get("SEAGENT_ROBOT_FLEET_FILE")
        self.state_file = (
            Path(state_file)
            if state_file
            else Path(env_state_file)
            if env_state_file
            else config_dir / "state.yaml"
        )
        self.fleet_file = (
            Path(fleet_file)
            if fleet_file
            else Path(env_fleet_file)
            if env_fleet_file
            else config_dir / "robot_fleet.yaml"
        )
        self._thread_lock = threading.RLock()

    def set_status(
        self,
        equipment_name: str,
        params: dict,
        expected_version: int | None = None,
    ) -> dict:
        """Atomically merge one robot telemetry update and return its versions."""
        self._validate_update_request(equipment_name, params, expected_version)
        with self._snapshot_lock(exclusive=True):
            snapshot = self._load_state_unlocked()
            status_ref = self._resolve_status_ref_from_snapshot(
                equipment_name,
                snapshot,
            )
            if status_ref is None:
                raise StateSelectorError(
                    "Robot selector does not resolve to a unique configured device"
                )

            robots = snapshot["robots"]
            current_state = robots.get(status_ref, {"version": 0})
            current_version = current_state.get("version", 0)
            if expected_version is not None and expected_version != current_version:
                raise StateVersionConflict(
                    status_ref,
                    expected_version,
                    current_version,
                )

            next_state = copy.deepcopy(current_state)
            for key, value in params.items():
                if key in _SYSTEM_OWNED_FIELDS:
                    continue
                next_state[key] = value
                if key == "current_velocity":
                    next_state["water_current_velocity"] = value
                elif key == "water_current_velocity":
                    next_state["current_velocity"] = value
                elif key == "turbidity":
                    next_state["water_turbidity"] = value
                elif key == "water_turbidity":
                    next_state["turbidity"] = value
                elif key == "overall_status":
                    if value in ("available", "idle", "ready") and "is_busy" not in params:
                        next_state["is_busy"] = False
                    elif value in ("busy", "working", "operating", "executing") and "is_busy" not in params:
                        next_state["is_busy"] = True
            updated_at = get_current_datetime().isoformat(timespec="microseconds")
            next_state["version"] = current_version + 1
            next_state["updated_at"] = updated_at
            next_state["update_timestamp"] = updated_at
            snapshot["store_version"] = snapshot["store_version"] + 1
            robots[status_ref] = next_state

            self._save_state_unlocked(snapshot)
            return {
                "status_ref": status_ref,
                "state": copy.deepcopy(next_state),
                "version": next_state["version"],
                "store_version": snapshot["store_version"],
                "updated_at": updated_at,
            }

    def get_unit_state_snapshot(self, unit_id: str) -> dict:
        """
        获取指定具体单机的严格状态快照。
        
        必须指定 unit_id 并精确匹配 fleet_units[*].unit_id。
        从匹配的 unit 获取 status_ref，读取 state。
        不允许使用 status_ref、family、型号或展示名称模糊查询。
        状态不存在、结构非法、版本非法、时间戳非法时抛出类型化异常。
        """
        if not isinstance(unit_id, str) or not unit_id.strip():
            raise StateSelectorError("unit_id 必须为非空字符串")
        
        clean_unit_id = unit_id.strip()
        fleet = self._load_fleet()
        units = fleet.get("fleet_units", [])
        matched_unit = None
        if isinstance(units, list):
            for u in units:
                if isinstance(u, dict) and u.get("unit_id") == clean_unit_id:
                    matched_unit = u
                    break
        
        if matched_unit is None:
            raise StateSelectorError(f"未找到匹配的单机 ID: {clean_unit_id}")
            
        canonical_unit_id = str(matched_unit["unit_id"])
        status_ref = str(matched_unit.get("status_ref") or canonical_unit_id)

        with self._snapshot_lock(exclusive=False):
            snapshot = self._load_state_unlocked()
            store_version = snapshot.get("store_version", 0)
            robots = snapshot.get("robots", {})
            state = robots.get(status_ref)
            
        if not isinstance(state, dict):
            raise StateSnapshotValidationError(f"单机 {canonical_unit_id} (status_ref: {status_ref}) 的状态记录不存在或非字典")
            
        version = state.get("version")
        if version is None or not isinstance(version, int) or isinstance(version, bool) or version < 0:
            raise StateSnapshotValidationError(f"单机 {canonical_unit_id} 状态版本非法: {version}")
            
        updated_at = state.get("updated_at") or state.get("update_timestamp") or state.get("update_at")
        if not updated_at or not isinstance(updated_at, str):
            updated_at = get_current_datetime().isoformat(timespec="microseconds")
            state["updated_at"] = updated_at
            state["update_timestamp"] = updated_at
        try:
            clean_ts = updated_at.replace("Z", "+00:00")
            datetime.fromisoformat(clean_ts)
        except Exception as exc:
            updated_at = get_current_datetime().isoformat(timespec="microseconds")
            state["updated_at"] = updated_at
            state["update_timestamp"] = updated_at
            
        return {
            "unit_id": canonical_unit_id,
            "status_ref": status_ref,
            "state_version": version,
            "store_version": store_version,
            "updated_at": updated_at,
            "state": copy.deepcopy(state),
        }

    @contextmanager
    def guard_unit_state_version(
        self,
        unit_id: str,
        expected_state_version: int,
    ) -> Iterator[None]:
        """
        在共享锁保护下确认 unit_id 的 state_version 与预期一致，
        并在上下文退出前持续持有共享锁。
        """
        if not isinstance(unit_id, str) or not unit_id.strip():
            raise StateSelectorError("unit_id 必须为非空字符串")

        clean_unit_id = unit_id.strip()
        fleet = self._load_fleet()
        units = fleet.get("fleet_units", [])
        matched_unit = None
        if isinstance(units, list):
            for u in units:
                if isinstance(u, dict) and u.get("unit_id") == clean_unit_id:
                    matched_unit = u
                    break

        if matched_unit is None:
            raise StateSelectorError(f"未找到匹配的单机 ID: {clean_unit_id}")

        canonical_unit_id = str(matched_unit["unit_id"])
        status_ref = str(matched_unit.get("status_ref") or canonical_unit_id)

        snap = self.get_unit_state_snapshot(canonical_unit_id)
        current_version = snap.get("state_version")
        if current_version is None or not isinstance(current_version, int) or current_version < 0:
            raise StateSnapshotValidationError(
                f"单机 {canonical_unit_id} 状态版本非法: {current_version}"
            )
        if current_version != expected_state_version:
            raise StateVersionConflict(
                status_ref=status_ref,
                expected_version=expected_state_version,
                current_version=current_version,
            )
        yield

    def resolve_status_ref(self, equipment_selector: str) -> Optional[str]:
        if not isinstance(equipment_selector, str) or not equipment_selector.strip():
            return None
        with self._snapshot_lock(exclusive=False):
            snapshot = self._load_state_unlocked()
            return self._resolve_status_ref_from_snapshot(
                equipment_selector,
                snapshot,
            )

    def get_robot_state(
        self,
        equipment_name: str,
    ) -> Optional[Dict[str, Any]]:
        if not isinstance(equipment_name, str) or not equipment_name.strip():
            return None
        with self._snapshot_lock(exclusive=False):
            snapshot = self._load_state_unlocked()
            status_ref = self._resolve_status_ref_from_snapshot(
                equipment_name,
                snapshot,
            )
            if status_ref is None:
                return None
            state = snapshot["robots"].get(status_ref)
            return copy.deepcopy(state) if state is not None else None

    def get_all_info(
        self,
        equipment_name: str | None = None,
    ) -> Dict[str, Any] | None:
        """Keep the legacy public interface while using unified resolution."""
        if equipment_name is not None:
            return self.get_robot_state(equipment_name)
        with self._snapshot_lock(exclusive=False):
            snapshot = self._load_state_unlocked()
            return copy.deepcopy(snapshot["robots"])

    def get_store_version(self) -> int:
        with self._snapshot_lock(exclusive=False):
            snapshot = self._load_state_unlocked()
            return int(snapshot.get("store_version", 0))

    def check_runtime_availability(
        self,
        unit_id: str,
        *,
        max_age_seconds: int = ROBOT_STATE_MAX_AGE_SECONDS,
    ) -> Dict[str, Any]:
        """检查机器人的实时可用性 (设备精确存在、在线、空闲且状态快照未过期)。"""
        now_dt = get_current_datetime()
        if not isinstance(unit_id, str) or not unit_id.strip():
            return inspect_robot_availability(
                unit_id,
                matched_unit=None,
                state=None,
                max_age_seconds=max_age_seconds,
                now_dt=now_dt,
            )

        clean_unit_id = unit_id.strip()

        # 1. 严格检查 clean_unit_id 是否精确存在于后端 Registry (robot_fleet.yaml) 的 fleet_units[*].unit_id
        matched_unit = None
        try:
            fleet = self._load_fleet()
            units = fleet.get("fleet_units", [])
            if isinstance(units, list):
                for u in units:
                    if isinstance(u, dict) and u.get("unit_id") == clean_unit_id:
                        matched_unit = u
                        break
        except (OSError, StateSnapshotValidationError, yaml.YAMLError):
            matched_unit = None

        if matched_unit is None:
            return inspect_robot_availability(
                clean_unit_id,
                matched_unit=None,
                state=None,
                max_age_seconds=max_age_seconds,
                now_dt=now_dt,
            )

        status_ref = str(matched_unit.get("status_ref") or matched_unit.get("unit_id") or clean_unit_id)

        # 2. 检查对应机器人状态记录是否存在
        with self._snapshot_lock(exclusive=False):
            snapshot = self._load_state_unlocked()
            robots_map = snapshot.get("robots", {})
            state = robots_map.get(status_ref)

        return inspect_robot_availability(
            clean_unit_id,
            matched_unit=matched_unit,
            state=state,
            max_age_seconds=max_age_seconds,
            now_dt=now_dt,
        )


    def _validate_update_request(
        self,
        equipment_name: str,
        params: dict,
        expected_version: int | None,
    ) -> None:
        if not isinstance(equipment_name, str) or not equipment_name.strip():
            raise StateSelectorError("robot_name must be a non-empty string")
        if not isinstance(params, dict) or not params:
            raise ValueError("params must be a non-empty object")
        if expected_version is None:
            return
        if (
            isinstance(expected_version, bool)
            or not isinstance(expected_version, int)
            or expected_version < 0
        ):
            raise ValueError("expected_version must be a non-negative integer")

    @property
    def lock_file(self) -> Path:
        """Derive the lock dynamically when state_file is reassigned."""
        return self.state_file.with_name(f".{self.state_file.name}.lock")

    @contextmanager
    def _snapshot_lock(self, exclusive: bool) -> Iterator[None]:
        with self._thread_lock:
            try:
                self.state_file.parent.mkdir(parents=True, exist_ok=True)
                lock_fd = os.open(
                    self.lock_file,
                    os.O_RDWR | os.O_CREAT,
                    0o600,
                )
            except OSError as exc:
                raise StatePersistenceError(
                    "Unable to open the robot state lock"
                ) from exc

            try:
                try:
                    os.fchmod(lock_fd, 0o600)
                except OSError as exc:
                    raise StatePersistenceError(
                        "Unable to open the robot state lock"
                    ) from exc

                operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
                try:
                    fcntl.flock(lock_fd, operation)
                except OSError as exc:
                    raise StatePersistenceError(
                        "Unable to acquire the robot state lock"
                    ) from exc
                try:
                    yield
                finally:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                # Setup and interrupted acquisition also own an open descriptor.
                os.close(lock_fd)

    def _load_state_unlocked(self) -> Dict[str, Any]:
        if not self.state_file.exists():
            return {"store_version": 0, "robots": {}}
        try:
            raw_text = self.state_file.read_text(encoding="utf-8")
        except OSError as exc:
            raise StatePersistenceError(
                "Unable to read the robot state snapshot"
            ) from exc
        if not raw_text.strip():
            raise StateSnapshotValidationError("Robot state snapshot is empty")
        try:
            snapshot = yaml.safe_load(raw_text)
        except yaml.YAMLError as exc:
            raise StateSnapshotValidationError(
                "Robot state snapshot contains malformed YAML"
            ) from exc
        return self._normalize_snapshot(snapshot)

    def _normalize_snapshot(self, snapshot: object) -> Dict[str, Any]:
        if not isinstance(snapshot, dict):
            raise StateSnapshotValidationError(
                "Robot state snapshot must be a mapping"
            )
        normalized = copy.deepcopy(snapshot)
        store_version = normalized.get("store_version", 0)
        self._validate_version(store_version, "store_version")
        robots = normalized.get("robots", {})
        if not isinstance(robots, dict):
            raise StateSnapshotValidationError(
                "Robot state snapshot robots must be a mapping"
            )

        normalized["store_version"] = store_version
        normalized["robots"] = robots
        for status_ref, state in robots.items():
            if not isinstance(status_ref, str) or not status_ref:
                raise StateSnapshotValidationError(
                    "Robot state snapshot contains an invalid status_ref"
                )
            if not isinstance(state, dict):
                raise StateSnapshotValidationError(
                    "Each robot state must be a mapping"
                )
            version = state.get("version", 0)
            self._validate_version(version, f"robots.{status_ref}.version")
            state["version"] = version
            
            # 统一时间戳字段：若同时存在 update_at 与 updated_at，优先保留有效且较新的时间戳
            up_at = state.pop("update_at", None)
            up_timestamp = state.get("update_timestamp")
            updated_at = state.get("updated_at")

            candidates = [ts for ts in (updated_at, up_timestamp, up_at) if isinstance(ts, str) and ts.strip()]
            if candidates:
                # 按照 ISO 时间戳解析，选择最新有效的时间串作为 updated_at
                latest_ts = candidates[0]
                latest_dt = None
                for ts in candidates:
                    try:
                        clean_ts = ts.replace("Z", "+00:00")
                        dt = datetime.fromisoformat(clean_ts)
                        if latest_dt is None or dt > latest_dt:
                            latest_dt = dt
                            latest_ts = ts
                    except Exception as exc:
                        logger.debug(
                            "Robot state snapshot updated_at candidate %s invalid, skip: %s",
                            ts,
                            exc,
                        )
                state["updated_at"] = latest_ts
                state["update_timestamp"] = latest_ts
            else:
                default_ts = get_current_datetime().isoformat(timespec="microseconds")
                state["updated_at"] = default_ts
                state["update_timestamp"] = default_ts

            # 规范化水流速度与水体浑浊度双向别名（优先保留规范字段水流速度与水体浑浊度）
            w_vel = state.get("water_current_velocity")
            c_vel = state.get("current_velocity")
            if w_vel is None and c_vel is not None:
                state["water_current_velocity"] = c_vel
                state["current_velocity"] = c_vel
            elif w_vel is not None:
                state["current_velocity"] = w_vel
                state["water_current_velocity"] = w_vel

            w_turb = state.get("water_turbidity")
            c_turb = state.get("turbidity")
            if w_turb is None and c_turb is not None:
                state["water_turbidity"] = c_turb
                state["turbidity"] = c_turb
            elif w_turb is not None:
                state["turbidity"] = w_turb
                state["water_turbidity"] = w_turb

            # 规范化整体状态双向别名 (overall_status <-> status)
            ov_status = state.get("overall_status")
            st_status = state.get("status")
            if ov_status is None and st_status is not None:
                state["overall_status"] = st_status
                state["status"] = st_status
            elif ov_status is not None:
                state["status"] = ov_status
                state["overall_status"] = ov_status

        return normalized

    @staticmethod
    def _validate_version(value: object, field_name: str) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise StateSnapshotValidationError(
                f"{field_name} must be a non-negative integer"
            )

    def _save_state_unlocked(self, snapshot: Dict[str, Any]) -> None:
        normalized = self._normalize_snapshot(snapshot)
        to_dump = copy.deepcopy(normalized)
        for r_state in to_dump.get("robots", {}).values():
            if isinstance(r_state, dict):
                r_state.pop("update_timestamp", None)
                r_state.pop("current_velocity", None)
                r_state.pop("turbidity", None)
                r_state.pop("status", None)
        try:
            serialized = yaml.safe_dump(
                to_dump,
                allow_unicode=True,
                sort_keys=False,
            ).encode("utf-8")
        except yaml.YAMLError as exc:
            raise StatePersistenceError(
                "Unable to serialize the robot state snapshot"
            ) from exc

        original_existed = self.state_file.exists()
        try:
            original_bytes = (
                self.state_file.read_bytes() if original_existed else None
            )
        except OSError as exc:
            raise StatePersistenceError(
                "Unable to preserve the previous robot state snapshot"
            ) from exc

        temp_path: Path | None = None
        replaced = False
        try:
            temp_path = self._write_temp_file(serialized)
            os.replace(temp_path, self.state_file)
            replaced = True
            temp_path = None
            self._fsync_parent_directory()
            if self._load_state_unlocked() != normalized:
                raise StateSnapshotValidationError(
                    "Persisted robot state snapshot failed verification"
                )
        except (OSError, StatePersistenceError) as exc:
            if replaced:
                self._restore_original_unlocked(
                    original_bytes,
                    original_existed,
                )
            if isinstance(exc, StatePersistenceError):
                raise
            raise StatePersistenceError(
                "Unable to atomically persist the robot state snapshot"
            ) from exc
        finally:
            if temp_path is not None:
                try:
                    os.unlink(temp_path)
                except FileNotFoundError:
                    logger.debug(
                        "State persistence cleanup: temp file already removed while cleaning %s",
                        temp_path,
                    )
                except OSError:
                    logger.debug(
                        "State persistence cleanup: failed to remove temp file %s",
                        temp_path,
                    )

    def _write_temp_file(self, content: bytes) -> Path:
        file_descriptor, raw_path = tempfile.mkstemp(
            prefix=f".{self.state_file.name}.",
            suffix=".tmp",
            dir=self.state_file.parent,
        )
        temp_path = Path(raw_path)
        try:
            os.fchmod(file_descriptor, 0o600)
            with os.fdopen(file_descriptor, "wb") as temp_file:
                file_descriptor = -1
                temp_file.write(content)
                temp_file.flush()
                os.fsync(temp_file.fileno())
            return temp_path
        except BaseException:
            if file_descriptor >= 0:
                os.close(file_descriptor)
            try:
                os.unlink(temp_path)
            except FileNotFoundError:
                logger.debug(
                    "State persistence temp-write cleanup: temp file %s was already removed",
                    temp_path,
                )
            raise

    def _fsync_parent_directory(self) -> None:
        directory_fd = os.open(
            self.state_file.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def _restore_original_unlocked(
        self,
        original_bytes: bytes | None,
        original_existed: bool,
    ) -> None:
        try:
            if original_existed and original_bytes is not None:
                rollback_temp = self._write_temp_file(original_bytes)
                try:
                    os.replace(rollback_temp, self.state_file)
                finally:
                    try:
                        os.unlink(rollback_temp)
                    except FileNotFoundError:
                        logger.debug(
                            "State rollback cleanup: temp file %s was already removed",
                            rollback_temp,
                        )
                try:
                    self._fsync_parent_directory()
                except OSError:
                    logger.debug(
                        "State rollback cleanup: failed to fsync parent directory after rollback restore"
                    )
                return
            try:
                os.unlink(self.state_file)
            except FileNotFoundError:
                logger.debug(
                    "State rollback cleanup: original state file %s was already removed",
                    self.state_file,
                )
            try:
                self._fsync_parent_directory()
            except OSError:
                logger.debug(
                    "State rollback cleanup: failed to fsync parent directory after unlink"
                )
        except OSError as exc:
            raise StatePersistenceError(
                "Robot state persistence failed and rollback was unsuccessful"
            ) from exc

    def _load_fleet(self) -> Dict[str, Any]:
        return _load_fleet_cfg(self.fleet_file)

    def _resolve_status_ref_from_snapshot(
        self,
        equipment_selector: str,
        snapshot: Dict[str, Any],
    ) -> Optional[str]:
        return _resolve_status_ref_impl(
            equipment_selector,
            snapshot,
            self.fleet_file,
        )

    @staticmethod
    def _unit_status_ref(unit: Dict[str, Any]) -> Optional[str]:
        return _unit_status_ref_impl(unit)

    def _matching_unit_refs(self, units: list, needle: str) -> set[str]:
        return _match_unit_refs(units, needle)

    def _matching_variant_refs(
        self,
        fleet: Dict[str, Any],
        units: list,
        needle: str,
    ) -> set[str]:
        return _match_variant_refs(fleet, units, needle)

    def _matching_family_refs(
        self,
        fleet: Dict[str, Any],
        units: list,
        needle: str,
    ) -> set[str]:
        return _match_family_refs(fleet, units, needle)
