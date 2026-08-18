import copy
import dataclasses
import logging
import math
import threading
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

from src.simulated_time import get_current_datetime
from src.knowledge_retriever import (
    RobotSelectionDataError,
    robot_selection_result_contract_error,
)

logger = logging.getLogger("backend.slot_store")

_OPTIONAL_SUFFIXES = ("（可选）", "(可选)")


def normalize_payload_match_key(value: str) -> str:
    text = str(value or "").strip().lower().replace(" ", "")
    for suffix in _OPTIONAL_SUFFIXES:
        if text.endswith(suffix):
            text = text[:-len(suffix)]
            break
    return text


def _robot_selection_lineage_contract_error(
    task_state: dict,
    selection: object,
) -> str | None:
    """Return a missing canonical lineage field for snapshot migration.

    The shared validator contract intentionally checks only the deepest field
    for compatibility with lightweight runtime test doubles.  Restore needs a
    stronger result because it materializes missing ancestors from that result.
    """
    required_by_selector = (
        ("equipment_class", ("robot_class",)),
        ("equipment_family", ("robot_class", "family_id")),
        (
            "equipment_type",
            ("robot_class", "family_id", "variant_id", "equipment_type"),
        ),
        (
            "equipment_unit_id",
            (
                "robot_class",
                "family_id",
                "variant_id",
                "equipment_type",
                "unit_id",
            ),
        ),
    )
    required_fields: tuple[str, ...] = ()
    for selector_key, fields in required_by_selector:
        if selector_key in task_state and task_state.get(selector_key) is not None:
            required_fields = fields
    # A V1 stale-Variant migration can legitimately remove the only robot
    # selector.  In that case there is no lineage left to materialize and a
    # ``None`` validator result is the correct partial-state contract.
    if not required_fields:
        return None
    if not isinstance(selection, dict):
        return required_fields[0]
    for field_name in required_fields:
        value = selection.get(field_name)
        if not isinstance(value, str) or not value.strip():
            return field_name
    return None


class SlotVersionConflict(RuntimeError):
    """Raised when commit_transaction detects a store version mismatch."""
    pass


class SnapshotValidationError(ValueError):
    """Raised when a snapshot fails structure validation."""
    pass


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
    for parent_key in changed_parent_keys:
        dependents = ROBOT_CASCADE_DEPENDENCIES.get(parent_key, ())
        for dep_key in dependents:
            if dep_key in preserve_set:
                continue
            if dep_key in target_slots:
                reset_slot_to_missing(target_slots[dep_key], source="system_dependency_invalidation")


def validate_specification_selector_input(
    spec_val: Any,
    slot_key: str = "equipment_specification",
) -> None:
    """验证用户/运行时输入的最小 Specification 选择器对象（必需字段：type, value, variant_id）。"""
    if spec_val is None:
        return
    if isinstance(spec_val, bool) or not isinstance(spec_val, dict):
        raise SnapshotValidationError(f"Specification must be a typed dict with 'type', 'value', 'variant_id'; got {type(spec_val).__name__}: {spec_val!r}")

    required_fields = ("type", "value", "variant_id")
    for f in required_fields:
        if f not in spec_val:
            raise SnapshotValidationError(
                f"Specification missing required keys: ['{f}']"
            )

    spec_type = spec_val.get("type")
    if spec_type not in ("power_hp", "diameter_mm"):
        raise SnapshotValidationError(
            f"Slot '{slot_key}' specification type must be 'power_hp' or 'diameter_mm', got '{spec_type}'."
        )

    vid = spec_val.get("variant_id")
    if not isinstance(vid, str) or not vid:
        raise SnapshotValidationError(
            f"Slot '{slot_key}' specification variant_id must be a non-empty string."
        )

    val = spec_val.get("value")
    if (
        isinstance(val, bool)
        or not isinstance(val, (int, float))
        or not math.isfinite(val)
        or val <= 0
    ):
        raise SnapshotValidationError(
            f"Specification value must be a positive finite number; got {type(val).__name__}: {val!r}"
        )


def validate_specification_object(
    spec_val: Any,
    slot_key: str = "equipment_specification",
) -> None:
    if spec_val is None:
        return
    if isinstance(spec_val, bool) or not isinstance(spec_val, dict):
        raise SnapshotValidationError(f"Slot '{slot_key}' specification value must be a dictionary.")

    required_fields = ("type", "value", "unit", "display_value", "variant_id")
    for f in required_fields:
        if f not in spec_val:
            raise SnapshotValidationError(
                f"Slot '{slot_key}' specification object missing required field '{f}'."
            )

    spec_type = spec_val.get("type")
    if spec_type not in ("power_hp", "diameter_mm"):
        raise SnapshotValidationError(
            f"Slot '{slot_key}' specification type must be 'power_hp' or 'diameter_mm', got '{spec_type}'."
        )

    unit = spec_val.get("unit")
    unit_str = str(unit).lower() if unit is not None else ""
    if spec_type == "power_hp" and unit_str != "hp":
        raise SnapshotValidationError(
            f"Slot '{slot_key}' power_hp specification unit must be 'hp', got '{unit}'."
        )
    if spec_type == "diameter_mm" and unit_str != "mm":
        raise SnapshotValidationError(
            f"Slot '{slot_key}' diameter_mm specification unit must be 'mm', got '{unit}'."
        )

    disp = spec_val.get("display_value")
    if not isinstance(disp, str) or not disp:
        raise SnapshotValidationError(
            f"Slot '{slot_key}' specification display_value must be a non-empty string."
        )

    vid = spec_val.get("variant_id")
    if not isinstance(vid, str) or not vid:
        raise SnapshotValidationError(
            f"Slot '{slot_key}' specification variant_id must be a non-empty string."
        )

    val = spec_val.get("value")
    if (
        isinstance(val, bool)
        or not isinstance(val, (int, float))
        or not math.isfinite(val)
        or val <= 0
    ):
        raise SnapshotValidationError(
            f"Specification value must be a finite positive number, got {val}."
        )


