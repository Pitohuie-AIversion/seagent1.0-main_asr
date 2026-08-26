"""
duration_parser.py — 中文与常用口语时长确定性解析器 v2.0
将自然语言时长文本（如"两个半小时"、"45分钟"、"2.5小时"）精准解析为正数秒。

核心改进（v2.0）：
1. 内置零依赖中文数字解析器（不依赖 cn2an 也能工作），cn2an 可用时优先使用
2. 完整支持：中文数字、阿拉伯数字、小数、"两"等口语变体
3. 支持复合表达："1天2小时30分钟"、"一个半钟头"
4. Fail-Fast：非法或不明确输入返回 None，不做猜测
5. 可审计：提供 parse_duration_with_detail() 返回结构化解析结果
"""

import math
import re
import unicodedata
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


# ============================================================================
# 内置中文数字解析器（零依赖 fallback，确保无 cn2an 环境仍能工作）
# ============================================================================

_CN_DIGIT_MAP = {
    "零": 0, "〇": 0, "○": 0, "O": 0, "o": 0,
    "一": 1, "壹": 1, "幺": 1,
    "二": 2, "贰": 2, "两": 2, "俩": 2,
    "三": 3, "叁": 3, "仨": 3,
    "四": 4, "肆": 4,
    "五": 5, "伍": 5,
    "六": 6, "陆": 6,
    "七": 7, "柒": 7, "拐": 7,
    "八": 8, "捌": 8,
    "九": 9, "玖": 9, "勾": 9,
}

_CN_UNIT_MAP = {
    "十": 10, "拾": 10,
    "百": 100, "佰": 100,
    "千": 1000, "仟": 1000,
    "万": 10000, "萬": 10000,
    "亿": 100000000, "億": 100000000,
}


def _parse_cn_integer(text: str) -> Optional[int]:
    """确定性中文整数解析：支持"十"到"九千九百九十九万"，以及"两百"、"三十"等简写。"""
    if not text or not isinstance(text, str):
        return None
    s = text.strip()
    if not s:
        return None

    # 纯阿拉伯数字快速路径
    if s.isdigit():
        try:
            return int(s)
        except ValueError:
            return None

    total = 0
    section = 0  # 当前"万/亿"区段累计
    current = 0  # 当前区段内数值
    last_unit = 1  # 上一个单位大小

    for ch in s:
        if ch in _CN_DIGIT_MAP:
            d = _CN_DIGIT_MAP[ch]
            if current == 0 and d == 0:
                continue
            current = current * 10 + d if last_unit <= 1 else d
            last_unit = 1
        elif ch in _CN_UNIT_MAP:
            unit = _CN_UNIT_MAP[ch]
            if unit >= 10000:
                # 遇到万/亿，先把 current 加入 section，再把 section 进位
                section += current if current != 0 else 1
                section *= unit
                total += section
                section = 0
                current = 0
            else:
                # 十/百/千：如果 current 为 0（如"十一"=11、"二百"=200），默认补 1
                base = current if current != 0 else 1
                section += base * unit
                current = 0
            last_unit = unit
        else:
            return None  # 遇到非法字符，整体失败

    total += section + current
    return total if total >= 0 else None


def parse_chinese_number(text: str) -> Optional[float]:
    """将阿拉伯数字或中文数字转为 float。零依赖但会优先尝试 cn2an。"""
    if not text or not isinstance(text, str):
        return None
    s = text.strip()
    if not s:
        return None

    if s == "半":
        return 0.5

    # 1. 纯阿拉伯数字（含小数）快速路径
    if re.fullmatch(r"[-+]?\d+(?:\.\d+)?", s):
        try:
            val = float(s)
            return val if math.isfinite(val) else None
        except ValueError:
            return None

    # 2. 尝试 cn2an（如果已安装，支持更复杂的"二点五"等表达）
    try:
        import cn2an  # type: ignore
        val = float(cn2an.cn2an(s, "smart"))
        return val if math.isfinite(val) else None
    except Exception:
        pass

    # 3. 内置 fallback：中文整数 + 可选"点"小数
    if "点" in s or "." in s:
        sep = "点" if "点" in s else "."
        parts = s.split(sep, 1)
        int_part = _parse_cn_integer(parts[0])
        if int_part is None:
            return None
        frac_str = parts[1]
        if frac_str == "半":
            return float(int_part) + 0.5
        # 小数部分逐位解析
        frac_val = 0.0
        for i, ch in enumerate(frac_str, 1):
            d = _CN_DIGIT_MAP.get(ch)
            if d is None:
                if ch.isdigit():
                    d = int(ch)
                else:
                    return None
            frac_val += d / (10 ** i)
        result = float(int_part) + frac_val
        return result if math.isfinite(result) else None

    # 4. 纯中文整数
    val = _parse_cn_integer(s)
    return float(val) if val is not None and math.isfinite(val) else None


