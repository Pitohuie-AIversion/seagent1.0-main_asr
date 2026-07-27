import copy
import logging
import threading
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

from src.simulated_time import get_current_datetime

logger = logging.getLogger("backend.slot_store")


class SlotVersionConflict(RuntimeError):
    """Raised when commit_transaction detects a store version mismatch."""
    pass


class SnapshotValidationError(ValueError):
    """Raised when a snapshot fails structure validation."""
    pass


BASE_SLOT_TYPES = {
    "task_type": "string",
    "task_type_key": "string",
    "emergency_mode": "boolean",
    "task_id": "string",
    "intent_id": "string",
    "equipment_family": "string",
    "equipment_type": "string",
    "equipment_name": "string",
}


INTERNAL_SLOT_TYPES = {
    "raw_oilfield_name": "string",
    "oilfield_match_status": "string",
    "oilfield_match_confidence": "number",
    "oilfield_match_evidence": "list",
    "oilfield_match_candidates": "list",
    "oilfield_entity_id": "string",
    "pending_oilfield_name": "string",
    "pending_oilfield_candidates": "list",
    "_rov_candidates": "list",
}


ALLOWED_INTERNAL_SLOTS = set(INTERNAL_SLOT_TYPES)

VALID_SLOT_STATUSES = {
    "missing",
    "candidate",
    "valid",
    "invalid",
    "conflict",
    "unresolved",
}

VALID_VALUE_TYPES = {"string", "number", "boolean", "list", "coord", "datetime", "object"}
LEGACY_SCHEMA_TYPES = {"tasktype", "auto", "fixed", "raw"}


