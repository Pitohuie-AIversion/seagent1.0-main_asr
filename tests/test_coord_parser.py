import pytest

from src.extraction.coord_parser import _normalize_coord


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
