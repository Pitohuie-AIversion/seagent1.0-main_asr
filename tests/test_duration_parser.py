import pytest
from src.temporal.duration_parser import (
    DurationState,
    parse_duration_spec,
    parse_duration_evidence,
    parse_duration_to_seconds,
    parse_chinese_number,
)


def test_parse_chinese_number():
    assert parse_chinese_number("0") == 0.0
    assert parse_chinese_number("1") == 1.0
    assert parse_chinese_number("2") == 2.0
    assert parse_chinese_number("2.5") == 2.5
    assert parse_chinese_number("两") == 2.0
    assert parse_chinese_number("三") == 3.0
    assert parse_chinese_number("十") == 10.0
    assert parse_chinese_number("十二") == 12.0
    assert parse_chinese_number("二十") == 20.0
    assert parse_chinese_number("二十五") == 25.0
    assert parse_chinese_number("四十五") == 45.0
    assert parse_chinese_number("二点五") == 2.5
    assert parse_chinese_number("半") == 0.5


def test_parse_duration_to_seconds():
    # 两个半小时 / 2个半小时 / 两小时半 / 2.5小时
    assert parse_duration_to_seconds("持续两个半小时") == 9000.0
    assert parse_duration_to_seconds("2个半小时") == 9000.0
    assert parse_duration_to_seconds("两小时半") == 9000.0
    assert parse_duration_to_seconds("2小时半") == 9000.0
    assert parse_duration_to_seconds("2.5小时") == 9000.0
    assert parse_duration_to_seconds("持续2.5h左右") == 9000.0

    # 一个半小时
    assert parse_duration_to_seconds("一个半小时") == 5400.0
    assert parse_duration_to_seconds("1个半小时") == 5400.0
    assert parse_duration_to_seconds("一小时半") == 5400.0
    assert parse_duration_to_seconds("1.5小时") == 5400.0

    # 半小时
    assert parse_duration_to_seconds("半小时") == 1800.0
    assert parse_duration_to_seconds("半个钟头") == 1800.0
    assert parse_duration_to_seconds("0.5小时") == 1800.0
    assert parse_duration_to_seconds("30分钟") == 1800.0
    assert parse_duration_to_seconds("30分") == 1800.0

    # 分钟与秒
    assert parse_duration_to_seconds("45分钟") == 2700.0
    assert parse_duration_to_seconds("3小时") == 10800.0
    assert parse_duration_to_seconds("3个小时") == 10800.0
    assert parse_duration_to_seconds("1小时30分钟") == 5400.0
    assert parse_duration_to_seconds("2小时15分") == 8100.0

    # 天
    assert parse_duration_to_seconds("1天") == 86400.0
    assert parse_duration_to_seconds("一天半") == 129600.0
    assert parse_duration_to_seconds("1.5天") == 129600.0
    assert parse_duration_to_seconds("半天") == 43200.0


@pytest.mark.parametrize("text", [
    "使用观察级深海机器人75HP", "使用75hp机器人", "功率75 Hp",
    "设备型号2hourspec", "设备型号2dayspec", "设备型号2minspec",
    "设备型号2secspec", "型号2个半HP",
    "水深300米，北纬20.1度东经115.2度", "水深300m", "支持船海洋石油681",
])
def test_english_duration_units_do_not_match_equipment_suffixes(text):
    assert parse_duration_to_seconds(text) is None
    assert not parse_duration_evidence(text).success


@pytest.mark.parametrize("text,expected", [
    ("3h", 10800), ("3HR", 10800), ("3hrs", 10800),
    ("3hours", 10800), ("1day2h30min15sec", 95415),
    ("1h30min", 5400), ("1小时30分钟", 5400),
    ("2 hours 30 minutes 15 seconds", 9015), ("持续2h左右", 7200),
    ("使用75HP机器人，持续3小时", 10800),
    ("使用75HP机器人，持续1day", 86400),
    ("持续2个半h", 9000),
])
def test_english_duration_unit_boundaries_keep_supported_durations(text, expected):
    assert parse_duration_to_seconds(text) == expected
    assert parse_duration_evidence(text).total_seconds == expected


