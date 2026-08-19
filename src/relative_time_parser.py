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

def parse_cn_number_str(s: str | None) -> int | None:
    """将阿拉伯数字字符串或中文数字转为 int。"""
    if not s or not isinstance(s, str):
        return None
    s = s.strip()
    if s.isdigit():
        return int(s)
    try:
        import cn2an
        return int(cn2an.cn2an(s, "smart"))
    except Exception:
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
    确定性解析口语相对日期与时间点。全量支持中文数字转换(cn2an)与完整用户句法上下文。
    """
    if not text or not isinstance(text, str):
        text = ""

    # 1. 使用 cn2an 将文本及上下文中的中文数字统一预转化为阿拉伯数字 (如 "上午十一点" -> "上午11点")
    norm_raw = unicodedata.normalize("NFKC", text).strip()
    full_raw = unicodedata.normalize("NFKC", full_user_message or "").strip()

    try:
        import cn2an
        norm = cn2an.transform(norm_raw, "cn2an") if norm_raw else ""
        full_msg = cn2an.transform(full_raw, "cn2an") if full_raw else ""
    except Exception:
        norm = norm_raw
        full_msg = full_raw

    if not norm:
        return None

    # 如果已经是完整的 ISO 绝对时间字符串（如 2026-08-20T10:00:00 或 2026/08/20），不作为相对口语表达处理
    if norm and re.match(r"^\d{4}[-/]\d{2}[-/]\d{2}", norm):
        return None

    if base_dt is None:
        from .simulated_time import get_current_datetime
        base_dt = get_current_datetime()

    # 1.5 检查相对时间偏移 (如 "五小时后", "2小时后", "30分钟后")
    m_offset_h = re.search(r"([0-9]+|[一二两三四五六七八九十]+)\s*(?:个)?\s*(?:小时|钟头)\s*后", norm)
    if m_offset_h:
        h_num = parse_cn_number_str(m_offset_h.group(1))
        if h_num:
            target_dt = base_dt + timedelta(hours=h_num)
            return target_dt.strftime("%Y-%m-%dT%H:%M:%S")

    m_offset_m = re.search(r"([0-9]+|[一二两三四五六七八九十]+)\s*(?:个)?\s*(?:分钟|分)\s*后", norm)
    if m_offset_m:
        m_num = parse_cn_number_str(m_offset_m.group(1))
        if m_num:
            target_dt = base_dt + timedelta(minutes=m_num)
            return target_dt.strftime("%Y-%m-%dT%H:%M:%S")

    # 2. 求解日期 target_date
    target_date = extract_explicit_date_from_text(norm, base_dt) if norm else None

    # 若候选短语本身不含日期，但整句 full_msg 中含有明确月日/相对日期，优先使用整句中的日期
    if target_date is None and full_msg:
        target_date = extract_explicit_date_from_text(full_msg, base_dt, is_full_message=True)

    # 若含有明确的 ISO 日期前缀 (如 2026-08-14T17:30:00)，保留该原日期
    if target_date is None and norm:
        m_iso = re.search(r"(\d{4}[-/]\d{2}[-/]\d{2})", norm)
        if m_iso:
            try:
                target_date = datetime.fromisoformat(m_iso.group(1).replace("/", "-")).date()
            except ValueError:
                pass

    # 检查句中是否有显式时间点（如 "11点", "11:00", "上午十一点"）
    combined_search_text = f"{norm} {full_msg}".strip()

    has_time_in_text = bool(
        re.search(r"([0-2]?[0-9])[:：]([0-5][0-9])", combined_search_text)
        or re.search(r"([0-2]?[0-9])\s*(?:点|时)", combined_search_text)
        or re.search(r"现在|当前|立即|此时", combined_search_text)
    )

    if target_date is None:
        if has_time_in_text:
            target_date = base_dt.date()
        else:
            # 尝试第三方 dateparser 兜底提取日期
            try:
                import dateparser
                dp_res = dateparser.parse(combined_search_text, settings={"RELATIVE_BASE": base_dt})
                if dp_res:
                    target_date = dp_res.date()
            except Exception:
                pass

    if target_date is None:
        return None

    # 3. 求解时间点 (小时与分钟)
    hour = 0
    minute = 0
    second = 0
    has_explicit_time = False

    # 优先在 norm 局部寻找时间点，找不到则在 combined_search_text 全局寻找
    search_target = norm if bool(re.search(r"(?:[0-2]?[0-9]\s*(?:点|时|[:：]))|现在|当前", norm)) else combined_search_text

    # 3.1 检查是否有 ISO 格式 (如 23:00 / 17:30:00 / 09:15)
    m_iso_time = re.search(r"([0-2]?[0-9])[:：]([0-5][0-9])(?:[:：]([0-5][0-9]))?", search_target)
    if m_iso_time:
        has_explicit_time = True
        hour = int(m_iso_time.group(1))
        minute = int(m_iso_time.group(2))
        if m_iso_time.group(3):
            second = int(m_iso_time.group(3))
    else:
        # 3.2 匹配口语小时点 (如 上午11点 / 下午3点半 / 晚上11点 / 8点30分 / 凌晨2点)
        is_pm = bool(re.search(r"下午|晚上|傍晚|夜里|夜间|午后|晚", search_target))
        is_am = bool(re.search(r"上午|早上|早晨|凌晨|清晨|早", search_target))

        m_clock = re.search(r"([0-2]?[0-9])\s*(?:点|时)", search_target)
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
            if re.search(r"点半|时半", search_target):
                minute = 30
            else:
                m_min = re.search(r"(?:点|时)\s*([0-5]?[0-9])\s*(?:分|分钟)?", search_target)
                if m_min and m_min.group(1):
                    minute = int(m_min.group(1))

    if not has_explicit_time and re.search(r"现在|当前|立即|此时", search_target):
        hour = base_dt.hour
        minute = base_dt.minute
        second = base_dt.second

    hour = min(23, max(0, hour))
    minute = min(59, max(0, minute))
    second = min(59, max(0, second))

    return f"{target_date.strftime('%Y-%m-%d')}T{hour:02d}:{minute:02d}:{second:02d}"
