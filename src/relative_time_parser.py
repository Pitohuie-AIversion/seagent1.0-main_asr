"""
relative_time_parser.py — 中文口语相对日期与时间点确定性解析器
基于基准时间（base_dt），解析“明天”、“后天”、“大后天”、“下周一”等口语日期，
并结合“下午3点”、“晚上11点”等时间点，输出标准 YYYY-MM-DDTHH:MM:SS。
"""

import math
import re
import unicodedata
from datetime import datetime, timedelta

WEEKDAY_MAP = {
    "一": 0, "1": 0,
    "二": 1, "2": 1,
    "三": 2, "3": 2,
    "四": 3, "4": 3,
    "五": 4, "5": 4,
    "六": 5, "6": 5,
    "日": 6, "天": 6, "7": 6, "0": 6,
}


def parse_relative_datetime(text: str | None, base_dt: datetime | None = None) -> str | None:
    """
    确定性解析口语相对日期与时间点。
    若无法识别相对日期表达，返回 None。
    """
    if not text or not isinstance(text, str):
        return None

    norm = unicodedata.normalize("NFKC", text).strip()
    if not norm:
        return None

    # 如果已经是完整的 ISO 绝对时间字符串（如 2026-08-20T10:00:00 或 2026/08/20），不作为相对口语表达处理
    if re.match(r"^\d{4}[-/]\d{2}[-/]\d{2}", norm):
        return None

    if base_dt is None:
        from .simulated_time import get_current_datetime
        base_dt = get_current_datetime()

    target_date = None

    # 1. 显式具体日期判断 (如 8月31号 / 8月31日 / 2026年8月31日 / 8-31)
    m_exact = re.search(
        r"(?:(\d{4})[年/-])?\s*([0-1]?[0-9])[月/-]\s*([0-3]?[0-9])[日号]?",
        norm,
    )
    if m_exact:
        try:
            year = int(m_exact.group(1)) if m_exact.group(1) else base_dt.year
            month = int(m_exact.group(2))
            day = int(m_exact.group(3))
            if 1 <= month <= 12 and 1 <= day <= 31:
                target_date = datetime(year, month, day).date()
        except ValueError:
            target_date = None

    # 2. 相对日期判断
    if target_date is None:
        if re.search(r"大后天", norm):
            target_date = base_dt.date() + timedelta(days=3)
        elif re.search(r"后天|后晚|后早", norm):
            target_date = base_dt.date() + timedelta(days=2)
        elif re.search(r"明天|明晚|明早|明个", norm):
            target_date = base_dt.date() + timedelta(days=1)
        elif re.search(r"今天|今晚|今早|现在|当前", norm):
            target_date = base_dt.date()
        else:
            # 下周X / 本周X / 这周X
            m_week = re.search(r"(下周|本周|这周)\s*([一二三四五六日天12345670])", norm)
            if m_week:
                prefix = m_week.group(1)
                day_str = m_week.group(2)
                target_weekday = WEEKDAY_MAP.get(day_str)
                if target_weekday is not None:
                    current_weekday = base_dt.weekday()
                    if prefix == "下周":
                        days_ahead = (target_weekday - current_weekday) + 7
                        target_date = base_dt.date() + timedelta(days=days_ahead)
                    else:  # 本周 / 这周
                        days_diff = target_weekday - current_weekday
                        target_date = base_dt.date() + timedelta(days=days_diff)

    if target_date is None:
        has_month_or_day = bool(re.search(r"[0-9一二三四五六七八九十]+\s*[月日号]", norm))
        if not has_month_or_day:
            has_time = bool(
                re.search(r"([0-2]?[0-9])[:：]([0-5][0-9])", norm)
                or re.search(r"([0-2]?[0-9])\s*(?:点|时)", norm)
                or re.search(r"现在|当前|立即|此时", norm)
            )
            if has_time:
                target_date = base_dt.date()

    if target_date is None:
        return None

    # 2. 时间点解析 (小时与分钟)
    hour = 0
    minute = 0
    second = 0
    has_explicit_time = False

    # 检查是否有显式 ISO 或 24h 时间点模式 (如 23:00 / 17:30:00 / 09:15)
    m_iso_time = re.search(r"([0-2]?[0-9])[:：]([0-5][0-9])(?:[:：]([0-5][0-9]))?", norm)
    if m_iso_time:
        has_explicit_time = True
        hour = int(m_iso_time.group(1))
        minute = int(m_iso_time.group(2))
        if m_iso_time.group(3):
            second = int(m_iso_time.group(3))
    else:
        # 匹配口语小时点 (如 下午3点半 / 晚上11点 / 8点30分 / 凌晨2点)
        is_pm = bool(re.search(r"下午|晚上|傍晚|夜里|夜间|午后|晚", norm))
        is_am = bool(re.search(r"上午|早上|早晨|凌晨|清晨|早", norm))

        m_clock = re.search(r"([0-2]?[0-9])\s*(?:点|时)", norm)
        if m_clock:
            has_explicit_time = True
            h_raw = int(m_clock.group(1))
            if is_pm and h_raw < 12:
                hour = h_raw + 12
            elif is_am and h_raw == 12:
                hour = 0
            else:
                hour = h_raw

            # 匹配分钟 / 半
            if re.search(r"点半|时半", norm):
                minute = 30
            else:
                m_min = re.search(r"(?:点|时)\s*([0-5]?[0-9])\s*(?:分|分钟)?", norm)
                if m_min and m_min.group(1):
                    minute = int(m_min.group(1))

    if not has_explicit_time and re.search(r"现在|当前|立即|此时", norm):
        hour = base_dt.hour
        minute = base_dt.minute
        second = base_dt.second

    hour = min(23, max(0, hour))
    minute = min(59, max(0, minute))
    second = min(59, max(0, second))

    return f"{target_date.strftime('%Y-%m-%d')}T{hour:02d}:{minute:02d}:{second:02d}"
