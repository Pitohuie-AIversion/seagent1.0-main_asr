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

from .assertion_scope import task_edits_are_paused


COORD_FIELD_ORDER = (
    "start_point",
    "end_point",
    "oilfield_coordinates",
)
COORD_FIELDS = frozenset(COORD_FIELD_ORDER)

FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "start_point": ("起始点", "开始点", "起点", "起始坐标", "开始坐标", "起始位置", "起始端"),
    "end_point": ("结束点", "终止点", "终点", "结束坐标", "终止坐标", "结束位置", "终止端"),
    "oilfield_coordinates": ("油田经纬度坐标", "油田经纬度", "油田坐标", "油田位置"),
}

_ARABIC_NUM = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)"
_CHINESE_NUM = r"[负零〇一二两三四五六七八九十百千万点]+"
_VALUE = rf"(?:{_ARABIC_NUM}|{_CHINESE_NUM})"
# 无括号时必须有明确分隔标点（逗号、顿号、斜杠），避免将日期与时间（如 2026-08-20 15:00）误匹配为 (-20, 15)
_SEP_STRICT = r"(?:\s*[,，、/]\s*|\s*逗号\s*)"
_PAIR_RE = re.compile(
    rf"(?:[（(]\s*({_VALUE})\s*(?:{_SEP_STRICT}|\s+)\s*({_VALUE})\s*[）)]|"
    rf"({_VALUE}){_SEP_STRICT}({_VALUE}))"
)
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
        lat = value.get("lat") if "lat" in value else value.get("latitude")
        lon = value.get("lon") if "lon" in value else value.get("longitude")
        return _normalize_coord(lat, lon)
    if not isinstance(value, str):
        return None

    text = value.strip()
    if not text:
        return None

    # 1. 尝试 JSON 与 Python dict 字符串反序列化
    if (text.startswith("{") and text.endswith("}")) or ("'lat'" in text or '"lat"' in text):
        try:
            import json
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                lat = parsed.get("lat") if "lat" in parsed else parsed.get("latitude")
                lon = parsed.get("lon") if "lon" in parsed else parsed.get("longitude")
                coord = _normalize_coord(lat, lon)
                if coord:
                    return coord
        except Exception:
            pass

        try:
            import ast
            parsed = ast.literal_eval(text)
            if isinstance(parsed, dict):
                lat = parsed.get("lat") if "lat" in parsed else parsed.get("latitude")
                lon = parsed.get("lon") if "lon" in parsed else parsed.get("longitude")
                coord = _normalize_coord(lat, lon)
                if coord:
                    return coord
        except Exception:
            pass

    # 2. 独立坐标字符串空格分隔裸值容错（如 "11.2 113.4"）
    bare_match = re.match(rf"^\s*({_VALUE})\s+({_VALUE})\s*$", text)
    if bare_match:
        coord = _normalize_coord(bare_match.group(1), bare_match.group(2))
        if coord:
            return coord

    return _extract_first_coord(text)



def parse_coordinate_updates(
    text: str,
    candidate_fields: set[str],
    current_state: dict[str, Any] | None = None,
    proposed_updates: dict[str, Any] | None = None,
) -> dict[str, dict[str, float]]:
    """
    Extract coordinate updates from the latest user text.

    Anchored phrases like "起始点19.8，113.5" are preferred. Bare pairs fill
    missing fields in schema order. Non-asserted and ambiguous values also
    reject corresponding model proposals.
    """
    fields = [field for field in COORD_FIELD_ORDER if field in candidate_fields]
    if not text or not fields:
        return {}

    current_state = current_state or {}
    proposed_updates = proposed_updates or {}
    explicit, protected = _coordinate_update_evidence(text, fields, current_state)
    results = {
        field: parsed
        for field in fields
        if field not in protected
        and (parsed := parse_coord_value(proposed_updates.get(field))) is not None
    }
    results.update(explicit)
    return results