def _validate_spec_slot_data(value: Any, candidate_val: Any, slot_key: str = "equipment_specification") -> None:
    if value is not None:
        validate_specification_object(value, slot_key=slot_key)
    if candidate_val is not None:
        if isinstance(candidate_val, dict) and ("type" in candidate_val or "display_value" in candidate_val or "variant_id" in candidate_val):
            try:
                validate_specification_selector_input(candidate_val, slot_key=slot_key)
            except SnapshotValidationError:
                validate_specification_object(candidate_val, slot_key=slot_key)
        else:
            validate_specification_object(candidate_val, slot_key=slot_key)


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
SLOT_SNAPSHOT_SCHEMA_VERSION = 2


def _validate_slot_value_type_compatibility(
    *,
    slot_key: str,
    value: Any,
    value_type: str,
    status: str,
) -> None:
    """Validate that actual slot value strictly matches its declared normalized value_type."""
    if status == "valid" and value is None:
        raise SnapshotValidationError(f"Valid slot '{slot_key}' cannot have null value.")

    if value is None:
        return

    if value_type == "string":
        if isinstance(value, bool) or not isinstance(value, str):
            raise SnapshotValidationError(
                f"Slot '{slot_key}' value {value!r} is not a valid string for value_type 'string'."
            )
    elif value_type == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise SnapshotValidationError(
                f"Slot '{slot_key}' value {value!r} is not a valid finite number for value_type 'number'."
            )
    elif value_type == "boolean":
        if not isinstance(value, bool):
            raise SnapshotValidationError(
                f"Slot '{slot_key}' value {value!r} is not a valid boolean for value_type 'boolean'."
            )
    elif value_type == "list":
        if not isinstance(value, list):
            raise SnapshotValidationError(
                f"Slot '{slot_key}' value {value!r} is not a valid list for value_type 'list'."
            )
    elif value_type == "object":
        if not isinstance(value, dict):
            raise SnapshotValidationError(
                f"Slot '{slot_key}' value {value!r} is not a valid dict for value_type 'object'."
            )
    elif value_type == "coord":
        if not isinstance(value, dict):
            raise SnapshotValidationError(
                f"Slot '{slot_key}' coord value must be a dictionary; got {type(value).__name__}: {value!r}"
            )
        if "lat" not in value or "lon" not in value:
            raise SnapshotValidationError(
                f"Slot '{slot_key}' coord dictionary missing required 'lat' or 'lon' keys."
            )
        lat = value["lat"]
        lon = value["lon"]
        if isinstance(lat, bool) or not isinstance(lat, (int, float)) or not math.isfinite(lat) or not (-90.0 <= float(lat) <= 90.0):
            raise SnapshotValidationError(
                f"Slot '{slot_key}' coord 'lat' must be a finite number between -90 and 90; got {lat!r}"
            )
        if isinstance(lon, bool) or not isinstance(lon, (int, float)) or not math.isfinite(lon) or not (-180.0 <= float(lon) <= 180.0):
            raise SnapshotValidationError(
                f"Slot '{slot_key}' coord 'lon' must be a finite number between -180 and 180; got {lon!r}"
            )
    elif value_type == "datetime":
        if isinstance(value, bool) or not isinstance(value, str):
            raise SnapshotValidationError(
                f"Slot '{slot_key}' datetime value must be an ISO-8601 string; got {type(value).__name__}: {value!r}"
            )
        try:
            clean_ts = value.replace("Z", "+00:00")
            datetime.fromisoformat(clean_ts)
        except Exception as exc:
            raise SnapshotValidationError(
                f"Slot '{slot_key}' datetime value '{value}' is not a valid ISO-8601 timestamp: {exc}"
            )


