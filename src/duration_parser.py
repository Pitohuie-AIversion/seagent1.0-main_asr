"""
duration_parser.py — 中文与常用口语时长确定性解析器
将自然语言时长文本（如“两个半小时”、“45分钟”、“2.5小时”）精准解析为正数秒。
"""

import math
import re
import unicodedata

CN_DIGITS = {
    "零": 0,
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


def parse_chinese_number(text: str) -> float | None:
    """将阿拉伯数字或简单中文数字（如 '2', '2.5', '两', '十二', '二十五'）转为 float。"""
    if not text:
        return None
    s = text.strip()
    # 尝试直接转 float
    try:
        val = float(s)
        return val if math.isfinite(val) else None
    except ValueError:
        pass

    if s == "半":
        return 0.5

    # 中文数字解析 (支持 1-99，以及带小数字如 '二点五')
    if "点" in s:
        parts = s.split("点", 1)
        int_part = parse_chinese_number(parts[0])
        if int_part is None:
            return None
        dec_str = parts[1]
        dec_val = 0.0
        weight = 0.1
        for ch in dec_str:
            if ch in CN_DIGITS:
                dec_val += CN_DIGITS[ch] * weight
                weight *= 0.1
            elif ch.isdigit():
                dec_val += int(ch) * weight
                weight *= 0.1
            else:
                return None
        return int_part + dec_val

    # 纯整数解析
    total = 0
    curr = 0
    has_digits = False
    i = 0
    while i < len(s):
        ch = s[i]
        if ch in CN_DIGITS:
            curr = CN_DIGITS[ch]
            has_digits = True
        elif ch == "十":
            has_digits = True
            if curr == 0:
                total += 10
            else:
                total += curr * 10
                curr = 0
        elif ch == "百":
            has_digits = True
            if curr == 0:
                total += 100
            else:
                total += curr * 100
                curr = 0
        elif ch.isdigit():
            # 混合数字
            curr = int(ch)
            has_digits = True
        else:
            return None
        i += 1
    total += curr
    return float(total) if has_digits else None


def parse_duration_to_seconds(text: str | None) -> float | None:
    """
    解析中文或英文自然语言中的时长表达，返回秒数。
    若无法确定性解析，返回 None。
    """
    if not text or not isinstance(text, str):
        return None

    norm = unicodedata.normalize("NFKC", text).strip()
    if not norm:
        return None

    # 清除常见前缀与后缀干扰词
    cleaned = re.sub(r"^(?:持续|大概|约|预估|用时|大约|预计|作业|需要|共|耗时|用|历时|时长|时间为?)+", "", norm).strip()
    cleaned = re.sub(r"(?:左右|上下|即可|时间|以内|之内|左右的时间|前后)+$", "", cleaned).strip()

    if not cleaned:
        return None

    # 1. 独立半小时 / 半个钟头 / 半天
    if re.fullmatch(r"半\s*(?:个)?\s*(?:小时|钟头|时)", cleaned):
        return 1800.0
    if re.fullmatch(r"半\s*(?:个)?\s*天", cleaned):
        return 43200.0
    if re.fullmatch(r"半\s*(?:个)?\s*(?:分钟|分)", cleaned):
        return 30.0

    # 2. X个半小时 / X小时半 (如 "两个半小时", "2个半小时", "两小时半", "2小时半")
    m = re.search(r"([0-9]+(?:\.[0-9]+)?|[零一二两三四五六七八九十百点]+)\s*(?:个)?\s*半\s*(?:个)?\s*(?:小时|钟头|h|hr|hours?|hrs?)", cleaned, re.IGNORECASE)
    if m:
        num = parse_chinese_number(m.group(1))
        if num is not None and num > 0:
            return (num + 0.5) * 3600.0

    m = re.search(r"([0-9]+(?:\.[0-9]+)?|[零一二两三四五六七八九十百点]+)\s*(?:小时|钟头)\s*半", cleaned, re.IGNORECASE)
    if m:
        num = parse_chinese_number(m.group(1))
        if num is not None and num > 0:
            return (num + 0.5) * 3600.0

    # 3. X天半 / X个半天
    m = re.search(r"([0-9]+(?:\.[0-9]+)?|[零一二两三四五六七八九十百点]+)\s*(?:个)?\s*半\s*(?:个)?\s*天", cleaned, re.IGNORECASE)
    if m:
        num = parse_chinese_number(m.group(1))
        if num is not None and num > 0:
            return (num + 0.5) * 86400.0

    m = re.search(r"([0-9]+(?:\.[0-9]+)?|[零一二两三四五六七八九十百点]+)\s*天\s*半", cleaned, re.IGNORECASE)
    if m:
        num = parse_chinese_number(m.group(1))
        if num is not None and num > 0:
            return (num + 0.5) * 86400.0

    # 4. 组合: X小时 Y分钟 (或单一 X小时 / X分钟)
    hour_val = 0.0
    min_val = 0.0
    sec_val = 0.0
    day_val = 0.0
    matched = False

    # 检查天
    day_match = re.search(r"([0-9]+(?:\.[0-9]+)?|[零一二两三四五六七八九十百点]+)\s*(?:天|日|days?)", cleaned, re.IGNORECASE)
    if day_match:
        d = parse_chinese_number(day_match.group(1))
        if d is not None:
            day_val = d
            matched = True

    # 检查小时
    hour_match = re.search(r"([0-9]+(?:\.[0-9]+)?|[零一二两三四五六七八九十百点]+)\s*(?:个)?\s*(?:小时|钟头|h|hr|hours?|hrs?)", cleaned, re.IGNORECASE)
    if hour_match:
        h = parse_chinese_number(hour_match.group(1))
        if h is not None:
            hour_val = h
            matched = True

    # 检查分钟
    min_match = re.search(r"([0-9]+(?:\.[0-9]+)?|[零一二两三四五六七八九十百点]+)\s*(?:个)?\s*(?:分钟|分|mins?|minutes?)", cleaned, re.IGNORECASE)
    if min_match:
        mn = parse_chinese_number(min_match.group(1))
        if mn is not None:
            min_val = mn
            matched = True

    # 检查秒
    sec_match = re.search(r"([0-9]+(?:\.[0-9]+)?|[零一二两三四五六七八九十百点]+)\s*(?:个)?\s*(?:秒钟|秒|secs?|seconds?)", cleaned, re.IGNORECASE)
    if sec_match:
        s = parse_chinese_number(sec_match.group(1))
        if s is not None:
            sec_val = s
            matched = True

    if matched:
        total_seconds = day_val * 86400.0 + hour_val * 3600.0 + min_val * 60.0 + sec_val
        if total_seconds > 0 and math.isfinite(total_seconds):
            return total_seconds

    return None