def normalize_slot_value_type(schema_type: Optional[str] = None, value: Any = None) -> str:
    """Map schema behavior types or Python values to canonical runtime value types."""
    if schema_type:
        st = schema_type.lower()
        if st in VALID_VALUE_TYPES:
            if st == "string" and value is not None and not isinstance(value, str):
                pass
            else:
                return st
        if st in ("tasktype", "raw"):
            return "string"
        if st == "auto":
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return "number"
            if isinstance(value, bool):
                return "boolean"
            return "string"
        if st == "fixed":
            pass
        if st not in LEGACY_SCHEMA_TYPES:
            return schema_type

    if value is not None:
        if isinstance(value, bool):
            return "boolean"
        if isinstance(value, (int, float)):
            return "number"
        if isinstance(value, list):
            return "list"
        if isinstance(value, dict):
            if "lat" in value and "lon" in value:
                return "coord"
            return "object"
        if isinstance(value, str):
            if schema_type and schema_type.lower() == "datetime":
                try:
                    clean_ts = value.replace("Z", "+00:00")
                    datetime.fromisoformat(clean_ts)
                    return "datetime"
                except Exception:
                    pass
            return "string"

    return "string"


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
        self.value_type = normalize_slot_value_type(value_type, value)
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
    def __init__(self, kb=None):
        self.kb = kb
        self._lock = threading.RLock()
        self.slots: Dict[str, Slot] = {}
        self.unresolved: List[Any] = []
        self.version: int = 0
        self._initialize_base_slots()

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
        with self._lock:
            keys = (
                [field["key"] for field in output_schema]
                if output_schema is not None
                else list(self.slots.keys())
            )
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
        with self._lock:
            return {
                "store_version": self.version,
                "slots": {
                    key: slot.to_dict()
                    for key, slot in self.slots.items()
                },
                "unresolved": copy.deepcopy(self.unresolved),
            }

    def restore_snapshot(self, snapshot: Dict[str, Any]):
        with self._lock:
            if not isinstance(snapshot, dict):
                raise SnapshotValidationError("Snapshot must be a dictionary.")

            store_ver = snapshot.get("store_version")
            if store_ver is None or not isinstance(store_ver, int) or isinstance(store_ver, bool) or store_ver < 0:
                raise SnapshotValidationError("store_version must be a non-negative integer.")

            slots_data = snapshot.get("slots")
            if slots_data is None or not isinstance(slots_data, dict):
                raise SnapshotValidationError("slots must be a dictionary.")

            unresolved_data = snapshot.get("unresolved")
            if unresolved_data is None or not isinstance(unresolved_data, list):
                raise SnapshotValidationError("unresolved must be a list.")

            new_slots = {}
            for key, sdict in slots_data.items():
                if not isinstance(key, str):
                    raise SnapshotValidationError("Slot key must be a string.")
                if not isinstance(sdict, (dict, Slot)):
                    raise SnapshotValidationError(f"Slot data for key '{key}' must be a dict or Slot.")

                if isinstance(sdict, dict):
                    slot_name = sdict.get("slot_name")
                    if slot_name is not None and slot_name != key:
                        raise SnapshotValidationError(f"Slot key '{key}' does not match slot_name '{slot_name}'.")
                    status = sdict.get("status")
                    if status not in VALID_SLOT_STATUSES:
                        raise SnapshotValidationError(f"Invalid status '{status}' for slot '{key}'.")
                    version = sdict.get("version", 0)
                    if not isinstance(version, int) or isinstance(version, bool) or version < 0:
                        raise SnapshotValidationError(f"Invalid slot version '{version}' for slot '{key}'.")
                    confidence = sdict.get("confidence")
                    if confidence is not None and (
                        isinstance(confidence, bool)
                        or not isinstance(confidence, (int, float))
                        or not (0.0 <= float(confidence) <= 1.0)
                    ):
                        raise SnapshotValidationError(f"Invalid confidence '{confidence}' for slot '{key}'.")
                    raw_val_type = sdict.get("value_type", "string")
                    value = copy.deepcopy(sdict.get("value"))
                    if not isinstance(raw_val_type, str):
                        raise SnapshotValidationError(f"Invalid value_type '{raw_val_type}' for slot '{key}'.")
                    value_type = normalize_slot_value_type(raw_val_type, value)
                    if value_type not in VALID_VALUE_TYPES:
                        raise SnapshotValidationError(f"Invalid value_type '{raw_val_type}' for slot '{key}'.")
                    source = sdict.get("source", "user_input")
                    if not isinstance(source, str):
                        raise SnapshotValidationError(f"Invalid source for slot '{key}'.")
                    updated_at = sdict.get("updated_at")
                    if updated_at is not None:
                        if not isinstance(updated_at, str):
                            raise SnapshotValidationError(f"Invalid updated_at for slot '{key}'.")
                        try:
                            clean_dt = updated_at.replace("Z", "+00:00")
                            datetime.fromisoformat(clean_dt)
                        except Exception as exc:
                            raise SnapshotValidationError(
                                f"Invalid ISO-8601 updated_at timestamp '{updated_at}' for slot '{key}': {exc}"
                            )

                    if status == "valid" and value is None:
                        raise SnapshotValidationError(f"Valid slot '{key}' cannot have null value.")

                    new_slots[key] = Slot(
                        slot_name=key,
                        value=value,
                        value_type=value_type,
                        status=status,
                        source=source,
                        raw_value=copy.deepcopy(sdict.get("raw_value")),
                        confidence=confidence,
                        validation_error=sdict.get("validation_error"),
                        updated_at=updated_at,
                        version=version,
                        candidate_value=copy.deepcopy(sdict.get("candidate_value")),
                    )
                elif isinstance(sdict, Slot):
                    if sdict.slot_name != key:
                        raise SnapshotValidationError(f"Slot key '{key}' does not match slot_name '{sdict.slot_name}'.")
                    if sdict.status not in VALID_SLOT_STATUSES:
                        raise SnapshotValidationError(f"Invalid status '{sdict.status}' for slot '{key}'.")
                    value_type = normalize_slot_value_type(sdict.value_type, sdict.value)
                    if value_type not in VALID_VALUE_TYPES:
                        raise SnapshotValidationError(f"Invalid value_type '{sdict.value_type}' for slot '{key}'.")
                    sdict.value_type = value_type
                    if not isinstance(sdict.version, int) or isinstance(sdict.version, bool) or sdict.version < 0:
                        raise SnapshotValidationError(f"Invalid slot version '{sdict.version}' for slot '{key}'.")
                    if sdict.confidence is not None and (
                        isinstance(sdict.confidence, bool)
                        or not isinstance(sdict.confidence, (int, float))
                        or not (0.0 <= float(sdict.confidence) <= 1.0)
                    ):
                        raise SnapshotValidationError(f"Invalid confidence '{sdict.confidence}' for slot '{key}'.")
                    if sdict.updated_at is not None:
                        if not isinstance(sdict.updated_at, str):
                            raise SnapshotValidationError(f"Invalid updated_at for slot '{key}'.")
                        try:
                            clean_dt = sdict.updated_at.replace("Z", "+00:00")
                            datetime.fromisoformat(clean_dt)
                        except Exception as exc:
                            raise SnapshotValidationError(
                                f"Invalid ISO-8601 updated_at timestamp '{sdict.updated_at}' for slot '{key}': {exc}"
                            )
                    if sdict.status == "valid" and sdict.value is None:
                        raise SnapshotValidationError(f"Valid slot '{key}' cannot have null value.")
                    new_slots[key] = sdict.copy()
                    new_slots[key].slot_name = key

            self._initialize_base_slots(new_slots)
            self.slots = new_slots
            self.version = store_ver
            self.unresolved = copy.deepcopy(unresolved_data)

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

            temp_slots = {key: slot.copy() for key, slot in new_slots.items()}
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
