"""
src/slot_store.py - 槽位状态存储、乐观并发控制与事务提交流程

职责：
1. 槽位状态核心数据模型（Slot, SlotVersionConflict）；
2. 约束确认记录（ValidationAcknowledgement）与级联失效依赖定义；
3. 线程安全 RLock 与读视图快照（snapshot, clone_slots）；
4. 乐观并发版本控制与原子事务提交（commit_transaction）；
5. 委托子模块：
   - SlotListMutationEngine (src/slot_list_mutation.py)：载荷列表增量变异；
   - SlotSnapshotCodec (src/slot_snapshot_codec.py)：快照校验、反序列化与多版本迁移。
"""

from __future__ import annotations

import copy
import dataclasses
import logging
import threading
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from .simulated_time import get_current_datetime
from .slot_list_mutation import (
    SlotListMutationEngine,
    normalize_payload_match_key,
)
from .slot_snapshot_codec import (
    ALLOWED_INTERNAL_SLOTS,
    INTERNAL_SLOT_TYPES,
    LEGACY_SCHEMA_TYPES,
    SLOT_SNAPSHOT_SCHEMA_VERSION,
    SnapshotValidationError,
    SlotSnapshotCodec,
    VALID_SLOT_STATUSES,
    VALID_VALUE_TYPES,
    _robot_selection_lineage_contract_error,
    _validate_and_build_restored_slot,
    _validate_and_clone_slot_mapping,
    _validate_slot_value_type_compatibility,
    _validate_spec_slot_data,
    normalize_slot_value_type,
    validate_specification_object,
    validate_specification_selector_input,
)

logger = logging.getLogger("backend.slot_store")


if "SlotVersionConflict" not in globals():
    class SlotVersionConflict(RuntimeError):
        """Raised when commit_transaction detects a store version mismatch."""
        pass


if "ValidationAcknowledgement" not in globals():
    @dataclasses.dataclass
    class ValidationAcknowledgement:
        constraint_id: str
        acknowledged_at: str
        task_version: int
        validation_version: int
        validation_fingerprint: str
        status_ref: str
        state_version: int
        field: str = ""
        value: Any = None

        def to_dict(self) -> dict:
            return {
                "constraint_id": self.constraint_id,
                "acknowledged_at": self.acknowledged_at,
                "task_version": self.task_version,
                "validation_version": self.validation_version,
                "validation_fingerprint": self.validation_fingerprint,
                "status_ref": self.status_ref,
                "state_version": self.state_version,
                "field": self.field,
                "value": copy.deepcopy(self.value),
            }

        @classmethod
        def from_dict(cls, data: dict) -> "ValidationAcknowledgement":
            if not isinstance(data, dict):
                raise TypeError("ValidationAcknowledgement data must be a dictionary")
            return cls(
                constraint_id=str(data.get("constraint_id", "")),
                acknowledged_at=str(data.get("acknowledged_at", "")),
                task_version=int(data.get("task_version", 1)),
                validation_version=int(data.get("validation_version", 1)),
                validation_fingerprint=str(data.get("validation_fingerprint", "")),
                status_ref=str(data.get("status_ref", "")),
                state_version=int(data.get("state_version", 0)),
                field=str(data.get("field", "")),
                value=copy.deepcopy(data.get("value")),
            )


BASE_SLOT_TYPES = {
    "task_type": "string",
    "task_type_key": "string",
    "emergency_mode": "boolean",
    "task_id": "string",
    "intent_id": "string",
    "internal_id": "string",
    "equipment_class": "string",
    "equipment_family": "string",
    "equipment_type": "string",
    "equipment_name": "string",
    "equipment_unit_id": "string",
}

ROBOT_CASCADE_DEPENDENCIES = {
    "equipment_class": (
        "equipment_family",
        "equipment_type",
        "equipment_unit_id",
        "equipment_name",
    ),
    "equipment_family": (
        "equipment_type",
        "equipment_unit_id",
        "equipment_name",
    ),
    "equipment_type": (
        "equipment_unit_id",
        "equipment_name",
        "payload",
    ),
}


