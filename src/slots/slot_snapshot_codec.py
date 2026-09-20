"""
src/slot_snapshot_codec.py - 槽位快照编解码、结构验证与多版本迁移恢复器

职责：
1. 规范化槽位类型（normalize_slot_value_type）与规格参数校验（validate_specification_object）；
2. 多版本快照 Schema 结构契约校验（SnapshotValidationError）；
3. 机器人级联谱系完整性契约校验（_robot_selection_lineage_contract_error）；
4. 快照反序列化原子构建与向后兼容迁移（restore_snapshot）；
5. 槽位状态、未决条目、约束决策与指纹回执的序列化快照导出（export_snapshot）。
"""

from __future__ import annotations

import copy
import dataclasses
import logging
import math
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from .slot_store import Slot, ValidationAcknowledgement

from src.knowledge_retriever import (
    RobotSelectionDataError,
    robot_selection_result_contract_error,
)

logger = logging.getLogger("backend.slot_store")

SLOT_SNAPSHOT_SCHEMA_VERSION = 2

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


class SnapshotValidationError(ValueError):
    """Raised when a snapshot fails structure validation."""
    pass


def _robot_selection_lineage_contract_error(
    task_state: dict,
    selection: object,
) -> Optional[str]:
    """Return a missing canonical lineage field for snapshot migration.
    The shared validator contract intentionally checks only the deepest field
    for compatibility with lightweight runtime test doubles. Restore needs a
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
    if not required_fields:
        return None
    if not isinstance(selection, dict):
        return required_fields[0]
    for field_name in required_fields:
        value = selection.get(field_name)
        if not isinstance(value, str) or not value.strip():
            return field_name
    return None


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
    """验证完整的 Specification 对象结构。"""
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
    from .slot_store import Slot

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

    elif isinstance(raw_slot, Slot) or type(raw_slot).__name__ == "Slot":
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
                    except Exception as exc:
                        logger.debug(
                            "SlotStore: failed to parse datetime hint for string value: %s",
                            exc,
                        )
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
                except Exception as exc:
                    logger.debug(
                        "SlotStore: failed to parse datetime hint for auto value: %s",
                        exc,
                    )
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
            except Exception as exc:
                logger.debug(
                    "SlotStore: failed to parse datetime hint for string fallback value: %s",
                    exc,
                )
            return "string"

    return "string"


class SlotSnapshotCodec:
    """槽位快照编解码、迁移与校验器"""

    def __init__(self, store: Any = None) -> None:
        self.store = store

    @property
    def kb(self) -> Any:
        return getattr(self.store, "kb", None)

    def export_snapshot(self) -> Dict[str, Any]:
        """导出当前 SlotStore 状态为结构化快照字典。"""
        store = self.store
        val_data = None
        if store.validation_result is not None:
            if hasattr(store.validation_result, "__dataclass_fields__"):
                val_data = dataclasses.asdict(store.validation_result)
            elif isinstance(store.validation_result, dict):
                val_data = copy.deepcopy(store.validation_result)

        ack_data = []
        if store.validation_acknowledgements:
            for ack in store.validation_acknowledgements:
                if hasattr(ack, "__dataclass_fields__"):
                    ack_data.append(dataclasses.asdict(ack))
                elif isinstance(ack, dict):
                    ack_data.append(copy.deepcopy(ack))

        return {
            "snapshot_schema_version": SLOT_SNAPSHOT_SCHEMA_VERSION,
            "store_version": store.version,
            "slots": {
                key: slot.to_dict()
                for key, slot in store.slots.items()
            },
            "unresolved": copy.deepcopy(store.unresolved),
            "validation": val_data,
            "validation_acknowledgements": ack_data,
        }

    def restore_snapshot(self, snapshot: Dict[str, Any]) -> None:
        """验证并恢复快照状态至关联的 SlotStore 实例（Fail-Closed 原子保证）。"""
        from .slot_store import (
            Slot,
            ValidationAcknowledgement,
            BASE_SLOT_TYPES,
            ROBOT_CASCADE_DEPENDENCIES,
            reset_slot_to_missing,
        )

        store = self.store
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
                    from src.validation.validator import ValidationResult
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
                elif hasattr(a, "__dataclass_fields__") or type(a).__name__ == "ValidationAcknowledgement":
                    if isinstance(a, ValidationAcknowledgement):
                        parsed_acks.append(copy.deepcopy(a))
                    elif hasattr(a, "to_dict"):
                        parsed_acks.append(ValidationAcknowledgement.from_dict(a.to_dict()))
                    else:
                        parsed_acks.append(ValidationAcknowledgement.from_dict(dataclasses.asdict(a)))
                else:
                    raise SnapshotValidationError("Each entry in validation_acknowledgements must be a dictionary or ValidationAcknowledgement.")

        # Snapshot Migration Rule for legacy equipment_specification
        if "equipment_specification" in new_slots:
            legacy_spec_slot = new_slots.pop("equipment_specification")
            eq_type_slot = new_slots.get("equipment_type")
            if not (eq_type_slot and eq_type_slot.status == "valid" and eq_type_slot.value):
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
                    except Exception as exc:
                        logger.debug(
                            "SlotStore: load robot_fleet.yaml for legacy equipment_specification failed: %s",
                            exc,
                        )

                if variant_id not in model_variants:
                    raise SnapshotValidationError(
                        f"Legacy equipment_specification variant_id '{variant_id}' not found in robot fleet."
                    )

                var_info = model_variants[variant_id]
                fam_id = var_info.get("family_id")
                fam_info = robot_families.get(fam_id, {})
                cls_id = fam_info.get("robot_class")

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

        store._initialize_base_slots(new_slots)
        store.slots = new_slots
        store.version = store_ver
        store.unresolved = copy.deepcopy(unresolved_data)
        store.validation_result = val_obj
        store.validation_acknowledgements = parsed_acks