# ============================================================================
# 时长解析核心
# ============================================================================

@dataclass
class DurationParseResult:
    """结构化解析结果，方便审计和回归测试。"""
    total_seconds: Optional[float] = None
    days: float = 0.0
    hours: float = 0.0
    minutes: float = 0.0
    seconds: float = 0.0
    raw_text: str = ""
    normalized_text: str = ""
    parse_method: str = "failed"

    @property
    def success(self) -> bool:
        return (
            self.total_seconds is not None
            and math.isfinite(self.total_seconds)
            and self.total_seconds > 0
        )


def _clean_duration_text(text: str) -> str:
    """统一 Unicode 形式，移除前后缀干扰词。"""
    norm = unicodedata.normalize("NFKC", text).strip()
    if not norm:
        return ""
    cleaned = re.sub(
        r"^(?:持续|大概|约|预估|用时|大约|预计|作业|需要|共|耗时|用|历时|时长|时间为?)+",
        "", norm,
    ).strip()
    cleaned = re.sub(
        r"(?:左右|上下|即可|时间|以内|之内|左右的时间|前后)+$",
        "", cleaned,
    ).strip()
    return cleaned


def parse_duration_with_detail(text: Optional[str]) -> DurationParseResult:
    """带结构化审计信息的时长解析。"""
    result = DurationParseResult(raw_text=str(text or ""))

    if not text or not isinstance(text, str):
        return result

    cleaned = _clean_duration_text(text)
    result.normalized_text = cleaned
    if not cleaned:
        return result

    # 负数或显式前导负号 —— 非法时长，直接失败
    if cleaned.startswith("-") or cleaned.startswith("负"):
        return result

    # --- 独立短句快速匹配（最高优先级） ---
    # 半小时 / 半个钟头 / 半天 / 半分钟
    if re.fullmatch(r"半\s*(?:个)?\s*(?:小时|钟头|时|h|hr|hours?|hrs?)", cleaned, re.IGNORECASE):
        result.hours = 0.5
        result.total_seconds = 1800.0
        result.parse_method = "quick_half_hour"
        return result
    if re.fullmatch(r"半\s*(?:个)?\s*天", cleaned, re.IGNORECASE):
        result.days = 0.5
        result.total_seconds = 43200.0
        result.parse_method = "quick_half_day"
        return result
    if re.fullmatch(r"半\s*(?:个)?\s*(?:分钟|分|min|mins?|minutes?)", cleaned, re.IGNORECASE):
        result.minutes = 0.5
        result.total_seconds = 30.0
        result.parse_method = "quick_half_minute"
        return result

    # --- "X个半小时" / "X小时半" / "X天半" / "X个半天" ---
    def _try_pattern(pattern_regex: re.Pattern, unit_multiplier: float) -> Optional[float]:
        m = pattern_regex.search(cleaned)
        if m:
            num = parse_chinese_number(m.group(1))
            if num is not None and num > 0 and math.isfinite(num):
                return (num + 0.5) * unit_multiplier
        return None

    p_hour_half = re.compile(
        r"([0-9]+(?:\.[0-9]+)?|[零一二两三四五六七八九十百千万亿点贰叁肆伍陆柒捌玖两佰仟万亿]+)\s*(?:个)?\s*半\s*(?:个)?\s*(?:小时|钟头|h|hr|hours?|hrs?)",
        re.IGNORECASE,
    )
    val = _try_pattern(p_hour_half, 3600.0)
    if val is not None:
        result.hours = val / 3600.0
        result.total_seconds = val
        result.parse_method = "hour_and_half_prefix"
        return result

    p_hour_half2 = re.compile(
        r"([0-9]+(?:\.[0-9]+)?|[零一二两三四五六七八九十百千万亿点贰叁肆伍陆柒捌玖两佰仟万亿]+)\s*(?:小时|钟头)\s*半",
        re.IGNORECASE,
    )
    val = _try_pattern(p_hour_half2, 3600.0)
    if val is not None:
        result.hours = val / 3600.0
        result.total_seconds = val
        result.parse_method = "hour_and_half_suffix"
        return result

    p_day_half = re.compile(
        r"([0-9]+(?:\.[0-9]+)?|[零一二两三四五六七八九十百千万亿点贰叁肆伍陆柒捌玖两佰仟万亿]+)\s*(?:个)?\s*半\s*(?:个)?\s*天",
        re.IGNORECASE,
    )
    val = _try_pattern(p_day_half, 86400.0)
    if val is not None:
        result.days = val / 86400.0
        result.total_seconds = val
        result.parse_method = "day_and_half_prefix"
        return result

    p_day_half2 = re.compile(
        r"([0-9]+(?:\.[0-9]+)?|[零一二两三四五六七八九十百千万亿点贰叁肆伍陆柒捌玖两佰仟万亿]+)\s*天\s*半",
        re.IGNORECASE,
    )
    val = _try_pattern(p_day_half2, 86400.0)
    if val is not None:
        result.days = val / 86400.0
        result.total_seconds = val
        result.parse_method = "day_and_half_suffix"
        return result

    # --- 组合式：X天Y小时Z分钟W秒（或任一字段单独出现） ---
    matched = False
    tmp_days = 0.0
    tmp_hours = 0.0
    tmp_minutes = 0.0
    tmp_seconds = 0.0

    # 天
    day_match = re.search(
        r"([0-9]+(?:\.[0-9]+)?|[零一二两三四五六七八九十百千万亿点贰叁肆伍陆柒捌玖两佰仟万亿]+)\s*(?:天|日|days?)",
        cleaned, re.IGNORECASE,
    )
    if day_match:
        d = parse_chinese_number(day_match.group(1))
        if d is not None and math.isfinite(d):
            tmp_days = d
            matched = True

    # 小时
    hour_match = re.search(
        r"([0-9]+(?:\.[0-9]+)?|[零一二两三四五六七八九十百千万亿点贰叁肆伍陆柒捌玖两佰仟万亿]+)\s*(?:个)?\s*(?:小时|钟头|h|hr|hours?|hrs?)",
        cleaned, re.IGNORECASE,
    )
    if hour_match:
        h = parse_chinese_number(hour_match.group(1))
        if h is not None and math.isfinite(h):
            tmp_hours = h
            matched = True

    # 分钟
    min_match = re.search(
        r"([0-9]+(?:\.[0-9]+)?|[零一二两三四五六七八九十百千万亿点贰叁肆伍陆柒捌玖两佰仟万亿]+)\s*(?:个)?\s*(?:分钟|分|mins?|minutes?)",
        cleaned, re.IGNORECASE,
    )
    if min_match:
        mn = parse_chinese_number(min_match.group(1))
        if mn is not None and math.isfinite(mn):
            tmp_minutes = mn
            matched = True

    # 秒
    sec_match = re.search(
        r"([0-9]+(?:\.[0-9]+)?|[零一二两三四五六七八九十百千万亿点贰叁肆伍陆柒捌玖两佰仟万亿]+)\s*(?:个)?\s*(?:秒钟|秒|secs?|seconds?)",
        cleaned, re.IGNORECASE,
    )
    if sec_match:
        sc = parse_chinese_number(sec_match.group(1))
        if sc is not None and math.isfinite(sc):
            tmp_seconds = sc
            matched = True

    if matched:
        total = (
            tmp_days * 86400.0
            + tmp_hours * 3600.0
            + tmp_minutes * 60.0
            + tmp_seconds
        )
        if math.isfinite(total) and total > 0:
            result.days = tmp_days
            result.hours = tmp_hours
            result.minutes = tmp_minutes
            result.seconds = tmp_seconds
            result.total_seconds = total
            result.parse_method = "composite_fields"
            return result

    return result


