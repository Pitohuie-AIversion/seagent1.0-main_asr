"""Oilfield entity linking for controlled field normalization."""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
import math
import re
from typing import Any


_CN_NUMBERS = {
    "零": "0",
    "〇": "0",
    "一": "1",
    "二": "2",
    "两": "2",
    "三": "3",
    "四": "4",
    "五": "5",
    "六": "6",
    "七": "7",
    "八": "8",
    "九": "9",
}

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

_PINYIN = {
    "流": "liu",
    "刘": "liu",
    "留": "liu",
    "硫": "liu",
    "浏": "liu",
    "花": "hua",
    "华": "hua",
    "化": "hua",
    "话": "hua",
    "陵": "ling",
    "灵": "ling",
    "临": "lin",
    "林": "lin",
    "水": "shui",
    "蓬": "peng",
    "鹏": "peng",
    "朋": "peng",
    "莱": "lai",
    "来": "lai",
    "春": "chun",
    "椿": "chun",
    "晓": "xiao",
    "小": "xiao",
    "宵": "xiao",
}


@dataclass(frozen=True)
class OilfieldMatch:
    raw: str
    standard_name: str | None
    entity_id: str | None
    confidence: float
    status: str
    evidence: list[str]
    candidates: list[dict[str, Any]]


@dataclass(frozen=True)
class OilfieldIssue:
    constraint_id: str
    constraint_name: str
    check_type: str
    severity: str
    message: str
    related_fields: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "constraint_id": self.constraint_id,
            "constraint_name": self.constraint_name,
            "check_type": self.check_type,
            "severity": self.severity,
            "message": self.message,
            "related_fields": list(self.related_fields),
        }


@dataclass(frozen=True)
class OilfieldContextResult:
    entity_id: str
    standard_name: str
    coordinate_range: dict[str, list[float]]
    default_coordinates: dict[str, float] | None
    reference_water_depth: float | int | None
    coordinate_status: str
    depth_status: str
    maximum_reference_water_depth: float | int | None = None
    feedback: tuple[str, ...] = ()
    issues: tuple[OilfieldIssue, ...] = ()

    @property
    def oilfield_name(self) -> str:
        return self.standard_name

    @property
    def defaults(self) -> dict[str, Any]:
        defaults: dict[str, Any] = {}
        if self.default_coordinates is not None:
            defaults["oilfield_coordinates"] = self.default_coordinates
        if self.reference_water_depth is not None:
            defaults["water_depth"] = self.reference_water_depth
        return defaults

    @property
    def reference(self) -> dict[str, Any]:
        return {
            "lat_range": self.coordinate_range.get("lat"),
            "lon_range": self.coordinate_range.get("lon"),
            "water_depth": self.reference_water_depth,
            "maximum_reference_water_depth": self.maximum_reference_water_depth,
        }

    @property
    def violations(self) -> list[dict[str, Any]]:
        return [issue.to_dict() for issue in self.issues]


_UNSET = object()