_COORD_CONDITION = re.compile(r"如果|假如|假设|假定|倘若|要是|除非|若|只要|只有|仅当")
_COORD_HISTORY = re.compile(r"昨天|此前|之前|已完成|完成了|曾经|过去|历史")
_COORD_OTHER_FIELD = re.compile(
    r"水深|支持船|母船|作业船|船只|机器人|设备|ROV|AUV|井口|管缆|电缆|载荷|工具|时间|开始|结束|油田|作业区域|工区",
    re.IGNORECASE,
)
_COORD_NON_ASSERTION = re.compile(
    r"不要|别(?:把|将|改|换|设|动|用|选)|不(?:要|想|改|换|用|选|修改|更改)|"
    r"取消|撤销|暂不|先不|不是|并非|不能|不可|不许|不必|无需|禁止|请勿|勿|不变|保持|保留|"
    r"讨论|咨询|考虑|例如|比如|示例|昨天|曾经|历史|"
    r"是否|能否|可否|怎么样|如何|什么|哪个|哪种|[？?]|(?:吗|呢)(?:[，,。！!\s]|$)"
)
_COORD_REVOCATION = re.compile(r"不要|别|不改|不修改|取消|撤销|不变|保持|保留|暂不|先不|不能|不可|不许|不必|无需|禁止|请勿|勿")
_COORD_FOLLOWUP_QUERY = re.compile(
    r"^\s*(?:这样|这个|上述|刚才的|这样设置|这样修改)?\s*"
    r"(?:行吗|可以吗|合适吗|对吗|是否可以|能否执行|可以不|怎么样)[？?。！!\s]*$"
)
_GENERIC_COORD_OBJECT = r"(?:(?:任何|所有|全部|当前|现有|这些|这个|上述|刚才的)\s*)*(?:坐标|经纬度)"
_GENERIC_COORD_PAUSE = re.compile(
    r"(?:(?:请|本轮|这轮|这次|现在|目前|暂时|先)\s*)*(?:"
    r"(?:不要|别|暂不|先不)(?:修改|更改|改动|改|调整)\s*" + _GENERIC_COORD_OBJECT
    + r"|" + _GENERIC_COORD_OBJECT + r"\s*(?:保持)?(?:不变|不要修改|不要改|不改|别改)"
    + r"|(?:保持|保留)\s*" + _GENERIC_COORD_OBJECT + r"\s*(?:不变)?"
    + r"|(?:取消|撤销)\s*(?:对)?\s*" + _GENERIC_COORD_OBJECT + r"\s*(?:的)?(?:修改|更改|改动|调整|变更|设置)?"
    + r")\s*(?:了|吧)?"
)


def protected_coordinate_fields(
    text: str,
    candidate_fields: set[str],
    current_state: dict[str, Any] | None = None,
) -> set[str]:
    """Fields whose rejected or ambiguous text must also veto model candidates."""
    fields = [field for field in COORD_FIELD_ORDER if field in candidate_fields]
    return _coordinate_update_evidence(text, fields, current_state or {})[1]


def coordinate_update_issues(
    text: str,
    candidate_fields: set[str],
    current_state: dict[str, Any] | None = None,
) -> list[str]:
    """Explain malformed or conflicting assertions without flagging questions."""
    fields = [field for field in COORD_FIELD_ORDER if field in candidate_fields]
    issues: list[str] = []
    _coordinate_update_evidence(text, fields, current_state or {}, issues)
    return issues


def _coordinate_clauses(sentence: str) -> list[str]:
    """Split comma clauses without splitting a latitude/longitude pair."""
    matches = sorted(
        (match.start(), match.end())
        for pattern in (_LAT_LON_RE, _LON_LAT_RE, _PAIR_RE)
        for match in pattern.finditer(sentence)
    )
    spans: list[tuple[int, int]] = []
    for start, end in matches:
        # A reverse-axis regex can bridge from the longitude of one complete
        # point to the latitude of the next. It must not hide their clauses.
        if not any(left < end and start < right for left, right in spans):
            spans.append((start, end))
    cuts = [
        match.start() for match in re.finditer(r"[,，]", sentence)
        if not any(start <= match.start() < end for start, end in spans)
    ]
    clauses = []
    start = 0
    for end in cuts:
        clauses.append(sentence[start:end])
        start = end + 1
    clauses.append(sentence[start:])
    return clauses


