"""SEAgent local operation window evaluation: CHECK and SEARCH algorithms."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import List, Literal, Optional

from .contracts import CurrentForecastData
from .processing import CurrentProcessor

WindowStatus = Literal["AVAILABLE", "UNAVAILABLE", "NOT_EVALUABLE"]


@dataclass(frozen=True)
class CandidateWindow:
    """Evaluated single time window candidate."""

    start_time: datetime
    end_time: datetime
    duration_hours: float
    v_max_mps: float
    current_limit_mps: float
    is_safe: bool


@dataclass(frozen=True)
class WindowCheckResult:
    """Outcome of CHECK operation over fixed user interval."""

    status: WindowStatus
    start_time: Optional[datetime]
    end_time: Optional[datetime]
    duration_hours: Optional[float]
    v_max_mps: Optional[float]
    current_limit_mps: float
    basis: str = "interpolated_model_current_only"
    execution_authorized: bool = False
    reason_code: Optional[str] = None
    message: str = ""


@dataclass(frozen=True)
class WindowSearchResult:
    """Outcome of SEARCH operation across an exploration range."""

    status: WindowStatus
    search_start: Optional[datetime]
    search_end: Optional[datetime]
    duration_hours: float
    current_limit_mps: float
    candidate_step_seconds: float
    total_checked_candidates: int
    available_windows: List[CandidateWindow] = field(default_factory=list)
    basis: str = "interpolated_model_current_only"
    execution_authorized: bool = False
    reason_code: Optional[str] = None
    message: str = ""


class OperationWindowService:
    """SEAgent-side business service to evaluate and search operational time windows."""

    @staticmethod
    def _is_covered(forecast: CurrentForecastData, start_time: datetime, end_time: datetime) -> bool:
        """Verify query time range is fully covered by forecast native steps."""
        if not forecast.native_time_steps or len(forecast.native_time_steps) < 2:
            return False
        f_start = forecast.native_time_steps[0].astimezone(timezone.utc)
        f_end = forecast.native_time_steps[-1].astimezone(timezone.utc)
        s_utc = start_time.astimezone(timezone.utc)
        e_utc = end_time.astimezone(timezone.utc)
        return f_start <= s_utc and e_utc <= f_end

    @classmethod
    def check_window(
        cls,
        forecast: CurrentForecastData,
        operation_depth_m: float,
        start_time: datetime,
        end_time: datetime,
        current_limit_mps: float,
    ) -> WindowCheckResult:
        """CHECK: Evaluates whether fixed interval [start_time, end_time] satisfies current_limit.

        Does not shift, search, or mutate user task intent.
        """
        if end_time <= start_time:
            return WindowCheckResult(
                status="NOT_EVALUABLE",
                start_time=start_time,
                end_time=end_time,
                duration_hours=None,
                v_max_mps=None,
                current_limit_mps=current_limit_mps,
                reason_code="INVALID_INTERVAL",
                message=f"end_time ({end_time.isoformat()}) must be after start_time ({start_time.isoformat()})",
            )

        if not cls._is_covered(forecast, start_time, end_time):
            f_s = forecast.native_time_steps[0].isoformat()
            f_e = forecast.native_time_steps[-1].isoformat()
            return WindowCheckResult(
                status="NOT_EVALUABLE",
                start_time=start_time,
                end_time=end_time,
                duration_hours=round((end_time - start_time).total_seconds() / 3600.0, 2),
                v_max_mps=None,
                current_limit_mps=current_limit_mps,
                reason_code="TIME_RANGE_NOT_COVERED",
                message=f"Target interval [{start_time.isoformat()}, {end_time.isoformat()}] exceeds available forecast [{f_s}, {f_e}]",
            )

        duration_hours = (end_time - start_time).total_seconds() / 3600.0
        v_max, _ = CurrentProcessor.find_interval_max_speed(forecast, operation_depth_m, start_time, end_time)

        if v_max <= current_limit_mps:
            return WindowCheckResult(
                status="AVAILABLE",
                start_time=start_time,
                end_time=end_time,
                duration_hours=round(duration_hours, 2),
                v_max_mps=round(v_max, 4),
                current_limit_mps=current_limit_mps,
                reason_code=None,
                message=f"Window condition satisfied: Vmax ({v_max:.2f} m/s) <= limit ({current_limit_mps:.2f} m/s)",
            )
        else:
            return WindowCheckResult(
                status="UNAVAILABLE",
                start_time=start_time,
                end_time=end_time,
                duration_hours=round(duration_hours, 2),
                v_max_mps=round(v_max, 4),
                current_limit_mps=current_limit_mps,
                reason_code="CURRENT_EXCEEDED",
                message=f"Window condition violated: Vmax ({v_max:.2f} m/s) > limit ({current_limit_mps:.2f} m/s)",
            )

    @classmethod
    def search_windows(
        cls,
        forecast: CurrentForecastData,
        operation_depth_m: float,
        search_start: datetime,
        search_end: datetime,
        duration_hours: float,
        current_limit_mps: float,
        candidate_step_seconds: float = 3600.0,
    ) -> WindowSearchResult:
        """SEARCH: Explores candidates of length duration_hours in [search_start, search_end].

        Generates candidate start times progressing by candidate_step_seconds (default 1h),
        plus the boundary start time (search_end - duration).
        Sorts matches by start_time ascending, then v_max ascending.
        """
        if duration_hours <= 0:
            return WindowSearchResult(
                status="NOT_EVALUABLE",
                search_start=search_start,
                search_end=search_end,
                duration_hours=duration_hours,
                current_limit_mps=current_limit_mps,
                candidate_step_seconds=candidate_step_seconds,
                total_checked_candidates=0,
                reason_code="INVALID_DURATION",
                message=f"duration_hours ({duration_hours}) must be positive",
            )

        duration_td = timedelta(hours=duration_hours)
        if search_end - search_start < duration_td:
            return WindowSearchResult(
                status="NOT_EVALUABLE",
                search_start=search_start,
                search_end=search_end,
                duration_hours=duration_hours,
                current_limit_mps=current_limit_mps,
                candidate_step_seconds=candidate_step_seconds,
                total_checked_candidates=0,
                reason_code="SEARCH_RANGE_TOO_SHORT",
                message=f"Search range ({(search_end - search_start).total_seconds()/3600.0:.1f}h) is shorter than task duration ({duration_hours:.1f}h)",
            )

        if not cls._is_covered(forecast, search_start, search_end):
            f_s = forecast.native_time_steps[0].isoformat()
            f_e = forecast.native_time_steps[-1].isoformat()
            return WindowSearchResult(
                status="NOT_EVALUABLE",
                search_start=search_start,
                search_end=search_end,
                duration_hours=duration_hours,
                current_limit_mps=current_limit_mps,
                candidate_step_seconds=candidate_step_seconds,
                total_checked_candidates=0,
                reason_code="TIME_RANGE_NOT_COVERED",
                message=f"Search range [{search_start.isoformat()}, {search_end.isoformat()}] exceeds available forecast [{f_s}, {f_e}]",
            )

        step_td = timedelta(seconds=candidate_step_seconds)
        curr_start = search_start.astimezone(timezone.utc)
        end_bound = search_end.astimezone(timezone.utc)

        candidate_starts = []
        while curr_start + duration_td <= end_bound:
            candidate_starts.append(curr_start)
            curr_start += step_td

        # Ensure last boundary candidate (search_end - duration) is included if not already present
        last_possible = end_bound - duration_td
        if candidate_starts and candidate_starts[-1] < last_possible:
            candidate_starts.append(last_possible)

        available_candidates: List[CandidateWindow] = []
        total_checked = len(candidate_starts)

        for c_start in candidate_starts:
            c_end = c_start + duration_td
            v_max, _ = CurrentProcessor.find_interval_max_speed(
                forecast, operation_depth_m, c_start, c_end
            )
            is_safe = (v_max <= current_limit_mps)
            candidate = CandidateWindow(
                start_time=c_start,
                end_time=c_end,
                duration_hours=round(duration_hours, 2),
                v_max_mps=round(v_max, 4),
                current_limit_mps=current_limit_mps,
                is_safe=is_safe,
            )
            if is_safe:
                available_candidates.append(candidate)

        # Sort fixed: start_time ascending, then v_max ascending
        available_candidates.sort(key=lambda c: (c.start_time, c.v_max_mps))

        if available_candidates:
            return WindowSearchResult(
                status="AVAILABLE",
                search_start=search_start,
                search_end=search_end,
                duration_hours=duration_hours,
                current_limit_mps=current_limit_mps,
                candidate_step_seconds=candidate_step_seconds,
                total_checked_candidates=total_checked,
                available_windows=available_candidates,
                reason_code=None,
                message=f"Found {len(available_candidates)} suitable operation windows out of {total_checked} evaluated.",
            )
        else:
            return WindowSearchResult(
                status="UNAVAILABLE",
                search_start=search_start,
                search_end=search_end,
                duration_hours=duration_hours,
                current_limit_mps=current_limit_mps,
                candidate_step_seconds=candidate_step_seconds,
                total_checked_candidates=total_checked,
                available_windows=[],
                reason_code="NO_SUITABLE_WINDOW",
                message=f"No suitable window found: All {total_checked} evaluated candidates exceeded current limit ({current_limit_mps:.2f} m/s).",
            )