class OilfieldEntityLinker:
    def __init__(self, environment: dict, constraints: list[dict[str, Any]] | None = None):
        self.entities = environment.get("oil_fields", []) if isinstance(environment, dict) else []
        self.entities_by_id = {
            entity["id"]: entity
            for entity in self.entities
            if isinstance(entity, dict) and entity.get("id")
        }
        self.constraints_by_check_type = {
            item.get("check_type"): item
            for item in (constraints or [])
            if isinstance(item, dict) and item.get("check_type")
        }

    def find_entity_by_coords(self, coords: dict[str, Any] | None) -> dict[str, Any] | None:
        """根据坐标检查是否落入知识库中某一油田的经纬度范围内。"""
        if not isinstance(coords, dict):
            return None
        lat = _as_float(coords.get("lat"))
        lon = _as_float(coords.get("lon"))
        if lat is None or lon is None:
            return None

        for entity in self.entities:
            try:
                coordinate_range = _get_coordinate_range(entity)
                lat_min, lat_max = coordinate_range["lat"]
                lon_min, lon_max = coordinate_range["lon"]
                if lat_min <= lat <= lat_max and lon_min <= lon <= lon_max:
                    return entity
            except Exception:
                continue
        return None

    def link(self, raw_name: str, coords: dict[str, Any] | None = None) -> OilfieldMatch:
        raw = str(raw_name or "").strip()
        if not raw:
            return OilfieldMatch(raw, None, None, 0.0, "empty", [], [])

        candidates = [self._score_entity(raw, entity, coords) for entity in self.entities]
        candidates.sort(key=lambda item: item["score"], reverse=True)
        if not candidates or candidates[0]["score"] <= 0:
            return OilfieldMatch(raw, None, None, 0.0, "unmatched", [], [])

        best = candidates[0]
        second_score = candidates[1]["score"] if len(candidates) > 1 else 0.0
        confidence = round(min(best["score"], 100.0) / 100.0, 3)
        public_candidates = [
            {
                "id": item["id"],
                "name": item["name"],
                "confidence": round(min(item["score"], 100.0) / 100.0, 3),
                "evidence": item["evidence"],
            }
            for item in candidates[:3]
            if item["score"] >= 45
        ]

        if best["score"] >= 75 and best["score"] - second_score >= 8:
            return OilfieldMatch(
                raw=raw,
                standard_name=best["name"],
                entity_id=best["id"],
                confidence=confidence,
                status="accepted",
                evidence=best["evidence"],
                candidates=public_candidates,
            )
        if best["score"] >= 55:
            return OilfieldMatch(
                raw=raw,
                standard_name=None,
                entity_id=None,
                confidence=confidence,
                status="ambiguous",
                evidence=best["evidence"],
                candidates=public_candidates,
            )
        return OilfieldMatch(
            raw=raw,
            standard_name=None,
            entity_id=None,
            confidence=confidence,
            status="unmatched",
            evidence=best["evidence"],
            candidates=public_candidates,
        )

    def evaluate_context(
        self,
        *,
        entity_id: str,
        coordinates: object = _UNSET,
        water_depth: object = _UNSET,
    ) -> OilfieldContextResult:
        entity = self.entities_by_id.get(str(entity_id or ""))
        if not entity:
            raise ValueError(f"未知油田实体: entity_id={entity_id}")

        coordinate_range = _get_coordinate_range(entity)
        default_coordinates = {
            "lat": round((coordinate_range["lat"][0] + coordinate_range["lat"][1]) / 2, 6),
            "lon": round((coordinate_range["lon"][0] + coordinate_range["lon"][1]) / 2, 6),
        }
        reference_depth = _get_reference_water_depth(entity)
        maximum_reference_depth = _get_maximum_reference_water_depth(entity)

        coordinate_status = "not_provided"
        depth_status = "not_provided"
        feedback: list[str] = [
            f"已匹配{entity.get('name')}。知识库记录的油田范围为北纬"
            f"{_format_number(coordinate_range['lat'][0])}～{_format_number(coordinate_range['lat'][1])}度、"
            f"东经{_format_number(coordinate_range['lon'][0])}～{_format_number(coordinate_range['lon'][1])}度，"
            f"默认参考水深为{_format_number(reference_depth)}米，知识库校验上限为"
            f"{_format_number(maximum_reference_depth)}米。当前暂采用油田范围中心坐标"
            f"（{_format_number(default_coordinates['lat'])}，{_format_number(default_coordinates['lon'])}）"
            f"和参考水深{_format_number(reference_depth)}米，您后续可以提供实际作业坐标和水深进行覆盖。"
        ]
        issues: list[OilfieldIssue] = []

        if coordinates is not _UNSET:
            coordinate_status, coord_values = _check_coordinates(coordinates, entity)
            if coordinate_status == "matched":
                feedback.append(
                    f"您提供的实际坐标位于{entity.get('name')}知识库范围内，"
                    "已采用实际坐标覆盖默认中心坐标。"
                )
            elif coordinate_status == "mismatched":
                issues.append(
                    self._build_issue(
                        "oilfield_coordinate_mismatch",
                        {"oilfield_name": entity.get("name"), **coord_values},
                        ("oilfield_name", "oilfield_coordinates"),
                    )
                )

        if water_depth is not _UNSET:
            depth_status, depth_values = _check_water_depth(water_depth, entity)
            if depth_status == "within_reference":
                feedback.append(
                    f"实际作业水深{_format_number(depth_values['actual_depth'])}米未超过"
                    f"{entity.get('name')}知识库校验上限"
                    f"{_format_number(depth_values['maximum_reference_depth'])}米，"
                    "已采用实际水深覆盖默认值。"
                )
            elif depth_status == "exceeded_reference":
                issues.append(
                    self._build_issue(
                        "oilfield_reference_depth_exceeded",
                        {"oilfield_name": entity.get("name"), **depth_values},
                        ("oilfield_name", "water_depth"),
                    )
                )

        return OilfieldContextResult(
            entity_id=str(entity.get("id")),
            standard_name=str(entity.get("name")),
            coordinate_range=coordinate_range,
            default_coordinates=default_coordinates,
            reference_water_depth=reference_depth,
            coordinate_status=coordinate_status,
            depth_status=depth_status,
            maximum_reference_water_depth=maximum_reference_depth,
            feedback=tuple(feedback),
            issues=tuple(issues),
        )

    def _find_entity(self, *, entity_id: str | None, oilfield_name: str | None) -> dict[str, Any] | None:
        if entity_id:
            for entity in self.entities:
                if entity.get("id") == entity_id:
                    return entity
        if oilfield_name:
            target = _normalize_text(str(oilfield_name))
            for entity in self.entities:
                names = [entity.get("name", ""), *entity.get("aliases", [])]
                if any(_normalize_text(str(name)) == target for name in names):
                    return entity
        return None

    def _build_issue(
        self,
        check_type: str,
        values: dict[str, Any],
        related_fields: tuple[str, ...],
    ) -> OilfieldIssue:
        constraint = self.constraints_by_check_type.get(check_type)
        if not constraint:
            raise ValueError(f"缺少油田校验约束配置: check_type={check_type}")

        message = str(constraint.get("violation_message", "")).strip()
        for key, value in values.items():
            message = message.replace("{" + key + "}", _format_number(value))
        return OilfieldIssue(
            constraint_id=str(constraint.get("id")),
            constraint_name=str(constraint.get("name")),
            check_type=check_type,
            severity=str(constraint.get("severity")),
            message=message,
            related_fields=related_fields,
        )

    def _score_entity(self, raw: str, entity: dict[str, Any], coords: dict[str, Any] | None) -> dict[str, Any]:
        names = [entity.get("name", ""), *entity.get("aliases", [])]
        raw_norm = _normalize_text(raw)
        raw_digits = _extract_digit_pattern(raw_norm)
        raw_pinyin = _to_loose_pinyin(raw_norm)

        best_text_score = 0.0
        evidence: list[str] = []
        for name in names:
            name_norm = _normalize_text(str(name))
            if not name_norm:
                continue
            if raw_norm == name_norm:
                best_text_score = max(best_text_score, 95.0)
                evidence.append(f"命中标准名或别名“{name}”")
                continue
            if raw_norm in name_norm or name_norm in raw_norm:
                best_text_score = max(best_text_score, 82.0)
                evidence.append(f"名称包含匹配“{name}”")

            char_ratio = SequenceMatcher(None, raw_norm, name_norm).ratio()
            best_text_score = max(best_text_score, char_ratio * 35.0)

            name_pinyin = _to_loose_pinyin(name_norm)
            if raw_pinyin and name_pinyin:
                pinyin_ratio = SequenceMatcher(None, raw_pinyin, name_pinyin).ratio()
                if pinyin_ratio >= 0.72:
                    best_text_score = max(best_text_score, pinyin_ratio * 55.0)
                    evidence.append(f"拼音相似“{name}”")

            name_digits = _extract_digit_pattern(name_norm)
            if raw_digits and name_digits and raw_digits == name_digits:
                best_text_score += 28.0
                evidence.append(f"数字段匹配“{raw_digits}”")
                break

        coord_score, coord_evidence = _score_coords(coords, entity)
        evidence.extend(coord_evidence)
        score = min(best_text_score + coord_score, 120.0)
        if not evidence and best_text_score > 0:
            evidence.append("名称相似度匹配")
        return {
            "id": entity.get("id"),
            "name": entity.get("name"),
            "score": score,
            "evidence": evidence,
        }


