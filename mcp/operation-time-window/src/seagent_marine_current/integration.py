"""SEAgent integration bridge for ocean current queries and window evaluation."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

from .contracts import CurrentForecastData, CurrentQuery, ForecastReply
from .windows import OperationWindowService, WindowCheckResult, WindowSearchResult

logger = logging.getLogger(__name__)


def parse_datetime_safe(val: Any) -> Optional[datetime]:
    """Parse string or datetime to timezone-aware datetime."""
    if val is None:
        return None
    if isinstance(val, datetime):
        if val.tzinfo is None:
            return val.replace(tzinfo=timezone.utc)
        return val
    if isinstance(val, str) and val.strip():
        # Handle ISO strings or 'YYYY-MM-DD HH:MM:SS'
        val_str = val.strip().replace(" ", "T")
        try:
            dt = datetime.fromisoformat(val_str)
            if dt.tzinfo is None:
                # Default to Asia/Shanghai (+08:00) if naive, or UTC
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            return None
    return None


# Known seabed task types where operation depth is inherently at seabed
SEABED_TASK_TYPES = {
    "seabed_survey",
    "pipeline_inspection",
    "subsea_pipeline_inspection",
    "wellhead_intervention",
    "riser_base_inspection",
}

# Deterministic robot operational current limits (m/s)
DETERMINISTIC_ROBOT_CURRENT_LIMITS: Dict[str, float] = {
    "通用工作级深海机器人250HP": 1.5,
    "WROV-250-001": 1.5,
    "轻型作业级ROV": 1.0,
    "OBS-100": 0.8,
    "OBS-ROV": 0.8,
    "AUV-DEEP-01": 1.2,
}


def get_deterministic_current_limit(task_state: Dict[str, Any]) -> Optional[float]:
    """Retrieve approved deterministic current limit for robot and task type.

    Fails closed (returns None) if robot or rating is unknown. Never guesses.
    """
    for key in ("equipment_type", "equipment_unit_id", "robot", "robot_model"):
        val = task_state.get(key)
        if val and isinstance(val, str):
            for pattern, limit in DETERMINISTIC_ROBOT_CURRENT_LIMITS.items():
                if pattern.lower() in val.lower():
                    return limit
    return None


def extract_current_query(
    task_state: Dict[str, Any],
    oilfield_kb: Optional[Dict[str, Any]] = None,
    is_task_intent: bool = True,
) -> Optional[CurrentQuery]:
    """Construct CurrentQuery if position, depth, and time bounds are present.

    Strict rules:
    - Never triggers for non-task intent (e.g. ordinary chat).
    - operation_depth and water_depth are strictly separated.
    - water_depth may only derive operation_depth for explicit seabed tasks.
    - Returns None if essential fields are incomplete or invalid.
    """
    if not is_task_intent:
        return None

    start_time = parse_datetime_safe(task_state.get("start_time"))
    end_time = parse_datetime_safe(task_state.get("end_time"))

    if not start_time or not end_time or end_time <= start_time:
        return None

    # Resolve depth: operation_depth vs water_depth
    depth_m = task_state.get("operation_depth")
    if depth_m is None:
        # Only allow deriving from water_depth if explicitly permitted for seabed operations
        task_type = str(task_state.get("task_type_key") or task_state.get("task_type") or "").lower()
        is_seabed = (
            task_state.get("is_seabed_task") is True
            or task_state.get("operation_depth_source") == "derived_from_water_depth"
            or any(s_type in task_type for s_type in SEABED_TASK_TYPES)
        )
        if is_seabed:
            depth_m = task_state.get("water_depth")
            if depth_m is not None:
                task_state["operation_depth_source"] = "derived_from_water_depth"
        else:
            # Mid-water or unspecified task: must continue collecting operation_depth
            return None

    if depth_m is None:
        return None

    try:
        depth_val = float(depth_m)
        if depth_val < 0:
            return None
    except (ValueError, TypeError):
        return None

    # Resolve latitude and longitude
    lat = task_state.get("latitude")
    lon = task_state.get("longitude")

    # If coordinates provided as list/tuple [lat, lon]
    coords = task_state.get("coordinates")
    if coords and isinstance(coords, (list, tuple)) and len(coords) >= 2:
        lat, lon = coords[0], coords[1]

    # Fallback to oilfield knowledge base lookup
    if (lat is None or lon is None) and oilfield_kb:
        oilfield_name = task_state.get("oilfield_name")
        if oilfield_name and isinstance(oilfield_name, str):
            for field in oilfield_kb.get("oil_fields", []):
                aliases = [field.get("name", "")] + field.get("aliases", [])
                if any(alias and alias.lower() in oilfield_name.lower() for alias in aliases):
                    lat_r = field.get("lat_range")
                    lon_r = field.get("lon_range")
                    if lat_r and lon_r:
                        lat = (lat_r[0] + lat_r[1]) / 2.0
                        lon = (lon_r[0] + lon_r[1]) / 2.0
                        break

    if lat is None or lon is None:
        return None

    try:
        lat_val = float(lat)
        lon_val = float(lon)
        return CurrentQuery(
            latitude=lat_val,
            longitude=lon_val,
            operation_depth_m=depth_val,
            start_time=start_time,
            end_time=end_time,
        )
    except Exception as exc:
        logger.debug("Failed to create CurrentQuery from task_state: %s", exc)
        return None


class MarineCurrentBridge:
    """Manages session forecast cache, fingerprint invalidation, and window checking."""

    def __init__(self, default_current_limit_mps: float = 0.5):
        self.default_current_limit_mps = default_current_limit_mps
        self._cached_fingerprint: Optional[str] = None
        self._cached_forecast: Optional[CurrentForecastData] = None

    @property
    def cached_forecast(self) -> Optional[CurrentForecastData]:
        return self._cached_forecast

    def invalidate_cache(self) -> None:
        """Clear cached forecast."""
        self._cached_fingerprint = None
        self._cached_forecast = None

    def update_cache(self, forecast: CurrentForecastData) -> None:
        """Store newly retrieved forecast with its request fingerprint."""
        self._cached_fingerprint = forecast.request_fingerprint
        self._cached_forecast = forecast

    def is_cache_valid_for(self, query: CurrentQuery) -> bool:
        """Verify cached forecast matches current query fingerprint."""
        return (
            self._cached_forecast is not None
            and self._cached_fingerprint == query.fingerprint
        )

    def evaluate_task_window(
        self,
        task_state: Dict[str, Any],
        current_limit_mps: Optional[float] = None,
        allow_synthetic_for_testing: bool = False,
    ) -> Optional[WindowCheckResult]:
        """Evaluate fixed [start_time, end_time] window against cached forecast."""
        if not self._cached_forecast:
            return None

        st = parse_datetime_safe(task_state.get("start_time"))
        et = parse_datetime_safe(task_state.get("end_time"))
        depth = task_state.get("operation_depth") or task_state.get("water_depth")
        if not st or not et or depth is None:
            return None

        limit = current_limit_mps or self.default_current_limit_mps
        return OperationWindowService.check_window(
            forecast=self._cached_forecast,
            operation_depth_m=float(depth),
            start_time=st,
            end_time=et,
            current_limit_mps=limit,
            allow_synthetic_for_testing=allow_synthetic_for_testing,
        )

    def search_task_windows(
        self,
        task_state: Dict[str, Any],
        duration_hours: float,
        search_range_hours: float = 48.0,
        current_limit_mps: Optional[float] = None,
        allow_synthetic_for_testing: bool = False,
    ) -> Optional[WindowSearchResult]:
        """Search available windows for task of specified duration."""
        if not self._cached_forecast:
            return None

        st = parse_datetime_safe(task_state.get("start_time"))
        if not st:
            st = self._cached_forecast.native_time_steps[0]

        et = st + timedelta(hours=search_range_hours)
        depth = task_state.get("operation_depth") or task_state.get("water_depth")
        if depth is None:
            return None

        limit = current_limit_mps or self.default_current_limit_mps
        return OperationWindowService.search_windows(
            forecast=self._cached_forecast,
            operation_depth_m=float(depth),
            search_start=st,
            search_end=et,
            duration_hours=duration_hours,
            current_limit_mps=limit,
            allow_synthetic_for_testing=allow_synthetic_for_testing,
        )


def apply_candidate_window_to_slots(
    slots: Dict[str, Any],
    best_window: Any,
) -> bool:
    """Store recommended window in candidate_value without mutating confirmed value.

    Strictly satisfies Rule 15:
    SEARCH found window -> candidate_value (never directly overwrites value).
    """
    if not hasattr(best_window, "start_time") or not hasattr(best_window, "end_time"):
        return False

    st_iso = best_window.start_time.isoformat()
    et_iso = best_window.end_time.isoformat()

    # If slots are wrapped in Slot objects
    if "start_time" in slots and hasattr(slots["start_time"], "candidate_value"):
        s_slot = slots["start_time"]
        s_slot.candidate_value = st_iso
        s_slot.status = "candidate"
    else:
        slots["start_time_candidate"] = st_iso

    if "end_time" in slots and hasattr(slots["end_time"], "candidate_value"):
        e_slot = slots["end_time"]
        e_slot.candidate_value = et_iso
        e_slot.status = "candidate"
    else:
        slots["end_time_candidate"] = et_iso

    return True


def confirm_candidate_window_in_slots(slots: Dict[str, Any]) -> bool:
    """Promote candidate_value to confirmed value, increment version, and set status to valid.

    Strictly satisfies Rule 15:
    User confirmed -> candidate_value -> formal value -> version + 1 -> clear candidate.
    """
    promoted = False

    for key in ("start_time", "end_time"):
        if key in slots and hasattr(slots[key], "candidate_value"):
            slot = slots[key]
            if slot.candidate_value is not None:
                slot.value = slot.candidate_value
                slot.candidate_value = None
                slot.status = "valid"
                slot.version = getattr(slot, "version", 0) + 1
                promoted = True
        elif f"{key}_candidate" in slots:
            cand = slots.pop(f"{key}_candidate", None)
            if cand is not None:
                slots[key] = cand
                promoted = True

    return promoted
