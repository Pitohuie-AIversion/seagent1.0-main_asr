"""
机器人实时状态存储。

读取和写入共享同一 selector 解析入口。更新事务在跨线程、跨进程锁内
完成 read-modify-write，并以同目录临时文件、fsync 和 os.replace 提交。
"""

from __future__ import annotations

import copy
import fcntl
import os
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

import yaml

from .exceptions import (
    StatePersistenceError,
    StateSelectorError,
    StateSnapshotValidationError,
    StateVersionConflict,
)
from .simulated_time import get_current_datetime


_SYSTEM_OWNED_FIELDS = {
    "version",
    "store_version",
    "updated_at",
    "update_timestamp",
}


def _normalize_selector(value: object) -> str:
    return str(value or "").strip().lower().replace(" ", "")


class RobotStateInfo:
    def __init__(
        self,
        state_file: Path | str | None = None,
        fleet_file: Path | str | None = None,
    ):
        config_dir = Path(__file__).parent.parent / "config"
        self.state_file = Path(state_file) if state_file else config_dir / "state.yaml"
        self.fleet_file = (
            Path(fleet_file) if fleet_file else config_dir / "robot_fleet.yaml"
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
            next_state.update(
                {
                    key: value
                    for key, value in params.items()
                    if key not in _SYSTEM_OWNED_FIELDS
                }
            )
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
                os.fchmod(lock_fd, 0o600)
            except OSError as exc:
                raise StatePersistenceError(
                    "Unable to open the robot state lock"
                ) from exc

            operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
            try:
                fcntl.flock(lock_fd, operation)
            except OSError as exc:
                os.close(lock_fd)
                raise StatePersistenceError(
                    "Unable to acquire the robot state lock"
                ) from exc
            try:
                yield
            finally:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                finally:
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
            if state.get("updated_at") is None and state.get("update_timestamp"):
                state["updated_at"] = state["update_timestamp"]
            elif state.get("updated_at"):
                state["update_timestamp"] = state["updated_at"]
        return normalized

    @staticmethod
    def _validate_version(value: object, field_name: str) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise StateSnapshotValidationError(
                f"{field_name} must be a non-negative integer"
            )

    def _save_state_unlocked(self, snapshot: Dict[str, Any]) -> None:
        normalized = self._normalize_snapshot(snapshot)
        try:
            serialized = yaml.safe_dump(
                normalized,
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
                    pass
                except OSError:
                    pass

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
                pass
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
                        pass
                try:
                    self._fsync_parent_directory()
                except OSError:
                    pass
                return
            try:
                os.unlink(self.state_file)
            except FileNotFoundError:
                pass
            try:
                self._fsync_parent_directory()
            except OSError:
                pass
        except OSError as exc:
            raise StatePersistenceError(
                "Robot state persistence failed and rollback was unsuccessful"
            ) from exc

    def _load_fleet(self) -> Dict[str, Any]:
        try:
            fleet = yaml.safe_load(self.fleet_file.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise StateSnapshotValidationError(
                "Robot fleet selector configuration is unavailable"
            ) from exc
        if not isinstance(fleet, dict):
            raise StateSnapshotValidationError(
                "Robot fleet selector configuration must be a mapping"
            )
        return fleet

    def _resolve_status_ref_from_snapshot(
        self,
        equipment_selector: str,
        snapshot: Dict[str, Any],
    ) -> Optional[str]:
        needle = _normalize_selector(equipment_selector)
        if not needle:
            return None
        fleet = self._load_fleet()
        units = fleet.get("fleet_units", [])
        if not isinstance(units, list):
            raise StateSnapshotValidationError("Robot fleet_units must be a list")

        for matches in (
            self._matching_unit_refs(units, needle),
            self._matching_variant_refs(fleet, units, needle),
            self._matching_family_refs(fleet, units, needle),
        ):
            if len(matches) == 1:
                return next(iter(matches))
            if len(matches) > 1:
                return None

        existing_refs = {
            status_ref
            for status_ref in snapshot["robots"]
            if _normalize_selector(status_ref) == needle
        }
        return next(iter(existing_refs)) if len(existing_refs) == 1 else None

    @staticmethod
    def _unit_status_ref(unit: Dict[str, Any]) -> Optional[str]:
        status_ref = unit.get("status_ref") or unit.get("unit_id")
        return str(status_ref) if status_ref else None

    def _matching_unit_refs(self, units: list, needle: str) -> set[str]:
        matches: set[str] = set()
        for unit in units:
            if not isinstance(unit, dict):
                raise StateSnapshotValidationError(
                    "Each fleet unit must be a mapping"
                )
            aliases = unit.get("aliases", [])
            if not isinstance(aliases, list):
                raise StateSnapshotValidationError(
                    "Fleet unit aliases must be a list"
                )
            targets = [
                unit.get("unit_id"),
                unit.get("display_name"),
                unit.get("serial_no"),
                unit.get("status_ref"),
                *aliases,
            ]
            if any(
                _normalize_selector(target) == needle
                for target in targets
                if target
            ):
                status_ref = self._unit_status_ref(unit)
                if status_ref:
                    matches.add(status_ref)
        return matches

    def _matching_variant_refs(
        self,
        fleet: Dict[str, Any],
        units: list,
        needle: str,
    ) -> set[str]:
        variants = fleet.get("model_variants", {})
        if not isinstance(variants, dict):
            raise StateSnapshotValidationError(
                "Robot model_variants must be a mapping"
            )
        matched_variant_ids: set[str] = set()
        for variant_id, variant in variants.items():
            if not isinstance(variant, dict):
                raise StateSnapshotValidationError(
                    "Each robot model variant must be a mapping"
                )
            aliases = variant.get("aliases", [])
            if not isinstance(aliases, list):
                raise StateSnapshotValidationError(
                    "Robot model variant aliases must be a list"
                )
            targets = [variant_id, variant.get("full_name"), *aliases]
            if any(
                _normalize_selector(target) == needle
                for target in targets
                if target
            ):
                matched_variant_ids.add(variant_id)
        return {
            status_ref
            for unit in units
            if unit.get("variant_id") in matched_variant_ids
            for status_ref in [self._unit_status_ref(unit)]
            if status_ref
        }

    def _matching_family_refs(
        self,
        fleet: Dict[str, Any],
        units: list,
        needle: str,
    ) -> set[str]:
        families = fleet.get("robot_families", {})
        variants = fleet.get("model_variants", {})
        if not isinstance(families, dict) or not isinstance(variants, dict):
            raise StateSnapshotValidationError(
                "Robot family selector configuration must be a mapping"
            )

        matched_family_ids: set[str] = set()
        for family_id, family in families.items():
            if not isinstance(family, dict):
                raise StateSnapshotValidationError(
                    "Each robot family must be a mapping"
                )
            aliases = family.get("aliases", [])
            if not isinstance(aliases, list):
                raise StateSnapshotValidationError(
                    "Robot family aliases must be a list"
                )
            targets = [family_id, family.get("full_name"), *aliases]
            if any(
                _normalize_selector(target) == needle
                for target in targets
                if target
            ):
                matched_family_ids.add(family_id)

        matched_variant_ids = {
            variant_id
            for variant_id, variant in variants.items()
            if isinstance(variant, dict)
            and variant.get("family_id") in matched_family_ids
        }
        return {
            status_ref
            for unit in units
            if unit.get("variant_id") in matched_variant_ids
            for status_ref in [self._unit_status_ref(unit)]
            if status_ref
        }
