import pytest
from src.duration_parser import parse_duration_to_seconds, parse_chinese_number


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
