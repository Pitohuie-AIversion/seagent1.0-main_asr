from datetime import datetime
from src.duration_parser import DurationState
from src.relative_time_parser import TimeFieldState, parse_relative_datetime, parse_time_range


def test_parse_relative_datetime_today_and_tomorrow():
    # 假设基准时间为 2026-08-18 (周二) 10:00:00
    base_dt = datetime(2026, 8, 18, 10, 0, 0)

    # 今天 / 今晚 / 现在 / 今天上午十一点
    assert parse_relative_datetime("今天下午3点", base_dt) == "2026-08-18T15:00:00"
    assert parse_relative_datetime("今晚11点", base_dt) == "2026-08-18T23:00:00"
    assert parse_relative_datetime("今天晚上23:30", base_dt) == "2026-08-18T23:30:00"
    assert parse_relative_datetime("现在", base_dt) == "2026-08-18T10:00:00"
    assert parse_relative_datetime("今天上午十一点", base_dt) == "2026-08-18T11:00:00"
    assert parse_relative_datetime("起始于今天上午十一点 持续三个小时", base_dt) == "2026-08-18T11:00:00"
    assert parse_relative_datetime("今天下午一点一刻", base_dt) == "2026-08-18T13:15:00"
    assert parse_relative_datetime("今天下午四点一刻", base_dt) == "2026-08-18T16:15:00"
    assert parse_relative_datetime("今天下午一点三刻", base_dt) == "2026-08-18T13:45:00"

    # 具体月份与日期 (如 8月31号早上6点 / 八月三十一号)
    assert parse_relative_datetime("8月31号早上6点", base_dt) == "2026-08-31T06:00:00"
    assert parse_relative_datetime("八月三十一号早上6点", base_dt) == "2026-08-31T06:00:00"
    assert parse_relative_datetime("8月31日 14:00", base_dt) == "2026-08-31T14:00:00"
    assert parse_relative_datetime("2026年10月1号上午9点", base_dt) == "2026-10-01T09:00:00"
    assert parse_relative_datetime("十月一日下午2点", base_dt) == "2026-10-01T14:00:00"

    # 明天 / 明晚
    assert parse_relative_datetime("明天下午3点半", base_dt) == "2026-08-19T15:30:00"
    assert parse_relative_datetime("明晚8点", base_dt) == "2026-08-19T20:00:00"
    assert parse_relative_datetime("明天凌晨2点", base_dt) == "2026-08-19T02:00:00"

    # 后天 / 大后天
    assert parse_relative_datetime("后天早上9点", base_dt) == "2026-08-20T09:00:00"
    assert parse_relative_datetime("大后天14:00", base_dt) == "2026-08-21T14:00:00"


def test_parse_relative_datetime_weekday():
    # 基准时间: 2026-08-18 (周二)
    base_dt = datetime(2026, 8, 18, 10, 0, 0)

    # 本周三 / 本周五
    assert parse_relative_datetime("本周三下午4点", base_dt) == "2026-08-19T16:00:00"
    assert parse_relative_datetime("这周五早上8点", base_dt) == "2026-08-21T08:00:00"

    # 下周一 / 下周三
    assert parse_relative_datetime("下周一早上9点", base_dt) == "2026-08-24T09:00:00"
    assert parse_relative_datetime("下周三15:00", base_dt) == "2026-08-26T15:00:00"


def test_parse_relative_datetime_non_relative_returns_none():
    # 绝对 ISO 时间或无相对日期词的文本返回 None
    assert parse_relative_datetime("2026-08-20T10:00:00") is None
    assert parse_relative_datetime("大概三小时") is None


def test_parse_time_range_derives_end_from_start_and_duration():
    base_dt = datetime(2026, 8, 18, 10, 0, 0)

    result = parse_time_range(
        "明天下午3点",
        "两个半小时",
        "未提供",
        base_dt=base_dt,
    )

    assert result.success is True
    assert result.start_time.state == TimeFieldState.EXPLICIT
    assert result.start_time.iso_string == "2026-08-19T15:00:00"
    assert result.duration.state == DurationState.EXPLICIT
    assert result.duration.total_seconds == 9000.0
    assert result.end_time.state == TimeFieldState.DERIVED
    assert result.end_time.iso_string == "2026-08-19T17:30:00"
    assert result.resolution_method == "start_plus_duration"