def _has_invalid_coordinate(text: str) -> bool:
    """Inspect literal pairs too, since range-invalid pairs are not extracted."""
    seen: list[tuple[int, int]] = []
    for pattern in (_LAT_LON_RE, _LON_LAT_RE, _PAIR_RE):
        for match in pattern.finditer(text):
            if any(start < match.end() and match.start() < end for start, end in seen):
                continue
            seen.append((match.start(), match.end()))
            if pattern is _PAIR_RE:
                lat = match.group(1) or match.group(3)
                lon = match.group(2) or match.group(4)
            elif pattern is _LON_LAT_RE:
                lon, lat = match.group(1), match.group(2)
            else:
                lat, lon = match.group(1), match.group(2)
            if _normalize_coord(lat, lon) is None:
                return True
    return False


def _coordinate_history_clauses(clauses, fields):
    historical_fields: set[str] = set()
    for clause in clauses:
        if _COORD_HISTORY.search(clause):
            anchored = {field for field, _, _ in _find_anchors(clause, fields)}
            historical_fields = anchored or (set() if _COORD_OTHER_FIELD.search(clause) else set(fields))
        elif re.match(r"\s*(?:现在|目前|当前|本次)", clause):
            historical_fields = set()
        yield clause, historical_fields


def _coordinate_update_evidence(
    text: str, fields: list[str], current_state: dict[str, Any],
    issues: list[str] | None = None,
) -> tuple[dict[str, dict[str, float]], set[str]]:
    if not text or not fields:
        return {}, set()
    if task_edits_are_paused(text):
        return {}, set(fields)
    # Match the whole object clause: "起点坐标不变" protects only the
    # anchored start point, whereas "坐标不变" protects all coordinate fields.
    historical_pauses: set[str] = set()
    for sentence in re.split(r"[。；;！!？?\n]+", text):
        for clause, historical_fields in _coordinate_history_clauses(_coordinate_clauses(sentence), fields):
            if _GENERIC_COORD_PAUSE.fullmatch(clause.strip()):
                if not historical_fields:
                    return {}, set(fields)
                historical_pauses.update(fields)

    selected: dict[str, list[dict[str, float]]] = {}
    rejected: set[str] = historical_pauses
    revoked: set[str] = set()
    invalid: set[str] = set()
    invalid_bare = False
    bare_coords: list[dict[str, float]] = []
    has_anchors = bool(_find_anchors(text, fields))
    for match in re.finditer(r"[^。；;！!？?\n]+[。；;！!？?\n]?", text):
        sentence = match.group()
        clauses = _coordinate_clauses(sentence)
        scoped_clauses = list(_coordinate_history_clauses(clauses, fields))
        condition = _COORD_CONDITION.search(sentence)
        condition_tail = sentence[condition.start():] if condition else ""
        condition_fields = {field for field, _, _ in _find_anchors(condition_tail, fields)}
        condition_has_branch = len(_coordinate_clauses(condition_tail)) > 1
        query_fields: set[str] = set()
        cancel_fields: set[str] = set()
        trailing_condition_fields: set[str] = set()
        previous_fields: set[str] = set()
        for clause, historical_fields in scoped_clauses:
            # Generic follow-ups refer to the nearest named subject. A depth
            # question or cancellation cannot revoke an earlier coordinate.
            if _COORD_FOLLOWUP_QUERY.fullmatch(clause):
                query_fields.update(previous_fields - historical_fields)
            if re.fullmatch(
                r"\s*(?:等等[，,]?)?(?:先|暂时)?(?:不要|别|取消|撤销)"
                r"(?:执行|应用|确认|修改|更改|改动|改|这个|上述|刚才的)(?:修改|操作)?[。！!\s]*",
                clause,
            ):
                cancel_fields.update(previous_fields - historical_fields)
            if not condition_has_branch and _COORD_CONDITION.search(clause):
                trailing_condition_fields.update(previous_fields - historical_fields)
            anchored = {field for field, _, _ in _find_anchors(clause, fields)}
            if anchored:
                previous_fields = anchored
            elif not has_anchors and (_extract_all_coords(clause) or _has_invalid_coordinate(clause)):
                previous_fields = set(fields)
            elif _COORD_OTHER_FIELD.search(clause):
                previous_fields = set()
        revoked.update(cancel_fields)

        clause_offset = 0
        for clause, historical_fields in scoped_clauses:
            clause_end = clause_offset + len(clause)
            # A later condition about another field does not retroactively
            # cancel an earlier assignment. A trailing qualifier without a
            # separate branch can still qualify the preceding coordinate.
            conditional_clause = bool(condition and condition.start() < clause_end)
            clause_offset = clause_end + 1
            anchors = _find_anchors(clause, fields)
            if not anchors:
                coords = _extract_all_coords(clause)
                malformed = _has_invalid_coordinate(clause)
                if (not coords and not malformed) or has_anchors:
                    continue
                if (conditional_clause or condition_fields or trailing_condition_fields
                        or query_fields or cancel_fields or historical_fields or _COORD_NON_ASSERTION.search(clause)):
                    rejected.update(fields)
                else:
                    bare_coords.extend(coords)
                    invalid_bare = invalid_bare or malformed
                continue
            for index, (field, start, end) in enumerate(anchors):
                segment_end = anchors[index + 1][1] if index + 1 < len(anchors) else len(clause)
                segment = clause[start:segment_end]
                # Include a leading verb such as "不要把" for the first anchor.
                if index == 0:
                    segment = clause[:segment_end]
                coords = _extract_all_coords(clause[end:segment_end])
                if (conditional_clause or field in condition_fields or field in trailing_condition_fields
                        or field in query_fields or field in cancel_fields or field in historical_fields
                        or _COORD_NON_ASSERTION.search(segment)):
                    rejected.add(field)
                    if field not in historical_fields and (
                        (not coords and _COORD_REVOCATION.search(segment))
                        or re.search(r"取消|撤销|不变|保持|保留", segment)
                    ):
                        revoked.add(field)
                    continue
                if coords:
                    selected.setdefault(field, []).extend(coords)
                if _has_invalid_coordinate(clause[end:segment_end]):
                    invalid.add(field)

    explicit: dict[str, dict[str, float]] = {}
    labels = {"start_point": "起始点", "end_point": "结束点", "oilfield_coordinates": "油田坐标"}
    for field, coords in selected.items():
        unique = {(coord["lat"], coord["lon"]) for coord in coords}
        if len(unique) == 1 and field not in revoked:
            explicit[field] = coords[-1]
            rejected.discard(field)
        else:
            rejected.add(field)
            if len(unique) > 1 and issues is not None:
                issues.append(f"{labels[field]}存在多个不同坐标，无法确定本轮选择，未写入。")
    if not has_anchors:
        missing = [
            field for field in fields
            if not is_coord_value(current_state.get(field))
        ]
        # Preserve positional start/end input, but do not silently ignore
        # surplus pairs that make the intended assignment ambiguous.
        if invalid_bare:
            rejected.update(fields)
            if issues is not None:
                issues.append("提供的经纬度超出合法范围，未写入。")
        elif len(bare_coords) > len(missing):
            rejected.update(missing)
            if issues is not None:
                issues.append("提供的坐标数量多于待补充的坐标字段，无法确定对应关系，未写入。")
        else:
            for field, coord in zip(missing, bare_coords):
                explicit[field] = coord
                rejected.discard(field)
    rejected.update(revoked)
    rejected.update(invalid)
    if issues is not None:
        issues.extend(f"{labels[field]}经纬度超出合法范围，未写入。" for field in fields if field in invalid)
    return {field: coord for field, coord in explicit.items() if field not in rejected}, rejected