def _validate_and_build_restored_slot(
    key: str,
    raw_slot: Any,
    slots_data: Dict[str, Any],
) -> "Slot":
    """Unified validation and creation for dict and Slot representations in restore_snapshot."""
    if not isinstance(key, str):
        raise SnapshotValidationError("Slot key must be a string.")

    if isinstance(raw_slot, dict):
        slot_name = raw_slot.get("slot_name")
        if slot_name is not None and slot_name != key:
            raise SnapshotValidationError(f"Slot key '{key}' does not match slot_name '{slot_name}'.")

        status = raw_slot.get("status")
        if status not in VALID_SLOT_STATUSES:
            raise SnapshotValidationError(f"Invalid status '{status}' for slot '{key}'.")

        version = raw_slot.get("version", 0)
        if not isinstance(version, int) or isinstance(version, bool) or version < 0:
            raise SnapshotValidationError(f"Invalid slot version '{version}' for slot '{key}'.")

        confidence = raw_slot.get("confidence")
        if confidence is not None and (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not math.isfinite(float(confidence))
            or not (0.0 <= float(confidence) <= 1.0)
        ):
            raise SnapshotValidationError(f"Invalid confidence '{confidence}' for slot '{key}'.")

        raw_val_type = raw_slot.get("value_type", "auto")
        if not isinstance(raw_val_type, str):
            raise SnapshotValidationError(f"Invalid value_type '{raw_val_type}' for slot '{key}'.")

        value = copy.deepcopy(raw_slot.get("value"))
        value_type = normalize_slot_value_type(raw_val_type, value)
        if value_type not in VALID_VALUE_TYPES:
            raise SnapshotValidationError(f"Invalid value_type '{raw_val_type}' for slot '{key}'.")

        source = raw_slot.get("source", "user_input")
        if not isinstance(source, str):
            raise SnapshotValidationError(f"Invalid source for slot '{key}'.")

        updated_at = raw_slot.get("updated_at")
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

        candidate_val = copy.deepcopy(raw_slot.get("candidate_value"))
        raw_val = copy.deepcopy(raw_slot.get("raw_value"))
        val_error = raw_slot.get("validation_error")

    elif isinstance(raw_slot, Slot):
        if raw_slot.slot_name is not None and raw_slot.slot_name != key:
            raise SnapshotValidationError(f"Slot key '{key}' does not match slot_name '{raw_slot.slot_name}'.")

        status = raw_slot.status
        if status not in VALID_SLOT_STATUSES:
            raise SnapshotValidationError(f"Invalid status '{status}' for slot '{key}'.")

        version = raw_slot.version
        if not isinstance(version, int) or isinstance(version, bool) or version < 0:
            raise SnapshotValidationError(f"Invalid slot version '{version}' for slot '{key}'.")

        source = raw_slot.source
        if not isinstance(source, str):
            raise SnapshotValidationError(f"Invalid source for slot '{key}'.")

        confidence = raw_slot.confidence
        if confidence is not None and (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not math.isfinite(float(confidence))
            or not (0.0 <= float(confidence) <= 1.0)
        ):
            raise SnapshotValidationError(f"Invalid confidence '{confidence}' for slot '{key}'.")

        updated_at = raw_slot.updated_at
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

        value = copy.deepcopy(raw_slot.value)
        if (
            raw_slot.value_type == "string"
            and value is not None
            and not isinstance(value, str)
        ):
            # Runtime callers may populate a default-constructed Slot in
            # multiple steps. Mirror Slot.__init__ and canonicalize only that
            # default-string case on the detached transactional copy.
            value_type = normalize_slot_value_type(value=value)
        else:
            value_type = normalize_slot_value_type(raw_slot.value_type, value)
        if value_type not in VALID_VALUE_TYPES:
            raise SnapshotValidationError(f"Invalid value_type '{raw_slot.value_type}' for slot '{key}'.")

        candidate_val = copy.deepcopy(raw_slot.candidate_value)
        raw_val = copy.deepcopy(raw_slot.raw_value)
        val_error = copy.deepcopy(raw_slot.validation_error)

    else:
        raise SnapshotValidationError(f"Slot data for key '{key}' must be a dict or Slot.")

    _validate_slot_value_type_compatibility(
        slot_key=key,
        value=value,
        value_type=value_type,
        status=status,
    )

    if key == "equipment_specification":
        eq_type_in_snapshot = slots_data.get("equipment_type")
        is_type_valid = False
        if isinstance(eq_type_in_snapshot, dict):
            is_type_valid = (
                eq_type_in_snapshot.get("status") == "valid"
                and eq_type_in_snapshot.get("value") is not None
            )
        elif hasattr(eq_type_in_snapshot, "__dataclass_fields__"):
            is_type_valid = (
                getattr(eq_type_in_snapshot, "status", None) == "valid"
                and getattr(eq_type_in_snapshot, "value", None) is not None
            )
        if not is_type_valid:
            _validate_spec_slot_data(value, candidate_val, slot_key=key)

    return Slot(
        slot_name=key,
        value=value,
        value_type=value_type,
        status=status,
        source=source,
        raw_value=raw_val,
        confidence=confidence,
        validation_error=val_error,
        updated_at=updated_at,
        version=version,
        candidate_value=candidate_val,
    )


def _validate_and_clone_slot_mapping(
    slots_data: Dict[str, Any],
) -> Dict[str, "Slot"]:
    """Validate a complete Slot mapping and return detached canonical copies."""
    if not isinstance(slots_data, dict):
        raise SnapshotValidationError("slots must be a dictionary.")
    return {
        key: _validate_and_build_restored_slot(key, raw_slot, slots_data)
        for key, raw_slot in slots_data.items()
    }


