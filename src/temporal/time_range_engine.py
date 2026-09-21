"""
src/temporal/time_range_engine.py — 时间区间业务模型与三元推导互算引擎

负责处理任务作业时间的三要素（开始时间 start_time、持续时间 duration、结束时间 end_time）之间的：
1. 确定性计算与互算（S + D -> E, E - S -> D, S + D == E 一致性校验）；
2. 缺失字段填补与历史状态继承（DurationState.KEEP / DURATION_DELTA）；
3. 跨午夜与跨日期的自动时序推导；
4. 严格的时序有效性断言（E > S）。
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import Enum
from typing import Any, Optional

from .duration_parser import (
    DurationSpec,
    DurationState,
    format_duration_template,
    parse_duration_spec,
)
from .relative_time_parser import (
    AmbiguityCode,
    AmbiguityInfo,
    TemporalIR,
    parse_relative_datetime_detail,
)


# ============================================================================
# 时间区间业务模型：开始时间 / 持续时间 / 结束时间
# ============================================================================

class TimeFieldState(str, Enum):
    MISSING = "missing"
    EXPLICIT = "explicit"
    DERIVED = "derived"
    KEEP = "keep"
    INVALID = "invalid"


@dataclass
class TimePointSpec:
    state: TimeFieldState = TimeFieldState.MISSING
    raw_text: str = ""
    value: Optional[datetime] = None
    iso_string: Optional[str] = None
    parse_method: Optional[str] = None
    ambiguities: list[AmbiguityInfo] = field(default_factory=list)
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    has_explicit_date: bool = False
    has_explicit_time: bool = False


@dataclass
class TimeRangeParseResult:
    start_time: TimePointSpec = field(default_factory=TimePointSpec)
    duration: DurationSpec = field(default_factory=DurationSpec)
    end_time: TimePointSpec = field(default_factory=TimePointSpec)
    success: bool = False
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    resolution_method: Optional[str] = None
    warnings: list[str] = field(default_factory=list)
    error_detail: dict[str, Any] = field(default_factory=dict)

    @property
    def start_template(self) -> Optional[str]:
        return format_datetime_template(self.start_time.value) if self.start_time.value else None

    @property
    def duration_template(self) -> Optional[str]:
        if self.duration.total_seconds is None:
            return None
        return format_duration_template(self.duration.total_seconds)

    @property
    def end_template(self) -> Optional[str]:
        return format_datetime_template(self.end_time.value) if self.end_time.value else None


_MISSING_TIME_TEXTS = {"未提供", "无", "null", "none"}
_FATAL_TIME_AMBIGUITIES = {
    AmbiguityCode.MERIDIEM_UNSPECIFIED,
    AmbiguityCode.DATE_WEEKDAY_CONFLICT,
    AmbiguityCode.LEAP_YEAR_EXPECTED,
    AmbiguityCode.DAY_OUT_OF_RANGE_FOR_MONTH,
    AmbiguityCode.DST_GAP_NONEXISTENT,
    AmbiguityCode.DST_FOLD_AMBIGUOUS,
}


def _normalize_missing_text(text: Optional[str]) -> str:
    if text is None or not isinstance(text, str):
        return ""
    return unicodedata.normalize("NFKC", text).strip()


def _parse_iso_datetime(text: str) -> Optional[datetime]:
    raw = text.strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _isoformat_seconds(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _duration_components(total_seconds: float) -> tuple[int, int, int, int]:
    whole_seconds = int(round(total_seconds))
    days, rem = divmod(whole_seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    return days, hours, minutes, seconds


def _duration_from_seconds(
    total_seconds: float,
    *,
    state: DurationState = DurationState.EXPLICIT,
    parse_method: str = "derived_duration",
) -> DurationSpec:
    days, hours, minutes, seconds = _duration_components(total_seconds)
    return DurationSpec(
        state=state,
        raw_text="",
        normalized_text="",
        total_seconds=float(total_seconds),
        days=float(days),
        hours=float(hours),
        minutes=float(minutes),
        seconds=float(seconds),
        parse_method=parse_method,
    )


def _point_has_explicit_date(ira: TemporalIR) -> bool:
    return any(
        [
            ira.year is not None,
            ira.month is not None,
            ira.day is not None,
            ira.weekday is not None,
            ira.week_anchor is not None,
            ira.day_offset is not None,
            ira.week_offset is not None,
            ira.month_offset is not None,
            ira.boundary is not None,
            ira.is_now,
            ira.hour_offset is not None,
            ira.minute_offset is not None,
        ]
    )


def _point_has_explicit_time(ira: TemporalIR) -> bool:
    return any(
        [
            ira.hour is not None,
            ira.hour_offset is not None,
            ira.minute_offset is not None,
            ira.is_now,
        ]
    )


def _has_fatal_ambiguity(ambiguities: list[AmbiguityInfo]) -> bool:
    return any(item.code in _FATAL_TIME_AMBIGUITIES for item in ambiguities)


def _parse_time_point_spec(
    text: Optional[str],
    base_dt: datetime,
    timezone_id: str,
    *,
    date_context: Optional[date] = None,
) -> TimePointSpec:
    """解析单个业务时间槽位。字段已由上游隔离，因此不接收 full_user_message。"""
    raw = _normalize_missing_text(text)
    if not raw or raw.lower() in _MISSING_TIME_TEXTS:
        return TimePointSpec(state=TimeFieldState.MISSING, raw_text=raw)

    iso_dt = _parse_iso_datetime(raw)
    if iso_dt is not None:
        return TimePointSpec(
            state=TimeFieldState.EXPLICIT,
            raw_text=raw,
            value=iso_dt,
            iso_string=_isoformat_seconds(iso_dt),
            parse_method="absolute_iso",
            has_explicit_date=True,
            has_explicit_time=True,
        )

    detail = parse_relative_datetime_detail(
        text=raw,
        base_dt=base_dt,
        full_user_message=None,
        timezone_id=timezone_id,
    )
    # Only a clock-only endpoint inherits the start date. A relative date such
    # as "tomorrow" is already anchored to the current date, not the start date.
    if date_context is not None and not _point_has_explicit_date(detail.ir):
        point_base = datetime.combine(date_context, base_dt.timetz().replace(tzinfo=None))
        detail = parse_relative_datetime_detail(
            text=raw, base_dt=point_base, full_user_message=None,
            timezone_id=timezone_id,
        )

    if not detail.success or _has_fatal_ambiguity(detail.ambiguities):
        return TimePointSpec(
            state=TimeFieldState.INVALID,
            raw_text=raw,
            parse_method=detail.ir.resolution_method,
            ambiguities=detail.ambiguities,
            error_code="INVALID_TIME_POINT",
            error_message="无法解析时间点",
            has_explicit_date=_point_has_explicit_date(detail.ir),
            has_explicit_time=_point_has_explicit_time(detail.ir),
        )

    return TimePointSpec(
        state=TimeFieldState.EXPLICIT,
        raw_text=raw,
        value=detail.target_local_datetime,
        iso_string=detail.iso_string,
        parse_method=detail.ir.resolution_method,
        ambiguities=detail.ambiguities,
        has_explicit_date=_point_has_explicit_date(detail.ir),
        has_explicit_time=_point_has_explicit_time(detail.ir),
    )


def _derived_time_point(dt: datetime, parse_method: str) -> TimePointSpec:
    return TimePointSpec(
        state=TimeFieldState.DERIVED,
        raw_text="",
        value=dt,
        iso_string=_isoformat_seconds(dt),
        parse_method=parse_method,
        has_explicit_date=True,
        has_explicit_time=True,
    )


def _history_time_point(dt: datetime, parse_method: str = "history_time") -> TimePointSpec:
    return TimePointSpec(
        state=TimeFieldState.EXPLICIT,
        raw_text="",
        value=dt,
        iso_string=_isoformat_seconds(dt),
        parse_method=parse_method,
        has_explicit_date=True,
        has_explicit_time=True,
    )


def _fail_time_range(
    result: TimeRangeParseResult,
    code: str,
    message: str,
    **detail: Any,
) -> TimeRangeParseResult:
    result.success = False
    result.error_code = code
    result.error_message = message
    result.error_detail = dict(detail)
    return result


def _resolve_previous_duration(
    previous_start: Optional[datetime],
    previous_end: Optional[datetime],
    previous_duration_seconds: Optional[float],
) -> Optional[float]:
    if previous_duration_seconds is not None:
        try:
            seconds = float(previous_duration_seconds)
        except (TypeError, ValueError):
            return None
        return seconds if seconds > 0 else None

    if previous_start is None or previous_end is None:
        return None
    seconds = (previous_end - previous_start).total_seconds()
    return seconds if seconds > 0 else None


def format_datetime_template(dt: datetime) -> str:
    """按协议模板输出规范化时间点，如 2026年08月27日15时30分。"""
    return dt.strftime("%Y年%m月%d日%H时%M分")


def parse_time_range(
    start_text: Optional[str],
    duration_text: Optional[str],
    end_text: Optional[str],
    *,
    base_dt: Optional[datetime] = None,
    previous_start: Optional[datetime] = None,
    previous_end: Optional[datetime] = None,
    previous_duration_seconds: Optional[float] = None,
    timezone_id: str = "Asia/Shanghai",
) -> TimeRangeParseResult:
    """统一时间区间入口：LLM 只给三个槽位，Python 负责确定性计算 S/D/E。"""
    if base_dt is None:
        from .simulated_time import get_current_datetime
        base_dt = get_current_datetime()
    base_naive = base_dt.replace(tzinfo=None, microsecond=0)

    result = TimeRangeParseResult()
    if not _normalize_missing_text(start_text) and previous_start is not None:
        start = _history_time_point(previous_start)
    else:
        start = _parse_time_point_spec(start_text, base_naive, timezone_id)

    duration = parse_duration_spec(duration_text)
    result.start_time = start
    result.duration = duration

    if start.state == TimeFieldState.INVALID:
        return _fail_time_range(result, "INVALID_START_TIME", "开始时间无法解析")
    if duration.state == DurationState.INVALID:
        return _fail_time_range(result, "INVALID_DURATION", "持续时间无法解析")
    if duration.state == DurationState.DELTA and previous_start is None and start.state == TimeFieldState.MISSING:
        return _fail_time_range(
            result,
            "DURATION_DELTA_WITHOUT_START",
            "用户要求调整持续时间，但没有可用于计算结束时间的开始时间",
        )
    if start.state == TimeFieldState.MISSING:
        end = _parse_time_point_spec(end_text, base_naive, timezone_id)
        result.end_time = end
        return _fail_time_range(result, "START_TIME_REQUIRED", "必须提供开始时间")

    assert start.value is not None
    end = _parse_time_point_spec(
        end_text,
        base_naive,
        timezone_id,
        date_context=start.value.date(),
    )
    result.end_time = end
    if end.state == TimeFieldState.INVALID:
        return _fail_time_range(result, "INVALID_END_TIME", "结束时间无法解析")

    duration_seconds: Optional[float] = None
    if duration.state == DurationState.EXPLICIT:
        duration_seconds = duration.total_seconds
    elif duration.state == DurationState.DELTA:
        previous_duration = _resolve_previous_duration(
            previous_start,
            previous_end,
            previous_duration_seconds,
        )
        if previous_duration is None or duration.delta_seconds is None:
            return _fail_time_range(
                result,
                "DURATION_DELTA_WITHOUT_HISTORY",
                "用户要求调整持续时间，但没有可继承的历史持续时间",
            )
        duration_seconds = previous_duration + duration.delta_seconds
        if duration_seconds <= 0:
            return _fail_time_range(
                result,
                "NON_POSITIVE_DURATION",
                "调整后的持续时间必须为正数",
            )
        result.duration = _duration_from_seconds(
            duration_seconds,
            state=DurationState.EXPLICIT,
            parse_method="duration_delta",
        )
        result.duration.raw_text = duration.raw_text
    elif duration.state == DurationState.KEEP:
        duration_seconds = _resolve_previous_duration(
            previous_start,
            previous_end,
            previous_duration_seconds,
        )
        if duration_seconds is None:
            return _fail_time_range(
                result,
                "KEEP_DURATION_WITHOUT_HISTORY",
                "用户要求持续时间不变，但没有可继承的历史持续时间",
            )
        result.duration = _duration_from_seconds(
            duration_seconds,
            state=DurationState.KEEP,
            parse_method=duration.parse_method or "keep_duration",
        )
        result.duration.raw_text = duration.raw_text
    elif duration.state == DurationState.MISSING and previous_start and previous_end:
        duration_seconds = (previous_end - previous_start).total_seconds()
        result.duration = _duration_from_seconds(
            duration_seconds,
            state=DurationState.EXPLICIT,
            parse_method="inherit_previous_duration",
        )

    start_dt = start.value
    end_dt = end.value

    if (
        end.state == TimeFieldState.EXPLICIT
        and end_dt is not None
        and end_dt <= start_dt
        and not end.has_explicit_date
    ):
        end_dt = end_dt + timedelta(days=1)
        result.end_time = TimePointSpec(
            state=TimeFieldState.EXPLICIT,
            raw_text=end.raw_text,
            value=end_dt,
            iso_string=_isoformat_seconds(end_dt),
            parse_method="range_cross_midnight",
            ambiguities=end.ambiguities,
            has_explicit_date=end.has_explicit_date,
            has_explicit_time=end.has_explicit_time,
        )

    if result.end_time.state == TimeFieldState.MISSING and duration_seconds is not None:
        derived_end = start_dt + timedelta(seconds=duration_seconds)
        result.end_time = _derived_time_point(derived_end, "duration_arithmetic")
        result.resolution_method = (
            "duration_delta_from_history"
            if result.duration.parse_method == "duration_delta"
            else "start_plus_duration"
        )
    elif result.end_time.state == TimeFieldState.EXPLICIT and duration.state == DurationState.MISSING:
        assert result.end_time.value is not None
        duration_seconds = (result.end_time.value - start_dt).total_seconds()
        if duration_seconds <= 0:
            return _fail_time_range(result, "END_NOT_AFTER_START", "结束时间必须晚于开始时间")
        result.duration = _duration_from_seconds(duration_seconds)
        result.resolution_method = "end_minus_start"
    elif result.end_time.state == TimeFieldState.EXPLICIT and duration_seconds is not None:
        assert result.end_time.value is not None
        calculated_end = start_dt + timedelta(seconds=duration_seconds)
        if calculated_end != result.end_time.value:
            return _fail_time_range(
                result,
                "TIME_RANGE_CONFLICT",
                "开始时间、持续时间和结束时间不一致",
                start=start_dt.isoformat(timespec="seconds"),
                duration_seconds=duration_seconds,
                explicit_end=result.end_time.value.isoformat(timespec="seconds"),
                calculated_end=calculated_end.isoformat(timespec="seconds"),
            )
        result.resolution_method = "explicit_all_verified"
    else:
        return _fail_time_range(
            result,
            "INCOMPLETE_TIME_RANGE",
            "开始时间之后必须提供持续时间或结束时间",
        )

    if result.end_time.value is None or result.end_time.value <= result.start_time.value:
        return _fail_time_range(result, "END_NOT_AFTER_START", "结束时间必须晚于开始时间")

    result.success = True
    return result
