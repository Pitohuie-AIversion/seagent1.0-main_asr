"""Coordinate recovery must share the user's actual assignment scope."""
import pytest

from src.extraction.coord_parser import (
    coordinate_update_issues,
    parse_coordinate_updates,
    protected_coordinate_fields,
)


FIELDS = {"start_point", "end_point"}
START = {"lat": 20.0, "lon": 115.0}
END = {"lat": 21.0, "lon": 116.0}


@pytest.mark.parametrize("message", [
    "不要把起点改为北纬20度东经115度",
    "请勿将起点改为北纬20度东经115度",
    "不能把起点改为北纬20度东经115度",
    "起点不要改成北纬20度东经115度，水深350米",
    "如果天气合适，起点北纬20度东经115度",
    "假设起点北纬20度东经115度",
    "只要天气允许，起点北纬20度东经115度",
    "起点北纬20度东经115度可以吗？",
    "起点北纬20度东经115度，行吗？",
    "请讨论起点北纬20度东经115度",
    "起点示例为北纬20度东经115度",
    "昨天起点北纬20度东经115度",
    "保持起点北纬20度东经115度",
    "起点北纬20度东经115度，先不要改",
    "起点北纬20度东经115度，取消这个修改",
    "起点北纬20度东经115度。撤销起点修改。",
])
def test_non_asserted_coordinate_is_not_recovered_or_seeded(message):
    assert parse_coordinate_updates(message, FIELDS) == {}
    assert "start_point" in protected_coordinate_fields(message, FIELDS)
    assert "start_point" not in parse_coordinate_updates(
        message, FIELDS, proposed_updates={"start_point": START},
    )


@pytest.mark.parametrize("message", [
    "起点北纬20度东经115度，起点北纬21度东经116度",
    "起点北纬20度东经115度。起始点北纬21度东经116度。",
    "起点北纬20度东经115度或北纬21度东经116度",
    "起点北纬20度东经115度或者(21,116)",
])
def test_conflicting_coordinates_are_not_arbitrarily_selected(message):
    assert parse_coordinate_updates(message, FIELDS) == {}
    assert protected_coordinate_fields(message, FIELDS) == {"start_point"}


@pytest.mark.parametrize("message", [
    "起点北纬20度东经115度",
    "刚才说错了，起点改为东经115度北纬20度",
    "起始坐标为北纬二十度，东经一百一十五度",
    "不要把终点改为北纬21度东经116度，起点改为北纬20度东经115度",
    "水深不变，起点改为北纬20度东经115度",
    "如果天气不好就停工。起点改为北纬20度东经115度。",
    "起点改为北纬21度东经116度可以吗？起点设为北纬20度东经115度。",
    "不要把起点改为北纬21度东经116度，起点改为北纬20度东经115度",
    "起点北纬20度东经115度，起始点北纬20度东经115度",
    "起点北纬20度东经115度，水深350米可以吗？",
])
def test_independent_affirmative_coordinate_survives(message):
    assert parse_coordinate_updates(message, FIELDS) == {"start_point": START}
    assert "start_point" not in protected_coordinate_fields(message, FIELDS)


@pytest.mark.parametrize("message", [
    "起点北纬20度东经115度，终点北纬21度东经116度",
    "终点东经116度北纬21度，起点东经115度北纬20度",
    "起点20,115，终点21,116",
    "20,115 21,116",
    "北纬20度东经115度，(21,116)",
])
def test_start_and_end_keep_explicit_or_positional_order(message):
    assert parse_coordinate_updates(message, FIELDS) == {
        "start_point": START, "end_point": END,
    }


def test_ambiguous_bare_pairs_for_one_remaining_field_are_rejected():
    current = {"end_point": END}
    message = "北纬20度东经115度，北纬21度东经116度"
    assert parse_coordinate_updates(message, FIELDS, current_state=current) == {}
    assert protected_coordinate_fields(message, FIELDS, current) == {"start_point"}


def test_bare_pair_fills_only_the_remaining_field():
    assert parse_coordinate_updates(
        "21,116", FIELDS, current_state={"start_point": START},
    ) == {"end_point": END}


def test_schema_scope_and_non_coordinate_inputs_are_preserved():
    assert parse_coordinate_updates("起点北纬20度东经115度", set()) == {}
    assert protected_coordinate_fields("水深不变", FIELDS) == set()
    assert parse_coordinate_updates("2026-08-20 15:00", FIELDS) == {}


@pytest.mark.parametrize("message", [
    "不要改坐标，北纬20度东经115度",
    "如果坐标是北纬20度东经115度会怎样？",
    "北纬20度东经115度？",
])
def test_non_asserted_bare_coordinates_are_protected(message):
    assert parse_coordinate_updates(message, FIELDS) == {}
    assert protected_coordinate_fields(message, FIELDS) == FIELDS


@pytest.mark.parametrize("message,fragment", [
    ("起点北纬91度东经115度", "超出合法范围"),
    ("起点北纬20度东经115度或者(91,115)", "超出合法范围"),
    ("91,115", "超出合法范围"),
    ("起点20,115，起点21,116", "多个不同坐标"),
    ("20,115 21,116 22,117", "数量多于"),
])
def test_rejected_assertions_explain_invalid_values_and_ambiguity(message, fragment):
    issues = coordinate_update_issues(message, FIELDS)
    assert any(fragment in issue for issue in issues)
    assert parse_coordinate_updates(message, FIELDS) == {}


@pytest.mark.parametrize("message", [
    "起点北纬20度东经115度可以吗？",
    "不要修改起点",
    "如果天气合适，起点北纬20度东经115度",
    "起点北纬20度东经115度",
])
def test_scope_protection_and_valid_values_need_no_extra_issue(message):
    assert coordinate_update_issues(message, FIELDS) == []
