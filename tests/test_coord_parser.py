import pytest
from src.extraction.coord_parser import _parse_chinese_number

@pytest.mark.parametrize("text,expected", [
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
])
def test_parse_chinese_number(text, expected):
    assert _parse_chinese_number(text) == expected
