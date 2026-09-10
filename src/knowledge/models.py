"""
src/knowledge/models.py — 数据契约、异常定义与载荷规范化工具
"""

from __future__ import annotations

import yaml
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"


def _load(filename: str) -> dict | list:
    with open(CONFIG_DIR / filename, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _norm(value: object) -> str:
    return str(value or "").lower().replace(" ", "")


def _payload_match_key(value: object) -> str:
    text = str(value or "").strip().lower().replace(" ", "").replace("-", "").replace("_", "")
    for suffix in ("（可选）", "(可选)", "可选"):
        if text.endswith(suffix):
            text = text[: -len(suffix)]
            break
    return text


PAYLOAD_GROUP_KEYS = (
    "Mechanical_arm",
    "End_effector",
    "Visual_sensor",
    "Propulsion_module",
    "Acoustic_sensor",
    "Navigation_sensor",
    "Other_sensor",
    "Operation_tool",
)

SEABED_TYPE_CN = {
    "soft": "软泥海床",
    "hard": "硬质海床",
    "sandy": "沙质海床",
    "mixed": "混合底质海床",
    "mud": "软泥海床",
    "silt": "淤泥海床",
    "rock": "岩石海床",
    "soft_to_medium": "软至中等底质",
    "unknown": "未知海床",
}


def format_seabed_type(raw_seabed: object) -> str:
    if not raw_seabed:
        return "未知海床"
    val = str(raw_seabed).strip().lower()
    return SEABED_TYPE_CN.get(val, str(raw_seabed))


TELEMETRY_VALUE_CN = {
    "available": "可用",
    "offline": "离线",
    "online": "在线",
    "busy": "忙碌",
    "idle": "空闲",
    "fault": "故障",
    "abnormal": "异常",
    "normal": "正常",
    "high": "高",
    "medium": "中",
    "low": "低",
    "strong": "强",
    "weak": "弱",
    "none": "无",
    "maintenance": "维保中",
    "soft": "软泥海床",
    "hard": "硬质海床",
    "sandy": "沙质海床",
    "mixed": "混合底质海床",
    "mud": "软泥海床",
    "silt": "淤泥海床",
    "rock": "岩石海床",
    "soft_to_medium": "软至中等底质",
    "unknown": "未知",
}


def format_telemetry_value(raw_val: object) -> str:
    if raw_val is None:
        return "未知"
    val_str = str(raw_val).strip()
    return TELEMETRY_VALUE_CN.get(val_str.lower(), val_str)


def _flatten_payload_items(raw: object) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw] if raw.strip() else []
    if isinstance(raw, list):
        items: list[str] = []
        for item in raw:
            items.extend(_flatten_payload_items(item))
        return items
    if isinstance(raw, dict):
        items: list[str] = []
        for item in raw.values():
            items.extend(_flatten_payload_items(item))
        return items
    return []


def normalize_payload_groups(raw: object) -> tuple[list[str], dict[str, list[str]]]:
    """Return flat payload names and stable UI groups from list or grouped YAML."""
    if isinstance(raw, list):
        flat = _flatten_payload_items(raw)
        return flat, {"Operation_tool": flat}

    if not isinstance(raw, dict):
        return [], {}

    groups: dict[str, list[str]] = {}
    flat: list[str] = []
    seen: set[str] = set()

    for key in PAYLOAD_GROUP_KEYS:
        items = _flatten_payload_items(raw.get(key))
        groups[key] = items
        for item in items:
            normalized = _payload_match_key(item)
            if normalized in seen:
                continue
            flat.append(item)
            seen.add(normalized)

    return flat, groups


def normalize_supported_payloads(raw: object) -> tuple[list[str], dict[str, list[str]]]:
    """Return a flat supported payload list and UI groups from old or new YAML."""
    return normalize_payload_groups(raw)


def robot_selection_result_contract_error(
    task_state: dict,
    selection: object,
    *,
    require_unit: bool = False,
) -> str | None:
    """Return the missing canonical key for an invalid static-validator result."""
    if not isinstance(task_state, dict):
        raise RobotSelectionDataError(
            "task_state must be a dictionary.",
            error_code="INVALID_TASK_STATE",
            actual_value=task_state,
        )

    selector_contract = (
        ("equipment_class", "robot_class", "INVALID_ROBOT_CLASS_SELECTOR"),
        ("equipment_family", "family_id", "INVALID_FAMILY_SELECTOR"),
        ("equipment_type", "variant_id", "INVALID_VARIANT_SELECTOR"),
        ("equipment_unit_id", "unit_id", "INVALID_UNIT_SELECTOR"),
    )
    explicit_selectors: dict[str, str] = {}
    for selector_key, canonical_key, error_code in selector_contract:
        if selector_key not in task_state or task_state[selector_key] is None:
            continue
        selector_value = task_state[selector_key]
        if not isinstance(selector_value, str) or not selector_value.strip():
            raise RobotSelectionDataError(
                f"{selector_key} must be a non-empty string when explicitly provided.",
                error_code=error_code,
                expected_field=selector_key,
                actual_value=selector_value,
            )
        explicit_selectors[selector_key] = canonical_key

    if require_unit and "equipment_unit_id" not in explicit_selectors:
        raise RobotSelectionDataError(
            "A concrete equipment_unit_id is required.",
            error_code="MISSING_UNIT_ID",
            expected_field="equipment_unit_id",
            actual_value=task_state.get("equipment_unit_id"),
        )

    expected_key = "unit_id" if require_unit else None
    if expected_key is None:
        for selector_key, canonical_key, _error_code in reversed(selector_contract):
            if selector_key in explicit_selectors:
                expected_key = explicit_selectors[selector_key]
                break
    if expected_key is None:
        return None
    if not isinstance(selection, dict):
        return expected_key
    canonical_value = selection.get(expected_key)
    if not isinstance(canonical_value, str) or not canonical_value.strip():
        return expected_key
    return None


@dataclass(frozen=True)
class RobotVariantFeasibility:
    eligible: bool
    reasons: tuple[str, ...] = ()
    requires_installation: tuple[str, ...] = ()


class RobotSelectionDataError(ValueError):
    """Exception raised when robot cascade query or static validation encounters invalid data or mismatched relationships."""

    def __init__(
        self,
        message: str,
        error_code: str,
        robot_class: str | None = None,
        family_id: str | None = None,
        variant_id: str | None = None,
        expected_field: str | None = None,
        actual_value: Any = None,
    ):
        super().__init__(message)
        self.error_code = error_code
        self.robot_class = robot_class
        self.family_id = family_id
        self.variant_id = variant_id
        self.expected_field = expected_field
        self.actual_value = actual_value

    def to_dict(self) -> dict[str, Any]:
        return {
            "error_code": self.error_code,
            "robot_class": self.robot_class,
            "family_id": self.family_id,
            "variant_id": self.variant_id,
            "expected_field": self.expected_field,
            "actual_value": self.actual_value,
            "message": str(self),
        }