def test_parse_time_range_derives_duration_from_start_and_end():
    base_dt = datetime(2026, 8, 18, 10, 0, 0)

    result = parse_time_range(
        "8月28日上午10点",
        None,
        "8月28日下午4点",
        base_dt=base_dt,
    )

    assert result.success is True
    assert result.duration.state == DurationState.EXPLICIT
    assert result.duration.total_seconds == 21600.0
    assert result.resolution_method == "end_minus_start"


def test_parse_time_range_keep_duration_uses_previous_window():
    base_dt = datetime(2026, 8, 18, 10, 0, 0)

    result = parse_time_range(
        "后天下午3点",
        "持续时间不变",
        None,
        base_dt=base_dt,
        previous_start=datetime(2026, 8, 18, 10, 0, 0),
        previous_end=datetime(2026, 8, 18, 12, 0, 0),
    )

    assert result.success is True
    assert result.duration.state == DurationState.KEEP
    assert result.duration.total_seconds == 7200.0
    assert result.end_time.iso_string == "2026-08-20T17:00:00"


def test_parse_time_range_increases_previous_duration_and_recomputes_end():
    base_dt = datetime(2026, 8, 26, 10, 0, 0)

    result = parse_time_range(
        None,
        "持续时间增加半小时",
        None,
        base_dt=base_dt,
        previous_start=datetime(2026, 8, 27, 7, 0, 0),
        previous_end=datetime(2026, 8, 27, 10, 15, 0),
    )

    assert result.success is True
    assert result.start_time.iso_string == "2026-08-27T07:00:00"
    assert result.duration.state == DurationState.EXPLICIT
    assert result.duration.total_seconds == 13500.0
    assert result.end_time.iso_string == "2026-08-27T10:45:00"
    assert result.resolution_method == "duration_delta_from_history"


def test_parse_time_range_shifts_previous_start_and_preserves_duration():
    base_dt = datetime(2026, 8, 26, 10, 0, 0)

    result = parse_time_range(
        "开始时间延后半小时",
        None,
        None,
        base_dt=base_dt,
        previous_start=datetime(2026, 8, 27, 7, 0, 0),
        previous_end=datetime(2026, 8, 27, 10, 15, 0),
    )

    assert result.success is True
    assert result.start_time.iso_string == "2026-08-27T07:30:00"
    assert result.duration.total_seconds == 11700.0
    assert result.end_time.iso_string == "2026-08-27T10:45:00"
    assert result.start_time.parse_method == "time_point_delta"


def test_parse_time_range_shifts_previous_end_and_recomputes_duration():
    base_dt = datetime(2026, 8, 26, 10, 0, 0)

    result = parse_time_range(
        None,
        None,
        "结束时间提前15分钟",
        base_dt=base_dt,
        previous_start=datetime(2026, 8, 27, 7, 0, 0),
        previous_end=datetime(2026, 8, 27, 10, 15, 0),
    )

    assert result.success is True
    assert result.start_time.iso_string == "2026-08-27T07:00:00"
    assert result.duration.total_seconds == 10800.0
    assert result.end_time.iso_string == "2026-08-27T10:00:00"
    assert result.end_time.parse_method == "time_point_delta"


def test_parse_time_range_can_preserve_previous_end_as_explicit_end():
    base_dt = datetime(2026, 8, 26, 10, 0, 0)

    result = parse_time_range(
        "明天早上7点",
        None,
        "2026-08-27T12:00:00",
        base_dt=base_dt,
    )

    assert result.success is True
    assert result.end_time.state == TimeFieldState.EXPLICIT
    assert result.end_time.iso_string == "2026-08-27T12:00:00"
    assert result.duration.state == DurationState.EXPLICIT
    assert result.duration.total_seconds == 18000.0
    assert result.resolution_method == "end_minus_start"


def test_parse_time_range_rolls_clock_only_end_over_midnight_from_start_date():
    base_dt = datetime(2026, 8, 18, 10, 0, 0)

    result = parse_time_range(
        "今晚11点",
        None,
        "凌晨2点",
        base_dt=base_dt,
    )

    assert result.success is True
    assert result.end_time.iso_string == "2026-08-19T02:00:00"
    assert result.end_time.parse_method == "range_cross_midnight"
    assert result.duration.total_seconds == 10800.0


def test_parse_time_range_fails_when_all_three_fields_conflict():
    base_dt = datetime(2026, 8, 18, 10, 0, 0)

    result = parse_time_range(
        "明天上午10点",
        "2小时",
        "明天下午1点",
        base_dt=base_dt,
    )

    assert result.success is False
    assert result.error_code == "TIME_RANGE_CONFLICT"