def _find_anchors(text: str, fields) -> list[tuple[str, int, int]]:
    aliases_to_fields = {}
    for field in fields:
        for alias in FIELD_ALIASES.get(field, ()):
            aliases_to_fields[alias] = field
    if not aliases_to_fields:
        return []
    pattern = "|".join(re.escape(alias) for alias in sorted(aliases_to_fields, key=len, reverse=True))
    return [
        (aliases_to_fields[match.group()], match.start(), match.end())
        for match in re.finditer(pattern, text)
    ]


def _extract_first_coord(text: str) -> dict[str, float] | None:
    match = _LAT_LON_RE.search(text)
    if match:
        return _normalize_coord(match.group(1), match.group(2))

    match = _LON_LAT_RE.search(text)
    if match:
        return _normalize_coord(match.group(2), match.group(1))

    match = _PAIR_RE.search(text)
    if match:
        lat_val = match.group(1) or match.group(3)
        lon_val = match.group(2) or match.group(4)
        return _normalize_coord(lat_val, lon_val)

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

    # 3. Include separate bare pairs too, while excluding numbers already
    # consumed by labelled coordinates.
    for match in _PAIR_RE.finditer(text):
        if not _overlaps(match.start(), match.end()):
            lat_val = match.group(1) or match.group(3)
            lon_val = match.group(2) or match.group(4)
            coord = _normalize_coord(lat_val, lon_val)
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