def _normalize_text(text: str) -> str:
    text = text.strip().lower()
    text = text.replace("杠", "-").replace("—", "-").replace("－", "-").replace("_", "-")
    text = _normalize_chinese_numbers(text)
    for cn, digit in _CN_NUMBERS.items():
        text = text.replace(cn, digit)
    for suffix in ("油田", "气田", "区块", "井区", "海域"):
        text = text.replace(suffix, "")
    return re.sub(r"\s+", "", text)


def _normalize_chinese_numbers(text: str) -> str:
    return re.sub(r"[零〇一二两三四五六七八九十]+", _replace_chinese_number, text)


def _replace_chinese_number(match: re.Match[str]) -> str:
    token = match.group()
    value = _parse_chinese_number(token)
    return str(value) if value is not None else token


def _parse_chinese_number(token: str) -> int | None:
    if not token:
        return None
    if "十" not in token:
        digits = [_CN_DIGITS.get(char) for char in token]
        if any(value is None for value in digits):
            return None
        return int("".join(str(value) for value in digits))

    parts = token.split("十")
    if len(parts) != 2:
        return None
    tens_text, ones_text = parts
    if len(tens_text) > 1 or len(ones_text) > 1:
        return None
    tens = _CN_DIGITS.get(tens_text, 1) if tens_text else 1
    ones = _CN_DIGITS.get(ones_text, 0) if ones_text else 0
    if tens is None or ones is None:
        return None
    return tens * 10 + ones