def normalize_slot_value_type(schema_type: Optional[str] = None, value: Any = None) -> str:
    """Map schema behavior types or Python values to canonical runtime value types."""
    if schema_type:
        st = schema_type.lower()
        if st in VALID_VALUE_TYPES:
            if st == "string" and value is not None:
                if isinstance(value, dict) and "lat" in value and "lon" in value:
                    return "coord"
                if isinstance(value, str):
                    try:
                        clean_ts = value.replace("Z", "+00:00")
                        if len(value) >= 10 and "T" in value:
                            datetime.fromisoformat(clean_ts)
                            return "datetime"
                    except Exception:
                        pass
                    return "string"
                return "string"
            else:
                return st
        if st in ("tasktype", "raw"):
            return "string"
        if st == "auto":
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
                try:
                    clean_ts = value.replace("Z", "+00:00")
                    if len(value) >= 10 and "T" in value:
                        datetime.fromisoformat(clean_ts)
                        return "datetime"
                except Exception:
                    pass
                return "string"
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
            try:
                clean_ts = value.replace("Z", "+00:00")
                if len(value) >= 10 and "T" in value:
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
    def __init__(self, kb=None):
        self.kb = kb
        self._lock = threading.RLock()
        self.slots: Dict[str, Slot] = {}
        self.unresolved: List[Any] = []
        self.version: int = 0
        self.validation_result: Dict[str, Any] | None = None
        self.validation_acknowledgements: List[Dict[str, Any]] = []
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

    def apply_list_mutation(
        self,
        new_slots: Dict[str, Slot],
        mutation: Dict[str, Any],
        required_schema: Optional[List[Dict[str, Any]]] = None,
        payload_catalog: Optional[Dict[str, Any]] = None,
        allowed_values_resolver: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """统一列表增量修改入口（add/remove/replace/clear）。"""
        field_name = mutation.get("field", "payload")
        op = mutation.get("operation")
        raw_text = mutation.get("raw_text", "")
        confidence = mutation.get("confidence", 0.95)
        source = mutation.get("source", "user_input")

        schema_field = next(
            (
                field
                for field in required_schema or []
                if isinstance(field, dict) and field.get("key") == field_name
            ),
            None,
        )
        if required_schema is not None and schema_field is None:
            return {
                "success": False,
                "changed": False,
                "operation": op,
                "old_value": copy.deepcopy(
                    new_slots.get(field_name).value
                    if new_slots.get(field_name) is not None
                    else []
                ),
                "new_value": copy.deepcopy(
                    new_slots.get(field_name).value
                    if new_slots.get(field_name) is not None
                    else []
                ),
                "error": f"列表字段 '{field_name}' 不属于当前任务 schema",
            }

        if payload_catalog is None:
            if self.kb and hasattr(self.kb, "assets") and isinstance(self.kb.assets, dict):
                payload_catalog = self.kb.assets.get("payload_catalog", {})
            else:
                try:
                    from .extractor import _load_payload_catalog
                    payload_catalog = _load_payload_catalog()
                except Exception:
                    payload_catalog = {}

        allowed_values = []
        constrained_field = bool(
            schema_field
            and (
                "allowed_values" in schema_field
                or schema_field.get("allowed_values_ref")
            )
        )
        if schema_field is not None:
            if allowed_values_resolver:
                allowed_values = allowed_values_resolver(schema_field) or []
            else:
                allowed_values = schema_field.get("allowed_values") or []
                if (
                    not allowed_values
                    and schema_field.get("allowed_values_ref")
                    and self.kb
                ):
                    try:
                        from .output_builder import OutputBuilder
                        task_type_slot = new_slots.get("task_type_key")
                        task_type_key = (
                            task_type_slot.value
                            if task_type_slot
                            and task_type_slot.status == "valid"
                            and task_type_slot.value is not None
                            else ""
                        )
                        current_state = {
                            k: v.value
                            for k, v in new_slots.items()
                            if v
                            and v.status == "valid"
                            and v.value is not None
                        }
                        allowed_values = OutputBuilder(self.kb).resolve_allowed_values(
                            schema_field,
                            str(task_type_key or ""),
                            current_state,
                        ) or []
                    except Exception:
                        pass

        def _resolve(item_str: str) -> Tuple[Optional[str], Optional[str]]:
            """解析项的 (catalog_id, task_canonical_name)。

            P1-1: 第一优先级：检查 item_str 是否匹配当前任务 allowed_values（允许忽略“（可选）”等受控展示后缀）。
            只有在用户输入未匹配 allowed_values 时，才退而使用 catalog 别名映射。
            """
            text = str(item_str or "").strip()
            if not text:
                return None, None

            text_key = normalize_payload_match_key(text)

            if constrained_field and not allowed_values:
                return None, None

            if allowed_values:
                for a_val in allowed_values:
                    if isinstance(a_val, str) and normalize_payload_match_key(a_val) == text_key:
                        cat_id = None
                        for c_id, info in payload_catalog.items():
                            name = info.get("name", "")
                            aliases = info.get("aliases") or []
                            if any(cand and normalize_payload_match_key(cand) == text_key for cand in [name, *aliases]):
                                cat_id = c_id
                                break
                        return cat_id, a_val

            cat_id = None
            cat_candidates = []
            for c_id, info in payload_catalog.items():
                name = info.get("name", "")
                aliases = info.get("aliases") or []
                all_cands = [name, *aliases]
                for cand in all_cands:
                    if cand and normalize_payload_match_key(cand) == text_key:
                        cat_id = c_id
                        cat_candidates = [c for c in all_cands if c]
                        break
                if cat_id:
                    break

            if not cat_candidates:
                cat_candidates = [text]

            if allowed_values:
                for cand in cat_candidates:
                    cand_key = normalize_payload_match_key(cand)
                    for a_val in allowed_values:
                        if isinstance(a_val, str) and normalize_payload_match_key(a_val) == cand_key:
                            return cat_id, a_val
                return cat_id, None

            if cat_id and payload_catalog.get(cat_id, {}).get("name"):
                return cat_id, payload_catalog[cat_id]["name"]
            return cat_id, text

        def _contains(item_list: List[str], target: str) -> bool:
            t_id, t_name = _resolve(target)
            for item in item_list:
                i_id, i_name = _resolve(item)
                if t_id and i_id and t_id == i_id:
                    return True
                if t_name and i_name and t_name.lower().replace(" ", "") == i_name.lower().replace(" ", ""):
                    return True
            return False

        def _find_index(item_list: List[str], target: str) -> int:
            t_id, t_name = _resolve(target)
            for idx, item in enumerate(item_list):
                i_id, i_name = _resolve(item)
                if t_id and i_id and t_id == i_id:
                    return idx
                if t_name and i_name and t_name.lower().replace(" ", "") == i_name.lower().replace(" ", ""):
                    return idx
            return -1

        slot = new_slots.get(field_name)
        if slot is None:
            slot = Slot(slot_name=field_name, value_type="list", status="missing")
            new_slots[field_name] = slot

        old_val = slot.value
        if isinstance(old_val, list):
            old_value = copy.deepcopy(old_val)
        elif isinstance(old_val, str) and old_val.strip():
            old_value = [old_val.strip()]
        else:
            old_value = []

        temp_list = copy.deepcopy(old_value)

        def _fail(op_name: str, err_msg: str) -> Dict[str, Any]:
            slot.raw_value = raw_text
            slot.source = source
            slot.confidence = confidence
            slot.validation_error = err_msg
            return {
                "success": False,
                "changed": False,
                "operation": op_name,
                "old_value": old_value,
                "new_value": old_value,
                "error": err_msg,
            }

        if op == "add":
            items = mutation.get("items") or []
            new_canonicals = []
            for item_raw in items:
                cat_id, c_name = _resolve(item_raw)
                if c_name is None:
                    return _fail("add", f"添加的载荷 '{item_raw}' 非法或不属于当前任务允许范围")
                new_canonicals.append(c_name)

            for item_to_add in new_canonicals:
                if item_to_add and not _contains(temp_list, item_to_add):
                    temp_list.append(item_to_add)
            new_value = temp_list

        elif op == "remove":
            targets = mutation.get("items") or mutation.get("target_items") or []
            for target_raw in targets:
                _, c_name = _resolve(target_raw)
                target_to_remove = c_name or target_raw
                idx = _find_index(temp_list, target_to_remove)
                if idx < 0:
                    return _fail("remove", f"待删除载荷 '{target_raw}' 不在当前列表中")
                temp_list.pop(idx)
            new_value = temp_list

        elif op == "replace":
            targets = mutation.get("target_items") or []
            new_items_raw = mutation.get("items") or []

            target_indices = []
            for target_raw in targets:
                _, c_name = _resolve(target_raw)
                target_to_find = c_name or target_raw
                idx = _find_index(temp_list, target_to_find)
                if idx < 0:
                    return _fail("replace", f"待替换的目标载荷 '{target_raw}' 不在当前列表中")
                target_indices.append(idx)

            new_canonicals = []
            for n_raw in new_items_raw:
                cat_id, n_cname = _resolve(n_raw)
                if n_cname is None:
                    return _fail("replace", f"替换的新载荷 '{n_raw}' 非法或不属于当前任务允许范围")
                new_canonicals.append(n_cname)

            for idx in sorted(set(target_indices), reverse=True):
                temp_list.pop(idx)
            for item_to_add in new_canonicals:
                if item_to_add and not _contains(temp_list, item_to_add):
                    temp_list.append(item_to_add)
            new_value = temp_list
        elif op in ("set", "override"):
            items = mutation.get("items") or []
            new_canonicals = []
            for item_raw in items:
                cat_id, c_name = _resolve(item_raw)
                if c_name is None:
                    return _fail(str(op), f"设置的载荷 '{item_raw}' 非法或不属于当前任务允许范围")
                if c_name not in new_canonicals:
                    new_canonicals.append(c_name)
            new_value = new_canonicals

        elif op == "clear":
            slot.value = []
            slot.value_type = "list"
            slot.status = "missing"
            slot.source = source
            slot.raw_value = raw_text
            slot.confidence = confidence
            slot.candidate_value = None
            slot.validation_error = None
            return {
                "success": True,
                "changed": (old_value != []),
                "operation": "clear",
                "old_value": old_value,
                "new_value": [],
                "error": None,
            }

        else:
            err = f"不支持的 list mutation 操作 '{op}'"
            slot.validation_error = err
            return {
                "success": False,
                "changed": False,
                "operation": str(op),
                "old_value": old_value,
                "new_value": old_value,
                "error": err,
            }

        slot.value = new_value
        slot.value_type = "list"
        slot.status = "candidate"
        slot.source = source
        slot.raw_value = raw_text
        slot.confidence = confidence
        slot.candidate_value = None
        slot.validation_error = None

        return {
            "success": True,
            "changed": (old_value != new_value),
            "operation": op,
            "old_value": old_value,
            "new_value": new_value,
            "error": None,
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
        with self._lock:
            val_data = None
            if self.validation_result is not None:
                if hasattr(self.validation_result, "__dataclass_fields__"):
                    val_data = dataclasses.asdict(self.validation_result)
                elif isinstance(self.validation_result, dict):
                    val_data = copy.deepcopy(self.validation_result)

            ack_data = []
            if self.validation_acknowledgements:
                for ack in self.validation_acknowledgements:
                    if hasattr(ack, "__dataclass_fields__"):
                        ack_data.append(dataclasses.asdict(ack))
                    elif isinstance(ack, dict):
                        ack_data.append(copy.deepcopy(ack))

            return {
                "snapshot_schema_version": 2,
                "store_version": self.version,
                "slots": {
                    key: slot.to_dict()
                    for key, slot in self.slots.items()
                },
                "unresolved": copy.deepcopy(self.unresolved),
                "validation": val_data,
                "validation_acknowledgements": ack_data,
            }

    def restore_snapshot(self, snapshot: Dict[str, Any]):
        with self._lock:
            if not isinstance(snapshot, dict):
                raise SnapshotValidationError("Snapshot must be a dictionary.")

            snap_schema_ver = snapshot.get("snapshot_schema_version")
            if snap_schema_ver is not None:
                if (
                    isinstance(snap_schema_ver, bool)
                    or not isinstance(snap_schema_ver, int)
                    or snap_schema_ver != SLOT_SNAPSHOT_SCHEMA_VERSION
                ):
                    raise SnapshotValidationError(
                        f"Unsupported snapshot_schema_version: {snap_schema_ver!r}. Expected {SLOT_SNAPSHOT_SCHEMA_VERSION} or None for legacy V1."
                    )

            store_ver = snapshot.get("store_version", 1)
            if store_ver is None:
                store_ver = 1
            if not isinstance(store_ver, int) or isinstance(store_ver, bool) or store_ver < 0:
                raise SnapshotValidationError("store_version must be a non-negative integer.")

            slots_data = snapshot.get("slots")
            if slots_data is None or not isinstance(slots_data, dict):
                raise SnapshotValidationError("slots must be a dictionary.")

            unresolved_data = snapshot.get("unresolved", [])
            if unresolved_data is None:
                unresolved_data = []
            if not isinstance(unresolved_data, list):
                raise SnapshotValidationError("unresolved must be a list.")

            validation_data = snapshot.get("validation")
            if validation_data is None and "validation_result" in snapshot:
                validation_data = snapshot.get("validation_result")
            if validation_data is not None:
                if hasattr(validation_data, "__dataclass_fields__"):
                    validation_data = dataclasses.asdict(validation_data)
                elif not isinstance(validation_data, dict):
                    raise SnapshotValidationError("validation must be a dictionary or None.")

            ack_data = snapshot.get("validation_acknowledgements")
            if ack_data is not None:
                if not isinstance(ack_data, list):
                    raise SnapshotValidationError("validation_acknowledgements must be a list.")
                cleaned_ack = []
                for item in ack_data:
                    if hasattr(item, "__dataclass_fields__"):
                        cleaned_ack.append(dataclasses.asdict(item))
                    elif isinstance(item, dict):
                        cleaned_ack.append(copy.deepcopy(item))
                    else:
                        raise SnapshotValidationError("Each entry in validation_acknowledgements must be a dictionary.")
                ack_data = cleaned_ack

            new_slots = _validate_and_clone_slot_mapping(slots_data)

            val_obj = None
            if validation_data is not None:
                if isinstance(validation_data, dict):
                    try:
                        from src.validator import ValidationResult
                        val_obj = ValidationResult.from_dict(validation_data)
                    except Exception as exc:
                        raise SnapshotValidationError(f"Invalid validation_result format: {exc}")
                elif hasattr(validation_data, "__dataclass_fields__"):
                    val_obj = copy.deepcopy(validation_data)
                else:
                    raise SnapshotValidationError("validation must be a dictionary or ValidationResult.")

            parsed_acks = []
            if ack_data:
                for a in ack_data:
                    if isinstance(a, dict):
                        try:
                            parsed_acks.append(ValidationAcknowledgement.from_dict(a))
                        except Exception as exc:
                            raise SnapshotValidationError(f"Invalid validation_acknowledgements format: {exc}")
                    elif hasattr(a, "__dataclass_fields__"):
                        parsed_acks.append(copy.deepcopy(a))
                    else:
                        raise SnapshotValidationError("Each entry in validation_acknowledgements must be a dictionary or ValidationAcknowledgement.")

            # Snapshot Migration Rule for legacy equipment_specification
            if "equipment_specification" in new_slots:
                legacy_spec_slot = new_slots.pop("equipment_specification")
                eq_type_slot = new_slots.get("equipment_type")
                # Case A: equipment_type already valid -> ignore equipment_specification
                if not (eq_type_slot and eq_type_slot.status == "valid" and eq_type_slot.value):
                    # Case B: try to backfill equipment_type from legacy_spec_slot.variant_id
                    spec_val = (
                        legacy_spec_slot.value
                        if isinstance(legacy_spec_slot.value, dict)
                        else (
                            legacy_spec_slot.candidate_value
                            if isinstance(legacy_spec_slot.candidate_value, dict)
                            else {}
                        )
                    )
                    variant_id = (
                        spec_val.get("variant_id") if isinstance(spec_val, dict) else None
                    )
                    if not variant_id or not isinstance(variant_id, str):
                        raise SnapshotValidationError(
                            "Legacy equipment_specification missing valid variant_id for migration."
                        )

                    model_variants = {}
                    robot_families = {}
                    robot_classes = {}
                    if (
                        self.kb
                        and hasattr(self.kb, "robot_fleet")
                        and isinstance(self.kb.robot_fleet, dict)
                    ):
                        model_variants = self.kb.robot_fleet.get("model_variants", {})
                        robot_families = self.kb.robot_fleet.get("robot_families", {})
                        robot_classes = self.kb.robot_fleet.get("robot_classes", {})
                    else:
                        try:
                            import yaml

                            with open("config/robot_fleet.yaml", "r", encoding="utf-8") as f:
                                rf_cfg = yaml.safe_load(f) or {}
                            model_variants = rf_cfg.get("model_variants", {})
                            robot_families = rf_cfg.get("robot_families", {})
                            robot_classes = rf_cfg.get("robot_classes", {})
                        except Exception:
                            pass

                    if variant_id not in model_variants:
                        raise SnapshotValidationError(
                            f"Legacy equipment_specification variant_id '{variant_id}' not found in robot fleet."
                        )

                    var_info = model_variants[variant_id]
                    fam_id = var_info.get("family_id")
                    fam_info = robot_families.get(fam_id, {})
                    cls_id = fam_info.get("robot_class")

                    # Check family/class consistency if present in snapshot
                    fam_slot = new_slots.get("equipment_family")
                    cls_slot = new_slots.get("equipment_class")
                    fam_ok = True
                    if fam_slot and fam_slot.status == "valid" and fam_slot.value:
                        fam_ok = fam_slot.value in (fam_id, fam_info.get("full_name"))
                    cls_ok = True
                    if cls_slot and cls_slot.status == "valid" and cls_slot.value:
                        cls_name = robot_classes.get(cls_id, {}).get("full_name", cls_id)
                        cls_ok = cls_slot.value in (cls_id, cls_name)

                    if not (fam_ok and cls_ok):
                        raise SnapshotValidationError(
                            f"Legacy equipment_specification variant '{variant_id}' conflicts with equipment_class or equipment_family in snapshot."
                        )

                    new_slots["equipment_type"] = Slot(
                        slot_name="equipment_type",
                        value=var_info.get("full_name", variant_id),
                        value_type="string",
                        status="valid",
                        source="snapshot_migration",
                    )

            static_robot_validator = getattr(
                self.kb,
                "validate_robot_selection_from_task_state",
                None,
            )
            restored_task_state = {
                key: copy.deepcopy(slot.value)
                for key, slot in new_slots.items()
                if slot.status == "valid" and slot.value is not None
            }
            has_explicit_robot_selector = any(
                key in restored_task_state
                for key in (
                    "equipment_class",
                    "equipment_family",
                    "equipment_type",
                    "equipment_unit_id",
                )
            )
            if self.kb is not None and has_explicit_robot_selector:
                if not callable(static_robot_validator):
                    raise SnapshotValidationError(
                        "Invalid robot selection "
                        "[STATIC_ROBOT_VALIDATOR_UNAVAILABLE]: "
                        "robot hierarchy validator is unavailable."
                    )
                try:
                    canonical_selection = static_robot_validator(
                        restored_task_state,
                        require_unit=False,
                    )
                    missing_key = robot_selection_result_contract_error(
                        restored_task_state,
                        canonical_selection,
                        require_unit=False,
                    )
                    if missing_key is not None:
                        raise RobotSelectionDataError(
                            "Static robot validator result is missing canonical "
                            f"field '{missing_key}'.",
                            error_code="STATIC_ROBOT_VALIDATOR_FAILURE",
                            expected_field=missing_key,
                            actual_value=canonical_selection,
                        )
                    lineage_missing_key = _robot_selection_lineage_contract_error(
                        restored_task_state,
                        canonical_selection,
                    )
                    if lineage_missing_key is not None:
                        raise RobotSelectionDataError(
                            "Static robot validator result is missing canonical "
                            f"lineage field '{lineage_missing_key}'.",
                            error_code="STATIC_ROBOT_VALIDATOR_FAILURE",
                            expected_field=lineage_missing_key,
                            actual_value=canonical_selection,
                        )
                except Exception as exc:
                    error_code = getattr(
                        exc,
                        "error_code",
                        "ROBOT_SELECTION_VALIDATOR_FAILURE",
                    )
                    # Schema-less V1 snapshots may contain a historical
                    # Variant label that no longer exists in the current
                    # registry.  Without a Unit this is still a collecting
                    # state, so migrate only that stale deepest selector back
                    # to missing and validate the remaining parent prefix.
                    if (
                        snap_schema_ver is None
                        and "equipment_unit_id" not in restored_task_state
                        and error_code == "VARIANT_NOT_FOUND"
                        and "equipment_type" in restored_task_state
                    ):
                        legacy_type_slot = new_slots.get("equipment_type")
                        if legacy_type_slot is not None:
                            reset_slot_to_missing(
                                legacy_type_slot,
                                source="snapshot_migration",
                            )
                        restored_task_state.pop("equipment_type", None)
                        try:
                            canonical_selection = static_robot_validator(
                                restored_task_state,
                                require_unit=False,
                            )
                            missing_key = robot_selection_result_contract_error(
                                restored_task_state,
                                canonical_selection,
                                require_unit=False,
                            )
                            if missing_key is not None:
                                raise RobotSelectionDataError(
                                    "Static robot validator result is missing "
                                    f"canonical field '{missing_key}'.",
                                    error_code="STATIC_ROBOT_VALIDATOR_FAILURE",
                                    expected_field=missing_key,
                                    actual_value=canonical_selection,
                                )
                            lineage_missing_key = (
                                _robot_selection_lineage_contract_error(
                                    restored_task_state,
                                    canonical_selection,
                                )
                            )
                            if lineage_missing_key is not None:
                                raise RobotSelectionDataError(
                                    "Static robot validator result is missing "
                                    f"canonical lineage field '{lineage_missing_key}'.",
                                    error_code="STATIC_ROBOT_VALIDATOR_FAILURE",
                                    expected_field=lineage_missing_key,
                                    actual_value=canonical_selection,
                                )
                        except Exception as migration_exc:
                            migration_error_code = getattr(
                                migration_exc,
                                "error_code",
                                "ROBOT_SELECTION_VALIDATOR_FAILURE",
                            )
                            raise SnapshotValidationError(
                                "Invalid robot selection "
                                f"[{migration_error_code}]: {migration_exc}"
                            ) from migration_exc
                    else:
                        if error_code == "ROBOT_SELECTION_VALIDATOR_FAILURE":
                            logger.exception(
                                "Unexpected robot hierarchy validation failure during snapshot restore"
                            )
                        raise SnapshotValidationError(
                            f"Invalid robot selection [{error_code}]: {exc}"
                        ) from exc

                canonical_unit_id = (
                    canonical_selection.get("unit_id")
                    if isinstance(canonical_selection, dict)
                    else None
                )
                restored_unit_slot = new_slots.get("equipment_unit_id")
                if (
                    canonical_unit_id
                    and restored_unit_slot
                    and restored_unit_slot.status == "valid"
                    and restored_unit_slot.value != canonical_unit_id
                ):
                    legacy_selector = restored_unit_slot.value
                    restored_unit_slot.value = canonical_unit_id
                    restored_unit_slot.raw_value = str(legacy_selector)
                    restored_unit_slot.source = "snapshot_migration"
                    restored_unit_slot.candidate_value = None
                    restored_unit_slot.validation_error = None

                # A valid deeper selector has an authoritative Registry
                # lineage.  Snapshot migration may materialize missing
                # ancestors, but it must never choose a descendant or
                # overwrite an explicitly restored valid parent.
                canonical_family_name = None
                if isinstance(canonical_selection, dict):
                    family_id = canonical_selection.get("family_id")
                    robot_families = getattr(
                        self.kb,
                        "robot_fleet",
                        {},
                    ).get("robot_families", {})
                    family_cfg = (
                        robot_families.get(family_id, {})
                        if isinstance(robot_families, dict)
                        else {}
                    )
                    canonical_family_name = (
                        canonical_selection.get("family_name")
                        or family_cfg.get("full_name")
                        or family_id
                    )

                canonical_ancestors = (
                    (
                        "equipment_class",
                        canonical_selection.get("robot_class")
                        if isinstance(canonical_selection, dict)
                        else None,
                    ),
                    ("equipment_family", canonical_family_name),
                    (
                        "equipment_type",
                        canonical_selection.get("equipment_type")
                        if isinstance(canonical_selection, dict)
                        else None,
                    ),
                )
                selector_order = (
                    "equipment_class",
                    "equipment_family",
                    "equipment_type",
                    "equipment_unit_id",
                )
                deepest_explicit_index = max(
                    (
                        index
                        for index, selector_key in enumerate(selector_order)
                        if selector_key in restored_task_state
                    ),
                    default=-1,
                )
                for ancestor_index, (slot_key, canonical_value) in enumerate(
                    canonical_ancestors
                ):
                    # Registry lineage may only reconstruct ancestors of the
                    # deepest restored selector.  It must never invent a
                    # descendant merely because a broken helper returned one.
                    if ancestor_index >= deepest_explicit_index:
                        continue
                    if not isinstance(canonical_value, str) or not canonical_value.strip():
                        continue
                    current_slot = new_slots.get(slot_key)
                    # Candidate/conflict/invalid slots carry user and audit
                    # state.  Only an actually absent or semantically empty
                    # missing ancestor may be synthesized; all other states
                    # must round-trip unchanged.
                    if current_slot is not None and not (
                        current_slot.status == "missing"
                        and current_slot.value is None
                    ):
                        continue
                    new_slots[slot_key] = Slot(
                        slot_name=slot_key,
                        value=canonical_value,
                        value_type=BASE_SLOT_TYPES[slot_key],
                        status="valid",
                        source="snapshot_migration",
                        raw_value=None,
                        confidence=None,
                        validation_error=None,
                        candidate_value=None,
                    )

            self._initialize_base_slots(new_slots)
            self.slots = new_slots
            self.version = store_ver
            self.unresolved = copy.deepcopy(unresolved_data)
            self.validation_result = val_obj
            self.validation_acknowledgements = parsed_acks

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
