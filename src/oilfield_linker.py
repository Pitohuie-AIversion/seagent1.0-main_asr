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


class OilfieldEntityLinker:
    def __init__(self, environment: dict):
        self.entities = environment.get("oil_fields", []) if isinstance(environment, dict) else []

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
