"""
src/knowledge/models.py — 向后兼容垫片模块，下沉至 src/knowledge/selection/models.py
"""

from .selection.models import (
    CONFIG_DIR,
    PAYLOAD_GROUP_KEYS,
    SEABED_TYPE_CN,
    TELEMETRY_VALUE_CN,
    RobotSelectionDataError,
    RobotVariantFeasibility,
    _flatten_payload_items,
    _load,
    _norm,
    _payload_match_key,
    format_seabed_type,
    format_telemetry_value,
    normalize_payload_groups,
    normalize_supported_payloads,
    robot_selection_result_contract_error,
)

__all__ = [
    "CONFIG_DIR",
    "PAYLOAD_GROUP_KEYS",
    "SEABED_TYPE_CN",
    "TELEMETRY_VALUE_CN",
    "RobotSelectionDataError",
    "RobotVariantFeasibility",
    "_flatten_payload_items",
    "_load",
    "_norm",
    "_payload_match_key",
    "format_seabed_type",
    "format_telemetry_value",
    "normalize_payload_groups",
    "normalize_supported_payloads",
    "robot_selection_result_contract_error",
]
