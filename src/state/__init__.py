"""
src/state/__init__.py - 机器人状态管理、机队配置与运行时可用性核心包
"""

from src.state.fleet_selector import (
    load_fleet,
    matching_family_refs,
    matching_unit_refs,
    matching_variant_refs,
    normalize_selector,
    resolve_status_ref_from_snapshot,
    unit_status_ref,
)
from src.state.runtime_checker import (
    AVAILABLE_STATUSES,
    BUSY_STATUSES,
    OFFLINE_STATUSES,
    ROBOT_STATE_MAX_AGE_SECONDS,
    TELEMETRY_MAX_FUTURE_SKEW_SECONDS,
    inspect_robot_availability,
    parse_bool,
)

__all__ = [
    "load_fleet",
    "matching_family_refs",
    "matching_unit_refs",
    "matching_variant_refs",
    "normalize_selector",
    "resolve_status_ref_from_snapshot",
    "unit_status_ref",
    "inspect_robot_availability",
    "parse_bool",
    "ROBOT_STATE_MAX_AGE_SECONDS",
    "TELEMETRY_MAX_FUTURE_SKEW_SECONDS",
    "OFFLINE_STATUSES",
    "BUSY_STATUSES",
    "AVAILABLE_STATUSES",
]