@pytest.mark.parametrize("text,expected,days,hours,minutes,seconds", [
    ("一天半2小时", 136800, 1.5, 2, 0, 0),
    ("一个半天两小时", 136800, 1.5, 2, 0, 0),
    ("一天两个半小时", 95400, 1, 2.5, 0, 0),
    ("1个半小时30分钟", 7200, 0, 1.5, 30, 0),
    ("1小时半15分钟30秒", 6330, 0, 1.5, 15, 30),
    ("一天半两个半小时", 138600, 1.5, 2.5, 0, 0),
])
def test_compound_half_duration_keeps_all_units(text, expected, days, hours, minutes, seconds):
    detail = parse_duration_evidence(text)
    assert detail.total_seconds == expected
    assert (detail.days, detail.hours, detail.minutes, detail.seconds) == (days, hours, minutes, seconds)
    assert parse_duration_to_seconds(text) == expected


@pytest.mark.parametrize("text,expected,components", [
    ("半天两小时", 50400, (0.5, 2, 0, 0)),
    ("半小时15分钟", 2700, (0, 0.5, 15, 0)),
    ("半个钟头15分钟", 2700, (0, 0.5, 15, 0)),
    ("半分钟10秒", 40, (0, 0, 0.5, 10)),
    ("一天半小时", 88200, (1, 0.5, 0, 0)),
    ("一天半个小时", 88200, (1, 0.5, 0, 0)),
    ("两小时半分钟", 7230, (0, 2, 0.5, 0)),
    ("两小时半个分钟", 7230, (0, 2, 0.5, 0)),
    ("一天半h", 88200, (1, 0.5, 0, 0)),
    ("两小时半min", 7230, (0, 2, 0.5, 0)),
    ("半天半小时半分钟半秒", 45030.5, (0.5, 0.5, 0.5, 0.5)),
    ("使用75HP机器人，持续半天两小时", 50400, (0.5, 2, 0, 0)),
])
def test_bare_half_duration_components_do_not_overlap(text, expected, components):
    detail = parse_duration_evidence(text)
    assert detail.total_seconds == expected
    assert (detail.days, detail.hours, detail.minutes, detail.seconds) == components
    assert parse_duration_to_seconds(text) == expected
    assert detail.parse_method == "composite_fields"


@pytest.mark.parametrize("text,expected,method", [
    ("半天", 43200, "quick_half_day"),
    ("半小时", 1800, "quick_half_hour"),
    ("半分钟", 30, "quick_half_minute"),
    ("一天半", 129600, "day_and_half_suffix"),
    ("一个半天", 129600, "day_and_half_prefix"),
    ("两小时半", 9000, "hour_and_half_suffix"),
    ("两个半小时", 9000, "hour_and_half_prefix"),
])
def test_half_duration_parse_methods_remain_compatible(text, expected, method):
    detail = parse_duration_evidence(text)
    assert detail.total_seconds == expected
    assert detail.parse_method == method


def test_is_keep_duration_expression():
    from src.temporal.duration_parser import is_keep_duration_expression
    assert is_keep_duration_expression("持续时间不变") is True
    assert is_keep_duration_expression("时长不变") is True
    assert is_keep_duration_expression("保持时长") is True
    assert is_keep_duration_expression("维持原时长") is True
    assert is_keep_duration_expression("按原时长") is True
    assert is_keep_duration_expression("持续5小时") is False
    assert is_keep_duration_expression("任务结束时间不变") is False


def test_parse_duration_spec_classifies_missing_keep_explicit_and_invalid():
    missing = parse_duration_spec("未提供")
    assert missing.state == DurationState.MISSING

    keep = parse_duration_spec("持续时间不变")
    assert keep.state == DurationState.KEEP
    assert keep.parse_method == "keep_duration"

    explicit = parse_duration_spec("两个半小时")
    assert explicit.state == DurationState.EXPLICIT
    assert explicit.total_seconds == 9000.0
    assert explicit.hours == 2.5

    increased = parse_duration_spec("增加半小时")
    assert increased.state == DurationState.DELTA
    assert increased.delta_seconds == 1800.0

    decreased = parse_duration_spec("减少30分钟")
    assert decreased.state == DurationState.DELTA
    assert decreased.delta_seconds == -1800.0

    invalid = parse_duration_spec("一会儿")
    assert invalid.state == DurationState.INVALID
    assert invalid.error_code == "INVALID_DURATION"