def parse_duration_to_seconds(text: Optional[str]) -> Optional[float]:
    """兼容旧接口：仅返回秒数，失败时返回 None。"""
    detail = parse_duration_with_detail(text)
    return detail.total_seconds if detail.success else None


# ============================================================================
# 保持时长不变的意图识别
# ============================================================================

KEEP_DURATION_PATTERNS = [
    r"持续时间不变",
    r"时长不变",
    r"保持持续时间",
    r"保持时长",
    r"维持原时长",
    r"维持原来?的?时长",
    r"保持原来?的?时长",
    r"时间不变",
    r"按原来?的?时长",
    r"按原时长",
    r"保持原?时长",
    r"沿用.*时长",
    r"时长.*保持.*不变",
]


def is_keep_duration_expression(text: Optional[str]) -> bool:
    """判断输入是否表达'保持原有持续时长不变'的意图。"""
    if not text or not isinstance(text, str):
        return False
    norm = unicodedata.normalize("NFKC", text).strip()
    if re.search(r"(?:开始|起始|结束|终止|截止|完工|收工)\s*时间\s*不变", norm):
        return False
    for pattern in KEEP_DURATION_PATTERNS:
        if re.search(pattern, norm):
            return True
    return False


# ============================================================================
# 持续时间语义状态：供时间区间层统一消费
# ============================================================================