def _extract_digit_pattern(text: str) -> str | None:
    match = re.search(r"(\d+)\D+(\d+)", text)
    if match:
        return f"{int(match.group(1))}-{int(match.group(2))}"
    match = re.search(r"\d+", text)
    return str(int(match.group())) if match else None


def _to_loose_pinyin(text: str) -> str:
    parts: list[str] = []
    for char in text:
        if char in _PINYIN:
            parts.append(_PINYIN[char])
        elif char.isascii() and char.isalnum():
            parts.append(char)
    return "".join(parts)


def _score_coords(coords: dict[str, Any] | None, entity: dict[str, Any]) -> tuple[float, list[str]]:
    if not isinstance(coords, dict):
        return 0.0, []
    try:
        lat = float(coords.get("lat"))
        lon = float(coords.get("lon"))
    except (TypeError, ValueError):
        return 0.0, []

    lat_range = entity.get("lat_range") or []
    lon_range = entity.get("lon_range") or []
    if len(lat_range) != 2 or len(lon_range) != 2:
        return 0.0, []

    if lat_range[0] <= lat <= lat_range[1] and lon_range[0] <= lon <= lon_range[1]:
        return 40.0, ["坐标落入标准油田范围"]

    center_lat = (float(lat_range[0]) + float(lat_range[1])) / 2
    center_lon = (float(lon_range[0]) + float(lon_range[1])) / 2
    distance = math.hypot(lat - center_lat, lon - center_lon)
    if distance <= 1.0:
        return 15.0, ["坐标接近标准油田范围"]
    return 0.0, []


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_float_pair(value: Any) -> tuple[float, float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    first = _as_float(value[0])
    second = _as_float(value[1])
    if first is None or second is None:
        return None
    return (first, second)


def _get_coordinate_range(entity: dict[str, Any]) -> dict[str, list[float]]:
    lat_range = _coerce_range(entity.get("lat_range"), -90.0, 90.0, "lat_range", entity)
    lon_range = _coerce_range(entity.get("lon_range"), -180.0, 180.0, "lon_range", entity)
    return {"lat": [lat_range[0], lat_range[1]], "lon": [lon_range[0], lon_range[1]]}


def _get_reference_water_depth(entity: dict[str, Any]) -> float | int | None:
    depth = entity.get("water_depth")
    if depth in (None, ""):
        return None
    value = _as_float(depth)
    if value is None:
        raise ValueError(f"油田参考水深配置无效: entity_id={entity.get('id')}, water_depth={depth}")
    if value < 0:
        raise ValueError(f"油田参考水深不能为负: entity_id={entity.get('id')}, water_depth={depth}")
    return int(value) if value.is_integer() else value


def _get_maximum_reference_water_depth(entity: dict[str, Any]) -> float | int:
    depth = entity.get("maximum_reference_water_depth")
    if depth in (None, ""):
        raise ValueError(
            "油田知识库缺少最大参考水深: "
            f"entity_id={entity.get('id')}, field=maximum_reference_water_depth"
        )
    value = _as_float(depth)
    if value is None:
        raise ValueError(
            "油田最大参考水深配置无效: "
            f"entity_id={entity.get('id')}, maximum_reference_water_depth={depth}"
        )
    if value < 0:
        raise ValueError(
            "油田最大参考水深不能为负: "
            f"entity_id={entity.get('id')}, maximum_reference_water_depth={depth}"
        )
    reference_depth = _get_reference_water_depth(entity)
    if reference_depth is not None and value < float(reference_depth):
        raise ValueError(
            "油田最大参考水深不能小于默认参考水深: "
            f"entity_id={entity.get('id')}, water_depth={reference_depth}, "
            f"maximum_reference_water_depth={depth}"
        )
    return int(value) if value.is_integer() else value


def _check_coordinates(coords: object, entity: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    if not isinstance(coords, dict):
        return "invalid", {}
    lat = _as_float(coords.get("lat"))
    lon = _as_float(coords.get("lon"))
    if lat is None or lon is None:
        return "invalid", {}
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return "invalid", {"actual_lat": lat, "actual_lon": lon}

    ranges = _get_coordinate_range(entity)
    lat_min, lat_max = ranges["lat"]
    lon_min, lon_max = ranges["lon"]
    values = {
        "actual_lat": lat,
        "actual_lon": lon,
        "lat_min": lat_min,
        "lat_max": lat_max,
        "lon_min": lon_min,
        "lon_max": lon_max,
    }
    if lat_min <= lat <= lat_max and lon_min <= lon <= lon_max:
        return "matched", values
    return "mismatched", values


def _check_water_depth(water_depth: object, entity: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    actual_depth = _as_float(water_depth)
    if actual_depth is None:
        return "invalid", {}
    if actual_depth < 0:
        return "invalid", {"actual_depth": actual_depth}

    maximum_reference_depth = _get_maximum_reference_water_depth(entity)
    values = {
        "actual_depth": actual_depth,
        "maximum_reference_depth": maximum_reference_depth,
    }
    if actual_depth <= float(maximum_reference_depth):
        return "within_reference", values
    return "exceeded_reference", values


def _coerce_range(
    raw_range: object,
    min_allowed: float,
    max_allowed: float,
    field_name: str,
    entity: dict[str, Any],
) -> tuple[float, float]:
    if not isinstance(raw_range, (list, tuple)) or len(raw_range) != 2:
        raise ValueError(f"油田{field_name}必须包含两个数值: entity_id={entity.get('id')}")
    first = _as_float(raw_range[0])
    second = _as_float(raw_range[1])
    if first is None or second is None:
        raise ValueError(f"油田{field_name}包含非数值: entity_id={entity.get('id')}")
    if first > second:
        raise ValueError(f"油田{field_name}最小值大于最大值: entity_id={entity.get('id')}")
    if first < min_allowed or second > max_allowed:
        raise ValueError(f"油田{field_name}超出合法范围: entity_id={entity.get('id')}")
    return first, second


def _format_number(value: Any) -> str:
    numeric = _as_float(value)
    if numeric is None:
        return str(value)
    if numeric.is_integer():
        return str(int(numeric))
    return f"{numeric:.6f}".rstrip("0").rstrip(".")
