"""用户按序号选择候选时的可见来源校验。

模型负责理解用户最终选择了哪个标准值；本模块只验证该值是否确实出现在
紧邻上一条助手消息中对应的显式编号位置，防止模型利用后台隐藏候选顺序写入。
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata
from typing import Iterable, Mapping

from .normalizer import FieldNormalizer


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
_ORDINAL_TOKEN = r"(?:\d{1,2}|[零〇一二两三四五六七八九十]{1,3})"
_OPTION_NOUN = r"(?:个|项|条|台|艘|种|号|款|组|位|套)?"
_NUMBERED_LINE = re.compile(
    rf"^\s*(?:[-*+]\s*)?(?:\|\s*)?(?:[（(]\s*)?"
    rf"(?P<number>{_ORDINAL_TOKEN})\s*"
    rf"(?:[.、:：)）]|\|)\s*(?P<body>.+?)\s*$"
)


@dataclass(frozen=True)
class OrdinalReference:
    """一基正向序号；负数表示倒数位置。"""

    position: int
    raw_text: str


def _parse_number_token(token: str) -> int | None:
    normalized = unicodedata.normalize("NFKC", str(token or "")).strip()
    if normalized.isdigit():
        value = int(normalized)
        return value if value > 0 else None
    if normalized in _CN_DIGITS:
        value = _CN_DIGITS[normalized]
        return value if value > 0 else None
    if "十" not in normalized:
        return None

    left, _, right = normalized.partition("十")
    tens = 1 if not left else _CN_DIGITS.get(left)
    ones = 0 if not right else _CN_DIGITS.get(right)
    if tens is None or ones is None:
        return None
    value = tens * 10 + ones
    return value if value > 0 else None


def parse_ordinal_reference(value: object) -> OrdinalReference | None:
    """识别“第三个/选3/3/倒数第二个/最后一个”等选择表达。"""
    if not isinstance(value, str):
        return None
    text = unicodedata.normalize("NFKC", value).strip()
    if not text:
        return None

    reverse = re.search(rf"倒数第\s*({_ORDINAL_TOKEN})\s*{_OPTION_NOUN}", text)
    if reverse:
        number = _parse_number_token(reverse.group(1))
        return OrdinalReference(-number, reverse.group(0)) if number else None

    last = re.search(r"最后(?:一)?(?:个|项|条|台|艘|种|号|款|组|位|套|那个)", text)
    if last:
        return OrdinalReference(-1, last.group(0))

    ordinal = re.search(rf"第\s*({_ORDINAL_TOKEN})\s*{_OPTION_NOUN}", text)
    if ordinal:
        number = _parse_number_token(ordinal.group(1))
        return OrdinalReference(number, ordinal.group(0)) if number else None

    selected_number = re.search(
        rf"(?:选(?:择)?|采用|使用|用|要|按|定)\s*({_ORDINAL_TOKEN})\s*{_OPTION_NOUN}",
        text,
    )
    if selected_number:
        number = _parse_number_token(selected_number.group(1))
        return OrdinalReference(number, selected_number.group(0)) if number else None

    bare = re.fullmatch(rf"\s*({_ORDINAL_TOKEN})\s*{_OPTION_NOUN}\s*[。.!！]?\s*", text)
    if bare:
        number = _parse_number_token(bare.group(1))
        return OrdinalReference(number, bare.group(0)) if number else None
    return None


def build_candidate_terms(field_definition: Mapping[str, object]) -> dict[str, set[str]]:
    """按标准值构造唯一可见名称集合，排除配置声明的歧义 alias。"""
    allowed_values = [
        value
        for value in (field_definition.get("allowed_values") or [])
        if isinstance(value, str) and value.strip()
    ]
    terms = {value: {value} for value in allowed_values}
    ambiguous_keys = {
        FieldNormalizer.make_match_key(alias)
        for alias in (field_definition.get("ambiguous_aliases") or {})
    }

    for alias, canonical in (field_definition.get("alias_mappings") or {}).items():
        if canonical in terms and FieldNormalizer.make_match_key(alias) not in ambiguous_keys:
            terms[canonical].add(str(alias))

    for evidence in field_definition.get("candidate_evidence") or []:
        if not isinstance(evidence, dict):
            continue
        canonical = evidence.get("canonical_value")
        if canonical not in terms:
            continue
        display_name = evidence.get("display_name")
        if (
            isinstance(display_name, str)
            and FieldNormalizer.make_match_key(display_name) not in ambiguous_keys
        ):
            terms[canonical].add(display_name)
    return terms


def _extract_numbered_entries(assistant_text: str) -> dict[int, str]:
    entries: dict[int, str] = {}
    duplicates: set[int] = set()
    for raw_line in str(assistant_text or "").splitlines():
        line = raw_line.replace("**", "").replace("__", "")
        match = _NUMBERED_LINE.match(line)
        if not match:
            continue
        number = _parse_number_token(match.group("number"))
        if number is None:
            continue
        if number in entries:
            duplicates.add(number)
            continue
        entries[number] = match.group("body").strip().rstrip("|").strip()
    for number in duplicates:
        entries.pop(number, None)
    return entries


def _term_occurs_in_body(term: str, body_key: str) -> bool:
    term_key = FieldNormalizer.make_match_key(term)
    if not term_key:
        return False
    if len(term_key) == 1:
        return body_key == term_key
    return term_key in body_key


def visible_ordinal_matches_candidate(
    assistant_text: str,
    reference: OrdinalReference,
    selected_value: str,
    candidate_terms: Mapping[str, Iterable[str]],
) -> bool:
    """校验目标编号行只对应模型选择的同一标准候选。"""
    entries = _extract_numbered_entries(assistant_text)
    if not entries:
        return False

    position = reference.position
    if position < 0:
        ordered_numbers = sorted(entries)
        if ordered_numbers != list(range(1, len(ordered_numbers) + 1)):
            return False
        offset = len(ordered_numbers) + position
        if offset < 0:
            return False
        position = ordered_numbers[offset]

    body = entries.get(position)
    if body is None:
        return False
    body_key = FieldNormalizer.make_match_key(body)
    matched_values = {
        canonical
        for canonical, terms in candidate_terms.items()
        if any(_term_occurs_in_body(str(term), body_key) for term in terms)
    }
    return matched_values == {selected_value}