def reset_slot_to_missing(
    slot: "Slot",
    source: str = "system_dependency_invalidation",
) -> None:
    """Reset a slot completely to missing state during dependency invalidation."""
    slot.value = [] if slot.value_type == "list" else None
    slot.status = "missing"
    slot.candidate_value = None
    slot.raw_value = None
    slot.confidence = None
    slot.validation_error = None
    slot.source = source


def invalidate_robot_cascade_dependents(
    target_slots: Dict[str, "Slot"],
    changed_parent_keys: Any,
    preserve_keys: Optional[Any] = None,
) -> None:
    """Reset downstream dependent slots when a parent cascade slot changes."""
    preserve_set = set(preserve_keys) if preserve_keys else set()
    queue = list(changed_parent_keys or [])
    visited: set[str] = set()
    while queue:
        parent_key = queue.pop(0)
        if parent_key in visited:
            continue
        visited.add(parent_key)
        dependents = ROBOT_CASCADE_DEPENDENCIES.get(parent_key, ())
        for dep_key in dependents:
            if dep_key in preserve_set:
                continue
            if dep_key in target_slots:
                reset_slot_to_missing(target_slots[dep_key], source="system_dependency_invalidation")
            queue.append(dep_key)


if "Slot" not in globals():
    class Slot:
        def __init__(
            self,
            slot_name: str,
            value: Any = None,
            value_type: str = "string",
            status: str = "missing",
            source: str = "user_input",
            raw_value: Any = None,
            confidence: Optional[float] = None,
            validation_error: Optional[str] = None,
            updated_at: Optional[str] = None,
            version: int = 0,
            candidate_value: Any = None,
        ):
            self.slot_name = slot_name
            self.value = value
            if value_type == "string" and value is not None and not isinstance(value, str):
                self.value_type = normalize_slot_value_type(value=value)
            else:
                self.value_type = normalize_slot_value_type(schema_type=value_type, value=value)
            self.status = status  # missing | candidate | valid | invalid | conflict | unresolved
            self.source = source  # user_input | auto | fixed | system-derived values
            self.raw_value = raw_value
            self.confidence = confidence
            self.validation_error = validation_error
            self.updated_at = updated_at or datetime.now().isoformat()
            self.version = version
            self.candidate_value = candidate_value

        def to_dict(self) -> Dict[str, Any]:
            return {
                "slot_name": self.slot_name,
                "value": copy.deepcopy(self.value),
                "value_type": self.value_type,
                "status": self.status,
                "source": self.source,
                "raw_value": copy.deepcopy(self.raw_value),
                "confidence": self.confidence,
                "validation_error": self.validation_error,
                "updated_at": self.updated_at,
                "version": self.version,
                "candidate_value": copy.deepcopy(self.candidate_value),
            }

        def copy(self):
            return Slot(
                slot_name=self.slot_name,
                value=copy.deepcopy(self.value),
                value_type=self.value_type,
                status=self.status,
                source=self.source,
                raw_value=copy.deepcopy(self.raw_value),
                confidence=self.confidence,
                validation_error=self.validation_error,
                updated_at=self.updated_at,
                version=self.version,
                candidate_value=copy.deepcopy(self.candidate_value),
            )


