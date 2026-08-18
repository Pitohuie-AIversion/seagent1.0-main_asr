from datetime import datetime
from src.relative_time_parser import parse_relative_datetime


def test_parse_relative_datetime_today_and_tomorrow():
    # 假设基准时间为 2026-08-18 (周二) 10:00:00
    base_dt = datetime(2026, 8, 18, 10, 0, 0)

    # 今天 / 今晚 / 现在
    assert parse_relative_datetime("今天下午3点", base_dt) == "2026-08-18T15:00:00"
    assert parse_relative_datetime("今晚11点", base_dt) == "2026-08-18T23:00:00"
    assert parse_relative_datetime("今天晚上23:30", base_dt) == "2026-08-18T23:30:00"
    assert parse_relative_datetime("现在", base_dt) == "2026-08-18T10:00:00"

    # 具体月份与日期 (如 8月31号早上6点)
    assert parse_relative_datetime("8月31号早上6点", base_dt) == "2026-08-31T06:00:00"
    assert parse_relative_datetime("8月31日 14:00", base_dt) == "2026-08-31T14:00:00"
    assert parse_relative_datetime("2026年10月1号上午9点", base_dt) == "2026-10-01T09:00:00"

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
