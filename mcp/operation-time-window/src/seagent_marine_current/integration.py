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


def extract_current_query(
    task_state: Dict[str, Any],
    oilfield_kb: Optional[Dict[str, Any]] = None,
) -> Optional[CurrentQuery]:
    """Construct CurrentQuery if position, depth, and time bounds are present.

    Returns None if essential fields are incomplete or invalid.
    """
    start_time = parse_datetime_safe(task_state.get("start_time"))
    end_time = parse_datetime_safe(task_state.get("end_time"))

    if not start_time or not end_time or end_time <= start_time:
        return None

    # Resolve depth: prefer operation_depth, fallback to water_depth
    depth_m = task_state.get("operation_depth") or task_state.get("water_depth")
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
        )

    def search_task_windows(
        self,
        task_state: Dict[str, Any],
        duration_hours: float,
        search_range_hours: float = 48.0,
        current_limit_mps: Optional[float] = None,
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
        )