class SlotStore:
    """槽位状态机存储与乐观并发事务控制器"""

    def __init__(self, kb=None):
        self.kb = kb
        self._lock = threading.RLock()
        self.slots: Dict[str, Slot] = {}
        self.unresolved: List[Any] = []
        self.version: int = 0
        self.validation_result: Dict[str, Any] | None = None
        self.validation_acknowledgements: List[Dict[str, Any]] = []
        self._initialize_base_slots()
        self._list_mutation_engine = SlotListMutationEngine(self)
        self._snapshot_codec = SlotSnapshotCodec(self)

    def _initialize_base_slots(self, slots_dict: Optional[Dict[str, Slot]] = None):
        target_slots = self.slots if slots_dict is None else slots_dict
        for key, vtype in {**BASE_SLOT_TYPES, **INTERNAL_SLOT_TYPES}.items():
            if key not in target_slots:
                target_slots[key] = Slot(slot_name=key, value_type=vtype)

    def init_task_slots(self, schema_fields: List[Dict[str, Any]]):
        """Synchronize store slots with the active task schema for legacy callers."""
        with self._lock:
            self._init_task_slots_in_transaction(self.slots, schema_fields)

    def _init_task_slots_in_transaction(
        self,
        target_slots: Dict[str, Slot],
        schema_fields: List[Dict[str, Any]],
    ):
        self._initialize_base_slots(target_slots)
        schema_keys = {field["key"] for field in schema_fields}

        to_remove = [
            key
            for key in target_slots
            if key not in BASE_SLOT_TYPES
            and key not in schema_keys
            and key not in ALLOWED_INTERNAL_SLOTS
        ]
        for key in to_remove:
            del target_slots[key]

        for field in schema_fields:
            key = field["key"]
            ftype = field.get("type", "string")
            current_value = target_slots[key].value if key in target_slots else None
            canonical_type = normalize_slot_value_type(ftype, current_value)
            if key not in target_slots:
                target_slots[key] = Slot(slot_name=key, value_type=canonical_type)
            elif target_slots[key].value_type != canonical_type:
                target_slots[key].value_type = canonical_type
                target_slots[key].value = None
                target_slots[key].candidate_value = None
                target_slots[key].status = "missing"

    def apply_list_mutation(
        self,
        new_slots: Dict[str, Slot],
        mutation: Dict[str, Any],
        required_schema: Optional[List[Dict[str, Any]]] = None,
        payload_catalog: Optional[Dict[str, Any]] = None,
        allowed_values_resolver: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """统一列表增量修改入口（委托给 SlotListMutationEngine 执行）。"""
        return self._list_mutation_engine.apply_list_mutation(
            new_slots=new_slots,
            mutation=mutation,
            required_schema=required_schema,
            payload_catalog=payload_catalog,
            allowed_values_resolver=allowed_values_resolver,
        )

    def get_task_state(self) -> Dict[str, Any]:
        """Returns ONLY status == 'valid' and non-None slots as current facts."""
        with self._lock:
            return {
                key: copy.deepcopy(slot.value)
                for key, slot in self.slots.items()
                if slot.status == "valid" and slot.value is not None
            }

    def get_slot_snapshot(self) -> Dict[str, Any]:
        """Returns full status dictionary of all slots."""
        with self._lock:
            return {
                key: copy.deepcopy(slot.to_dict())
                for key, slot in self.slots.items()
            }

    def get_built_json(
        self,
        output_schema: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Return valid slots, optionally projected to official output schema fields."""
        _INTERNAL_AUDIT_KEYS = {
            "raw_oilfield_name",
            "oilfield_match_status",
            "oilfield_match_confidence",
            "oilfield_match_evidence",
            "oilfield_match_candidates",
            "pending_oilfield_name",
            "pending_oilfield_candidates",
            "_rov_candidates",
        }
        with self._lock:
            if output_schema is not None:
                keys = [field["key"] for field in output_schema if field.get("key")]
            else:
                keys = [
                    k for k in self.slots.keys()
                    if not k.startswith("_") and k not in _INTERNAL_AUDIT_KEYS
                ]
            return {
                key: copy.deepcopy(self.slots[key].value)
                for key in keys
                if key in self.slots
                and self.slots[key].status == "valid"
                and self.slots[key].value is not None
            }

    def get_missing_slots(
        self,
        required_schema: List[Dict[str, Any]],
        allowed_values_resolver: Optional[Callable[[Dict[str, Any]], List[Any]]] = None,
    ) -> List[Dict[str, Any]]:
        """Return missing fields and optionally fill dynamic allowed values."""
        with self._lock:
            missing_fields = []
            for field in required_schema:
                key = field["key"]
                slot = self.slots.get(key)
                if slot and slot.status == "valid" and slot.value is not None:
                    continue
                missing_fields.append(copy.deepcopy(field))

        if allowed_values_resolver is not None:
            for field in missing_fields:
                field["allowed_values"] = list(allowed_values_resolver(field) or [])

        return missing_fields

    def export_snapshot(self) -> Dict[str, Any]:
        """导出快照（委托给 SlotSnapshotCodec 执行）。"""
        with self._lock:
            return self._snapshot_codec.export_snapshot()

    def restore_snapshot(self, snapshot: Dict[str, Any]) -> None:
        """恢复快照（委托给 SlotSnapshotCodec 执行）。"""
        with self._lock:
            self._snapshot_codec.restore_snapshot(snapshot)

    @classmethod
    def from_snapshot(cls, snapshot: Dict[str, Any], kb=None):
        store = cls(kb)
        store.restore_snapshot(snapshot)
        return store

    def clone_slots(self) -> Dict[str, Slot]:
        with self._lock:
            return {key: slot.copy() for key, slot in self.slots.items()}

    def snapshot(self) -> Tuple[Dict[str, Slot], List[Any], int]:
        with self._lock:
            return self.clone_slots(), copy.deepcopy(self.unresolved), self.version

    def commit_transaction(
        self,
        new_slots: Dict[str, Slot],
        new_unresolved: List[Any],
        request_id: str = "req_default",
        expected_version: Optional[int] = None,
    ):
        with self._lock:
            if expected_version is not None and expected_version != self.version:
                raise SlotVersionConflict(
                    f"SlotStore version conflict: expected version {expected_version}, "
                    f"but current store version is {self.version}"
                )

            if not isinstance(new_unresolved, list):
                raise SnapshotValidationError("unresolved must be a list.")

            temp_slots = _validate_and_clone_slot_mapping(new_slots)
            temp_unresolved = copy.deepcopy(new_unresolved)

            now_str = get_current_datetime().isoformat()
            task_id = (
                self.slots.get("task_id").value
                if self.slots.get("task_id") and self.slots.get("task_id").value
                else "unknown"
            )

            slot_changes_detected = False

            deleted_keys = set(self.slots.keys()) - set(temp_slots.keys())
            for key in deleted_keys:
                old_slot = self.slots[key]
                slot_changes_detected = True
                logger.info(
                    f"[SLOT_DELETE] task_id={task_id} request_id={request_id} "
                    f"store_version={self.version} slot_name={key} "
                    f"old_value={old_slot.value} old_status={old_slot.status} action=delete"
                )

            for key, new_slot in temp_slots.items():
                old_slot = self.slots.get(key)
                has_changed = False

                if not old_slot:
                    has_changed = True
                    old_val = None
                    old_status = "non_existent"
                    new_slot.version = 1
                    new_slot.updated_at = now_str
                else:
                    old_val = old_slot.value
                    old_status = old_slot.status
                    if (
                        old_slot.value != new_slot.value
                        or old_slot.value_type != new_slot.value_type
                        or old_slot.status != new_slot.status
                        or old_slot.source != new_slot.source
                        or old_slot.raw_value != new_slot.raw_value
                        or old_slot.confidence != new_slot.confidence
                        or old_slot.validation_error != new_slot.validation_error
                        or old_slot.candidate_value != new_slot.candidate_value
                    ):
                        has_changed = True
                        new_slot.version = old_slot.version + 1
                        new_slot.updated_at = now_str
                    else:
                        new_slot.version = old_slot.version
                        new_slot.updated_at = old_slot.updated_at

                if has_changed:
                    slot_changes_detected = True
                    logger.info(
                        f"[SLOT_UPDATE] task_id={task_id} request_id={request_id} "
                        f"store_version={self.version} slot_name={key} "
                        f"old_value={old_val} new_value={new_slot.value} "
                        f"old_status={old_status} new_status={new_slot.status} "
                        f"source={new_slot.source}"
                    )

            unresolved_changed = self.unresolved != temp_unresolved
            if unresolved_changed:
                logger.info(
                    f"[UNRESOLVED_UPDATE] task_id={task_id} request_id={request_id} "
                    f"store_version={self.version} old_unresolved={self.unresolved} "
                    f"new_unresolved={temp_unresolved}"
                )

            if slot_changes_detected or unresolved_changed:
                self.slots = temp_slots
                self.unresolved = temp_unresolved
                self.version += 1
