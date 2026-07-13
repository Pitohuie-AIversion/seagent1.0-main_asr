"""
Coordinate parsing helpers for user supplied task text.

The LLM extractor is still the primary parser. This module is a deterministic
fallback for common coordinate formats produced by typed input or ASR, such as
"19.8，113.5" without parentheses.
"""

# 专做规则校验

from __future__ import annotations

import re
from typing import Any


COORD_FIELDS = {
    "cable_position",
    "start_point",
    "end_point",
    "oilfield_coordinates",
}

FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "cable_position": ("管缆位置", "管线位置", "管道位置", "电缆位置", "光缆位置"),
    "start_point": ("起始点", "开始点", "起点", "起始坐标", "开始坐标", "起始位置", "起始端"),
    "end_point": ("结束点", "终止点", "终点", "结束坐标", "终止坐标", "结束位置", "终止端"),
    "oilfield_coordinates": ("油田经纬度坐标", "油田经纬度", "油田坐标", "油田位置"),
}

_ARABIC_NUM = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)"
_CHINESE_NUM = r"[负零〇一二两三四五六七八九十百千万点]+"
_VALUE = rf"(?:{_ARABIC_NUM}|{_CHINESE_NUM})"
_SEP = r"(?:\s*[,，、/]\s*|\s*逗号\s*|\s+)"
_PAIR_RE = re.compile(rf"[（(]?\s*({_VALUE}){_SEP}({_VALUE})\s*[）)]?")
_LAT_LON_RE = re.compile(
    rf"(?:纬度|北纬|lat(?:itude)?)\s*[:：=为是]?\s*({_VALUE})\s*度?"
    rf".{{0,30}}?"
    rf"(?:经度|东经|lon(?:gitude)?)\s*[:：=为是]?\s*({_VALUE})",
    re.IGNORECASE,
)
_LON_LAT_RE = re.compile(
    rf"(?:经度|东经|lon(?:gitude)?)\s*[:：=为是]?\s*({_VALUE})\s*度?"
    rf".{{0,30}}?"
    rf"(?:纬度|北纬|lat(?:itude)?)\s*[:：=为是]?\s*({_VALUE})",
    re.IGNORECASE,
)

_CN_DIGITS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000}


