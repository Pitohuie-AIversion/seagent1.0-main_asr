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


_ORDINAL_DIGITS = r"(?:[1-9]\d?|[零〇一二两三四五六七八九十]{1,3})"
_OPTION_NOUN_EXPLICIT = r"(?:个|项|条|台|艘|种|款|组|位|套)"
_OPTION_NOUN_OPTIONAL = r"(?:个|项|条|台|艘|种|号|款|组|位|套)?"
_MODAL_OR_END = r"(?:[吧啦了哈呗嘛噢哦呀呢\s.。!！,，~～]|$)"


def parse_ordinal_reference(value: object) -> OrdinalReference | None:
    """识别“第三个/选3/3/倒数第二个/最后一个”等序号选择表达。

    严格区分列表序号（如“选1”、“第2个”）与实际规格型号/编号（如“选择150HP”、“LROV-150-001”），
    避免将带单位、字母或多位实体数值误判为序号引用。
    """
    if not isinstance(value, str):
        return None
    text = unicodedata.normalize("NFKC", value).strip()
    if not text:
        return None

    # 1. 倒数第 N 项 (如: 倒数第二个, 倒数第1项)
    reverse = re.search(
        rf"倒数第\s*({_ORDINAL_DIGITS})\s*{_OPTION_NOUN_OPTIONAL}(?!\d|[a-zA-Z])",
        text,
    )
    if reverse:
        number = _parse_number_token(reverse.group(1))
        if number:
            return OrdinalReference(-number, reverse.group(0))

    # 2. 最后一项 / 倒数第一
    last = re.search(r"最后(?:一)?(?:个|项|条|台|艘|种|号|款|组|位|套|那个)", text)
    if last:
        return OrdinalReference(-1, last.group(0))

    # 3. 带“第”的序数表达 (如: 第2个, 第1项; 若无量词如“第2”，其后不能接字母、数字或实体字)
    ordinal_with_noun = re.search(
        rf"第\s*({_ORDINAL_DIGITS})\s*{_OPTION_NOUN_EXPLICIT}(?!\d|[a-zA-Z])",
        text,
    )
    if ordinal_with_noun:
        number = _parse_number_token(ordinal_with_noun.group(1))
        if number:
            return OrdinalReference(number, ordinal_with_noun.group(0))

    ordinal_bare = re.search(
        rf"(?:^|[^\w])第\s*({_ORDINAL_DIGITS})\s*{_MODAL_OR_END}",
        text,
    )
    if ordinal_bare:
        number = _parse_number_token(ordinal_bare.group(1))
        if number:
            raw = ordinal_bare.group(0).strip(" \t.。!！,，~～")
            return OrdinalReference(number, raw)

    # 4. 动词 + 序号表达 (如: 选1, 选择2, 选第3个, 选3吧, 定1)
    # 必须保证数字后没有紧跟英文字母(如150HP)、非序号数字(如150截断)或其他实体后缀
    selected_with_noun = re.search(
        rf"(?:选(?:择)?|采用|使用|用|要|按|定)\s*(?:第\s*)?({_ORDINAL_DIGITS})\s*{_OPTION_NOUN_EXPLICIT}(?!\d|[a-zA-Z])",
        text,
    )
    if selected_with_noun:
        number = _parse_number_token(selected_with_noun.group(1))
        if number:
            return OrdinalReference(number, selected_with_noun.group(0))

    selected_pure_num = re.search(
        rf"(?:选(?:择)?|采用|使用|用|要|按|定)\s*(?:第\s*)?({_ORDINAL_DIGITS})\s*{_MODAL_OR_END}",
        text,
    )
    if selected_pure_num:
        number = _parse_number_token(selected_pure_num.group(1))
        if number:
            raw = selected_pure_num.group(0).strip(" \t.。!！,，~～")
            return OrdinalReference(number, raw)

    # 5. 整句仅为纯序号表达 (如: "1", "3", "02", "第二个")
    bare = re.fullmatch(
        rf"\s*({_ORDINAL_TOKEN})\s*{_OPTION_NOUN_OPTIONAL}\s*[。.!！]?\s*",
        text,
    )
    if bare:
        # 如果是两位以上纯数字(如150)，_ORDINAL_TOKEN不会完全匹配，返回None
        number = _parse_number_token(bare.group(1))
        if number:
            return OrdinalReference(number, bare.group(0).strip())
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


def _extract_numbered_candidate_entries(
    assistant_text: str,
    candidate_terms: Mapping[str, Iterable[str]],
) -> dict[int, set[str]]:
    """逐行提取与当前候选词相关的编号项。

    返回编号到命中的标准候选值集合的映射（若某编号出现多个不同候选，或存在歧义冲突，则放入冲突集合并在最后剔除）。
    """
    entries: dict[int, set[str]] = {}
    conflicts: set[int] = set()

    for raw_line in str(assistant_text or "").splitlines():
        line = raw_line.replace("**", "").replace("__", "")
        match = _NUMBERED_LINE.match(line)
        if not match:
            continue
        number = _parse_number_token(match.group("number"))
        if number is None:
            continue

        body = match.group("body").strip().rstrip("|").strip()
        body_key = FieldNormalizer.make_match_key(body)
        matched_values = {
            canonical
            for canonical, terms in candidate_terms.items()
            if any(_term_occurs_in_body(str(term), body_key) for term in terms)
        }
        # 如果该行未命中任何当前字段的候选词，说明该编号行属于无关正文（例如载荷配置建议等），直接忽略
        if not matched_values:
            continue

        if number in entries:
            # 如果同一编号再次出现且命中了不同的候选值，说明存在歧义冲突
            if entries[number] != matched_values:
                conflicts.add(number)
            continue

        entries[number] = matched_values

    for number in conflicts:
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
    entries = _extract_numbered_candidate_entries(assistant_text, candidate_terms)
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

    matched_values = entries.get(position)
    if matched_values is None:
        return False

    return matched_values == {selected_value}
