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

CN_NUM_MAP = {
    "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}


def parse_cn_number_str(s: str | None) -> int | None:
    """将阿拉伯数字字符串或简单中文数字（如 '8', '31', '八', '十一', '二十五', '三十一'）转为 int。"""
    if not s or not isinstance(s, str):
        return None
    s = s.strip()
    if s.isdigit():
        return int(s)
    if s in CN_NUM_MAP:
        return CN_NUM_MAP[s]
    if s.startswith("十"):
        if len(s) == 1:
            return 10
        if len(s) == 2 and s[1] in CN_NUM_MAP:
            return 10 + CN_NUM_MAP[s[1]]
    if "十" in s:
        parts = s.split("十", 1)
        tens = (CN_NUM_MAP.get(parts[0]) or 1) * 10
        ones = (CN_NUM_MAP.get(parts[1]) or 0) if parts[1] else 0
        return tens + ones
    return None


def extract_explicit_date_from_text(text: str | None, base_dt: datetime | None = None, is_full_message: bool = False):
    """从句子文本中提取显式月日或相对日期。"""
    if not text or not isinstance(text, str):
        return None
    norm = unicodedata.normalize("NFKC", text).strip()
    if not norm:
        return None
    if base_dt is None:
        from .simulated_time import get_current_datetime
        base_dt = get_current_datetime()

    m_exact = re.search(
        r"(?:([0-9]{4}|[零一二三四五六七八九]{4})[年/-])?\s*([0-1]?[0-9]|[一二三四五六七八九十]+)[月/-]\s*([0-3]?[0-9]|[一二三四五六七八九十]+)[日号]?",
        norm,
    )
    if m_exact:
        try:
            year = parse_cn_number_str(m_exact.group(1)) if m_exact.group(1) else base_dt.year
            month = parse_cn_number_str(m_exact.group(2))
            day = parse_cn_number_str(m_exact.group(3))
            if month and day and 1 <= month <= 12 and 1 <= day <= 31:
                return datetime(year or base_dt.year, month, day).date()
        except ValueError:
            pass

    # 相对词检查 (强相对词优先)
    if re.search(r"大后天", norm):
        return base_dt.date() + timedelta(days=3)
    if re.search(r"后天|后晚|后早", norm):
        return base_dt.date() + timedelta(days=2)
    if re.search(r"明天|明晚|明早|明个", norm):
        return base_dt.date() + timedelta(days=1)
    if is_full_message:
        # 当从 full_user_message 检索时，不让弱词'今天'覆盖已有的特定 ISO 日期
        return None
    if re.search(r"今天|今晚|今早|现在|当前", norm):
        return base_dt.date()

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
                return base_dt.date() + timedelta(days=days_ahead)
            else:  # 本周 / 这周
                days_diff = target_weekday - current_weekday
                return base_dt.date() + timedelta(days=days_diff)

    return None


def parse_relative_datetime(
    text: str | None,
    base_dt: datetime | None = None,
    full_user_message: str | None = None,
) -> str | None:
    """
    确定性解析口语相对日期与时间点。支持完整用户句法上下文兜底与 dateparser/cn2an 引入。
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

    target_date = extract_explicit_date_from_text(norm, base_dt)

    # 若候选短语本身不含日期，但整句 full_user_message 中含有明确月日，优先使用整句中的日期
    if target_date is None and full_user_message:
        target_date = extract_explicit_date_from_text(full_user_message, base_dt, is_full_message=True)

    # 若含有明确的 ISO 日期前缀 (如 2026-08-14T17:30:00)，保留该原日期
    if target_date is None:
        m_iso = re.search(r"(\d{4}[-/]\d{2}[-/]\d{2})", norm)
        if m_iso:
            try:
                target_date = datetime.fromisoformat(m_iso.group(1).replace("/", "-")).date()
            except ValueError:
                pass

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

    # 尝试第三方 dateparser 兜底提取日期
    if target_date is None:
        try:
            import dateparser
            import cn2an
            norm_cn = cn2an.transform(norm, "smart")
            dp_res = dateparser.parse(norm_cn, settings={"RELATIVE_BASE": base_dt})
            if dp_res:
                target_date = dp_res.date()
        except Exception:
            pass

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