class DurationState(str, Enum):
    MISSING = "missing"
    EXPLICIT = "explicit"
    KEEP = "keep"
    INVALID = "invalid"


@dataclass
class DurationSpec:
    state: DurationState = DurationState.MISSING
    raw_text: str = ""
    normalized_text: str = ""
    total_seconds: Optional[float] = None
    days: float = 0.0
    hours: float = 0.0
    minutes: float = 0.0
    seconds: float = 0.0
    parse_method: str = ""
    error_message: Optional[str] = None


def parse_duration_spec(text: Optional[str]) -> DurationSpec:
    """持续时间统一入口：区分未提供、显式时长、保持不变和非法输入。"""
    if text is None:
        return DurationSpec(state=DurationState.MISSING)

    raw = text.strip() if isinstance(text, str) else ""
    if not raw or raw.lower() in {"未提供", "无", "null", "none"}:
        return DurationSpec(state=DurationState.MISSING, raw_text=raw)

    if is_keep_duration_expression(raw) or raw == "不变":
        return DurationSpec(
            state=DurationState.KEEP,
            raw_text=raw,
            parse_method="keep_duration",
        )

    detail = parse_duration_with_detail(raw)
    if detail.success:
        return DurationSpec(
            state=DurationState.EXPLICIT,
            raw_text=detail.raw_text,
            normalized_text=detail.normalized_text,
            total_seconds=detail.total_seconds,
            days=detail.days,
            hours=detail.hours,
            minutes=detail.minutes,
            seconds=detail.seconds,
            parse_method=detail.parse_method,
        )

    return DurationSpec(
        state=DurationState.INVALID,
        raw_text=raw,
        error_code="INVALID_DURATION",
        error_message="无法解析持续时间",
    )


def format_duration_template(total_seconds: float) -> str:
    """按协议模板输出规范化持续时间，如 0日2时30分。"""
    if not math.isfinite(total_seconds) or total_seconds < 0:
        raise ValueError("total_seconds must be a non-negative finite number")
    whole_seconds = int(round(total_seconds))
    days, rem = divmod(whole_seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    if seconds:
        return f"{days}日{hours}时{minutes}分{seconds}秒"
    return f"{days}日{hours}时{minutes}分"
