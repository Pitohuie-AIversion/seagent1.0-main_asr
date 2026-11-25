import pytest

from src.extraction.coord_parser import (
    _in_coord_range,
    _normalize_coord,
    _parse_chinese_number,
)


@pytest.mark.parametrize(
    "lat, lon, expected",
    [
        # Valid coordinates (Happy path)
        (0.0, 0.0, True),          # Equator / Prime Meridian
        (45.0, 90.0, True),        # Middle values
        (90.0, 180.0, True),       # Maximum boundaries
        (-90.0, -180.0, True),     # Minimum boundaries
        (90.0, -180.0, True),      # Mixed boundaries
        (-90.0, 180.0, True),      # Mixed boundaries

        # Invalid latitude
        (90.1, 0.0, False),        # Just above max lat
        (-90.1, 0.0, False),       # Just below min lat
        (100.0, 0.0, False),       # Way out lat
        (-100.0, 0.0, False),      # Way out lat

        # Invalid longitude
        (0.0, 180.1, False),       # Just above max lon
        (0.0, -180.1, False),      # Just below min lon
        (0.0, 200.0, False),       # Way out lon
        (0.0, -200.0, False),      # Way out lon

        # Both invalid
        (91.0, 181.0, False),      # Both just above max
        (-91.0, -181.0, False),    # Both just below min
        (100.0, 200.0, False),     # Both way out
    ],
)
def test_in_coord_range(lat, lon, expected):
    """Ensure _in_coord_range correctly validates lat/lon bounds."""
    assert _in_coord_range(lat, lon) is expected


@pytest.mark.parametrize(
    "text, expected",
    [
        ("零", 0.0),
        ("一", 1.0),
        ("二", 2.0),
        ("三", 3.0),
        ("十", 10.0),
        ("十二", 12.0),
        ("二十", 20.0),
        ("二十五", 25.0),
        ("四十五", 45.0),
        ("二点五", 2.5),
        ("负十", -10.0),
        ("负十点五", -10.5),
        ("二十五点三", 25.3),
        ("一百", 100.0),
        ("一百二十点五", 120.5),
        ("点", 0.0),
        ("", None),
        ("负", None),
        ("三点一四", 3.14),
        ("十五点零", 15.0),
        ("未知", None),
        ("一点未知", None),
    ],
)
def test_parse_chinese_number(text, expected):
    assert _parse_chinese_number(text) == expected


def test_normalize_coord_valid():
    assert _normalize_coord(10, 20) == {"lat": 10.0, "lon": 20.0}
    assert _normalize_coord(10.5, "20.5") == {"lat": 10.5, "lon": 20.5}
    assert _normalize_coord("三十点五", "五十点二") == {"lat": 30.5, "lon": 50.2}
    assert _normalize_coord("-90", "-180") == {"lat": -90.0, "lon": -180.0}
    assert _normalize_coord(90, 180) == {"lat": 90.0, "lon": 180.0}


def test_normalize_coord_invalid_parsing():
    assert _normalize_coord(None, 20) is None
    assert _normalize_coord(10, None) is None
    assert _normalize_coord(10, "") is None
    assert _normalize_coord("", 20) is None
    assert _normalize_coord("abc", 20) is None
    assert _normalize_coord(10, "xyz") is None
    assert _normalize_coord(10, object()) is None
    assert _normalize_coord(object(), 20) is None


def test_normalize_coord_out_of_bounds():
    assert _normalize_coord(90.1, 20) is None
    assert _normalize_coord(91, 20) is None
    assert _normalize_coord(-90.1, 20) is None
    assert _normalize_coord(-91, 20) is None
    assert _normalize_coord(10, 180.1) is None
    assert _normalize_coord(10, 181) is None
    assert _normalize_coord(10, -180.1) is None
    assert _normalize_coord(10, -181) is None