def format_coord_display(val: Any) -> str | None:
    """Format a coordinate value into natural language: '北纬 19.8 度，东经 113.2 度'"""
    if val is None:
        return None

    if isinstance(val, str):
        val_str = val.strip()
        if not val_str:
            return None
        if ("北纬" in val_str or "南纬" in val_str) and ("东经" in val_str or "西经" in val_str):
            return val_str
        if val_str.startswith("{") and val_str.endswith("}"):
            try:
                import json
                val = json.loads(val_str)
            except Exception:
                pass

    if isinstance(val, dict):
        lat = val.get("lat") if "lat" in val else val.get("latitude")
        lon = val.get("lon") if "lon" in val else val.get("longitude")
        if lat is not None and lon is not None:
            try:
                lat_f = float(lat)
                lon_f = float(lon)
                lat_dir = "北纬" if lat_f >= 0 else "南纬"
                lon_dir = "东经" if lon_f >= 0 else "西经"
                lat_val = int(abs(lat_f)) if abs(lat_f).is_integer() else abs(lat_f)
                lon_val = int(abs(lon_f)) if abs(lon_f).is_integer() else abs(lon_f)
                return f"{lat_dir} {lat_val} 度，{lon_dir} {lon_val} 度"
            except (ValueError, TypeError):
                pass
    elif isinstance(val, (list, tuple)) and len(val) == 2:
        try:
            lat_f = float(val[0])
            lon_f = float(val[1])
            lat_dir = "北纬" if lat_f >= 0 else "南纬"
            lon_dir = "东经" if lon_f >= 0 else "西经"
            lat_val = int(abs(lat_f)) if abs(lat_f).is_integer() else abs(lat_f)
            lon_val = int(abs(lon_f)) if abs(lon_f).is_integer() else abs(lon_f)
            return f"{lat_dir} {lat_val} 度，{lon_dir} {lon_val} 度"
        except (ValueError, TypeError):
            pass
    return None


def format_slot_display_value(key: str, val: Any) -> str:
    """Format any slot value into user-facing natural language."""
    if val is None or val == "":
        return "暂无"

    # 1. Coordinates
    coord_fmt = format_coord_display(val)
    if coord_fmt is not None:
        return coord_fmt

    # 2. Water depth / numeric fields with unit
    if key == "water_depth" or "depth" in key:
        if isinstance(val, (int, float)):
            num_val = int(val) if isinstance(val, float) and val.is_integer() else val
            return f"{num_val} 米"
        elif isinstance(val, str):
            val_str = val.strip()
            if val_str.endswith("米"):
                return val_str
            try:
                f_val = float(val_str)
                num_val = int(f_val) if f_val.is_integer() else f_val
                return f"{num_val} 米"
            except ValueError:
                return val_str

    # 3. Specification dict
    if isinstance(val, dict):
        if "display_value" in val and val["display_value"]:
            return str(val["display_value"])
        if "value" in val and "unit" in val:
            return f"{val['value']} {val['unit']}"
        import json
        return json.dumps(val, ensure_ascii=False)

    # 4. List values
    if isinstance(val, list):
        formatted_items = []
        for item in val:
            if isinstance(item, dict):
                item_fmt = format_coord_display(item)
                if item_fmt:
                    formatted_items.append(item_fmt)
                elif "display_value" in item:
                    formatted_items.append(str(item["display_value"]))
                else:
                    import json
                    formatted_items.append(json.dumps(item, ensure_ascii=False))
            else:
                formatted_items.append(str(item))
        return "、".join(formatted_items)

    return str(val)