def is_coord_value(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    try:
        lat = float(value.get("lat"))
        lon = float(value.get("lon"))
    except (TypeError, ValueError):
        return False
    return _in_coord_range(lat, lon)


def parse_coord_value(value: Any) -> dict[str, float] | None:
    """Parse one coordinate value from a dict or a short string."""
    if isinstance(value, dict):
        return _normalize_coord(value.get("lat"), value.get("lon"))
    if not isinstance(value, str):
        return None
    return _extract_first_coord(value)


def parse_coordinate_updates(
    text: str,
    candidate_fields: set[str],
    current_state: dict[str, Any] | None = None,
    proposed_updates: dict[str, Any] | None = None,
) -> dict[str, dict[str, float]]:
    """
    Extract coordinate updates from the latest user text.

    Anchored phrases like "起始点19.8，113.5" are preferred. If there is no
    anchor and exactly one coordinate field is currently needed, a bare pair is
    assigned to that field.
    """
    fields = set(candidate_fields) & COORD_FIELDS
    if not text or not fields:
        return {}

    current_state = current_state or {}
    proposed_updates = proposed_updates or {}
    results: dict[str, dict[str, float]] = {}

    for field in fields:
        parsed = parse_coord_value(proposed_updates.get(field))
        if parsed:
            results[field] = parsed

    anchors = _find_anchors(text, fields)
    for field, start, end in anchors:
        if field in results and is_coord_value(results[field]):
            continue
        segment_end = _next_anchor_start(anchors, start, default=len(text))
        coord = _extract_first_coord(text[end:segment_end])
        if coord:
            results[field] = coord

    unresolved = [
        field
        for field in fields
        if field not in results
        and not is_coord_value(proposed_updates.get(field))
        and not is_coord_value(current_state.get(field))
    ]

    if unresolved and not anchors:
        # 尝试从文本中提取所有坐标对，按顺序分配给未解决字段
        all_coords = _extract_all_coords(text)
        for field, coord in zip(unresolved, all_coords):
            results[field] = coord

    return results


def _find_anchors(text: str, fields: set[str]) -> list[tuple[str, int, int]]:
    anchors: list[tuple[str, int, int]] = []
    for field in fields:
        aliases = sorted(FIELD_ALIASES.get(field, ()), key=len, reverse=True)
        for alias in aliases:
            match = re.search(re.escape(alias), text)
            if match:
                anchors.append((field, match.start(), match.end()))
                break
    anchors.sort(key=lambda item: item[1])
    return anchors


def _next_anchor_start(
    anchors: list[tuple[str, int, int]],
    current_start: int,
    default: int,
) -> int:
    for _, start, _ in anchors:
        if start > current_start:
            return start
    return default


def _extract_first_coord(text: str) -> dict[str, float] | None:
    match = _LAT_LON_RE.search(text)
    if match:
        return _normalize_coord(match.group(1), match.group(2))

    match = _LON_LAT_RE.search(text)
    if match:
        return _normalize_coord(match.group(2), match.group(1))

    match = _PAIR_RE.search(text)
    if match:
        return _normalize_coord(match.group(1), match.group(2))

    return None


def _extract_all_coords(text: str) -> list[dict[str, float]]:
    """Extract ALL coordinate pairs from text in order of appearance."""
    coords: list[dict[str, float]] = []
    seen_spans: list[tuple[int, int]] = []

    def _overlaps(start: int, end: int) -> bool:
        return any(s < end and start < e for s, e in seen_spans)

    # 1. 优先匹配 北纬X度，东经Y度 格式（全文 findall）
    for match in _LAT_LON_RE.finditer(text):
        if not _overlaps(match.start(), match.end()):
            coord = _normalize_coord(match.group(1), match.group(2))
            if coord:
                coords.append(coord)
                seen_spans.append((match.start(), match.end()))

    # 2. 匹配 东经X度，北纬Y度 格式
    for match in _LON_LAT_RE.finditer(text):
        if not _overlaps(match.start(), match.end()):
            coord = _normalize_coord(match.group(2), match.group(1))
            if coord:
                coords.append(coord)
                seen_spans.append((match.start(), match.end()))

    # 3. 若无带标注的格式，回退到纯数字对
    if not coords:
        for match in _PAIR_RE.finditer(text):
            if not _overlaps(match.start(), match.end()):
                coord = _normalize_coord(match.group(1), match.group(2))
                if coord:
                    coords.append(coord)
                    seen_spans.append((match.start(), match.end()))

    # 按文本出现顺序排序
    paired = sorted(zip(seen_spans, coords), key=lambda x: x[0][0])
    return [c for _, c in paired]


def _normalize_coord(lat_raw: Any, lon_raw: Any) -> dict[str, float] | None:
    lat = _parse_number(lat_raw)
    lon = _parse_number(lon_raw)
    if lat is None or lon is None:
        return None
    if not _in_coord_range(lat, lon):
        return None
    return {"lat": lat, "lon": lon}


def _in_coord_range(lat: float, lon: float) -> bool:
    return -90 <= lat <= 90 and -180 <= lon <= 180


def _parse_number(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None

    text = value.strip()
    if not text:
        return None

    try:
        return float(text)
    except ValueError:
        return _parse_chinese_number(text)


def _parse_chinese_number(text: str) -> float | None:
    negative = text.startswith("负")
    if negative:
        text = text[1:]
    if not text:
        return None

    if "点" in text:
        integer_text, decimal_text = text.split("点", 1)
    else:
        integer_text, decimal_text = text, ""

    integer = _parse_chinese_integer(integer_text) if integer_text else 0
    if integer is None:
        return None

    decimal = 0.0
    if decimal_text:
        factor = 0.1
        for char in decimal_text:
            digit = _CN_DIGITS.get(char)
            if digit is None:
                return None
            decimal += digit * factor
            factor /= 10

    result = integer + decimal
    return -result if negative else result


def _parse_chinese_integer(text: str) -> int | None:
    if not text:
        return 0
    if all(char in _CN_DIGITS for char in text):
        return int("".join(str(_CN_DIGITS[char]) for char in text))

    total = 0
    section = 0
    number = 0
    for char in text:
        if char in _CN_DIGITS:
            number = _CN_DIGITS[char]
        elif char in _CN_UNITS:
            unit = _CN_UNITS[char]
            section += (number or 1) * unit
            number = 0
        elif char == "万":
            total += (section + number or 1) * 10000
            section = 0
            number = 0
        else:
            return None
    return total + section + number
