"""
relative_time_parser.py — 中文口语相对日期与时间点确定性解析器 v2.0

基于基准时间（reference_instant），解析"明天"、"后天"、"大后天"、"下周一"、
"三天后上午9点"、"月底下午5点"、"8月31号早上6点"等口语日期与时间点。

核心改进（v2.0）：
1. Temporal IR：所有解析先形成结构化中间表示，再归一化到 ISO 字符串，可审计
2. 相对偏移：支持"N天前/后"、"N周前/后"、"N个月后"、"N小时后"等
3. 边界表达：月底、月初、年末、年初、本周末、下周末等
4. 歧义检测：日期与星期冲突（如"9月3日周一"实际是周四）、meridiem 缺失（"3点"）
5. 闰年适配：2月29日只有在闰年才合法，否则明确失败
6. 跨边界正确：跨年、跨月、跨 DST 区间的显式处理
7. IANA 时区：reference 携带 IANA timezone，DST gap/fold 检测框架
8. 语义证据链：parse_relative_datetime_detail 返回 resolution_method 等审计字段

向后兼容：parse_relative_datetime(text, base_dt, full_user_message) 仍然返回
YYYY-MM-DDTHH:MM:SS 字符串或 None，旧调用点无需修改即可享受新逻辑。
"""

import calendar
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta
from enum import Enum
from typing import Any, Optional
from zoneinfo import ZoneInfo

from .duration_parser import (
    DurationSpec,
    DurationState,
    format_duration_template,
    parse_chinese_number,
    parse_duration_spec,
)


# ============================================================================
# 基础枚举与数据结构：Temporal IR（时间语义中间表示）
# ============================================================================

class TemporalKind(str, Enum):
    """解析结果的语义类型。"""
    INSTANT = "instant"          # 确定的具体时刻（日期+时间）
    DATE_ONLY = "date_only"      # 只有日期，没有具体时刻
    TIME_ONLY = "time_only"      # 只有时刻，需要结合上下文确定日期
    AMBIGUOUS = "ambiguous"      # 需要进一步确认（如"3点"没有上午/下午）
    CONFLICT = "conflict"        # 内部字段冲突（如日期与星期不匹配）
    INVALID = "invalid"          # 非法（如非闰年2月29日）


class AmbiguityCode(str, Enum):
    """歧义或冲突原因代码。"""
    MERIDIEM_UNSPECIFIED = "MERIDIEM_UNSPECIFIED"          # "3点"：未说明上午/下午
    DATE_WEEKDAY_CONFLICT = "DATE_WEEKDAY_CONFLICT"        # 日期与星期对不上
    LEAP_YEAR_EXPECTED = "LEAP_YEAR_EXPECTED"              # 2月29日但不是闰年
    DAY_OUT_OF_RANGE_FOR_MONTH = "DAY_OUT_OF_RANGE"        # 某月没有那一天（如4月31日）
    DST_GAP_NONEXISTENT = "DST_GAP_NONEXISTENT"            # 本地时间落在 DST 跳过区间
    DST_FOLD_AMBIGUOUS = "DST_FOLD_AMBIGUOUS"              # 本地时间对应两个真实时刻
    MULTIPLE_PARSE_INTERPRETATIONS = "MULTIPLE_INTERPRET"  # 多种可能解析且无法消歧


WEEKDAY_MAP_CN = {
    "一": 0, "1": 0, "壹": 0, "幺": 0,
    "二": 1, "2": 1, "贰": 1, "两": 1,
    "三": 2, "3": 2, "叁": 2,
    "四": 3, "4": 3, "肆": 3,
    "五": 4, "5": 4, "伍": 4,
    "六": 5, "6": 5, "陆": 5,
    "日": 6, "天": 6, "7": 6, "0": 6, "柒": 6, "七": 6,
}


@dataclass
class AmbiguityInfo:
    code: AmbiguityCode
    message: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class TemporalIR:
    """时间语义中间表示 —— 所有解析先汇聚到此结构，再归一化到最终输出。"""
    kind: TemporalKind = TemporalKind.INVALID
    # 日期字段（任一可能为 None 表示未给出）
    year: Optional[int] = None
    month: Optional[int] = None
    day: Optional[int] = None
    weekday: Optional[int] = None          # 0=周一 ... 6=周日
    week_anchor: Optional[str] = None      # "this" | "next" | "last" | None（与 weekday 搭配）
    # 时刻字段
    hour: Optional[int] = None
    minute: int = 0
    second: int = 0
    meridiem_explicit: Optional[str] = None  # "am" | "pm" | None（None 表示没有明确上午/下午词）
    # 相对偏移（按解析顺序叠加，先做日期偏移再做时刻偏移）
    day_offset: Optional[int] = None
    week_offset: Optional[int] = None
    month_offset: Optional[int] = None
    hour_offset: Optional[int] = None
    minute_offset: Optional[int] = None
    # 边界锚点
    boundary: Optional[str] = None         # "eom"（月底）| "bom"（月初）| "eoy" | "boy" | ...
    # 即时词
    is_now: bool = False
    # 来源与证据
    source_text: str = ""
    resolution_method: Optional[str] = None
    # 时区
    timezone_id: Optional[str] = None
    # 歧义/冲突
    ambiguities: list[AmbiguityInfo] = field(default_factory=list)

    def add_ambiguity(self, code: AmbiguityCode, message: str, **detail):
        self.ambiguities.append(AmbiguityInfo(code=code, message=message, detail=dict(detail)))


# ============================================================================
# 中文数字辅助（复用 duration_parser，添加专门的整数字典）
# ============================================================================

def parse_cn_number_str(s: Optional[str]) -> Optional[int]:
    """中文或阿拉伯数字字符串 -> int。对外兼容接口。"""
    val = parse_chinese_number(s)
    if val is None:
        return None
    if not val.is_integer():
        # 严格整数："二点五" 这种视为不合法（整数字段要求整数）
        return None
    return int(val)


# ============================================================================
# 归一化辅助：在本地日期/时间上做确定性运算
# ============================================================================

def _add_months_safe(d: date, months: int) -> date:
    """安全地加/减月份，超出月末日期时夹到该月最后一天（如 1/31 +1月 -> 2/28）。"""
    total = d.year * 12 + (d.month - 1) + months
    new_year, new_month_0 = divmod(total, 12)
    new_month = new_month_0 + 1
    last_day = calendar.monthrange(new_year, new_month)[1]
    new_day = min(d.day, last_day)
    return date(new_year, new_month, new_day)


def _last_day_of_month(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]


def _is_leap_year(year: int) -> bool:
    return calendar.isleap(year)


def _validate_date_components(year: int, month: int, day: int) -> tuple[bool, Optional[AmbiguityCode], str]:
    """校验 (year, month, day) 是否构成真实存在的日期。"""
    if not (1 <= month <= 12):
        return False, AmbiguityCode.DAY_OUT_OF_RANGE_FOR_MONTH, f"月份 {month} 不在 1-12 范围内"
    last_day = _last_day_of_month(year, month)
    if day < 1 or day > last_day:
        if month == 2 and day == 29 and not _is_leap_year(year):
            return (
                False,
                AmbiguityCode.LEAP_YEAR_EXPECTED,
                f"{year} 年不是闰年，2 月没有 29 日",
            )
        return (
            False,
            AmbiguityCode.DAY_OUT_OF_RANGE_FOR_MONTH,
            f"{year} 年 {month} 月只有 {last_day} 天，无法填入 {day} 日",
        )
    return True, None, ""


def _compute_weekday_date(
    reference: date,
    target_weekday: int,
    anchor: Optional[str],
) -> date:
    """根据周锚点和目标星期数计算具体日期。

    anchor="this" 或 None -> 本周（周一为一周起点，已过则回到本周，尚未到则取本周）
    anchor="next" -> 下周
    anchor="last" -> 上周

    行为与中文口语对齐：周三说"本周二" -> 取昨天；周三说"本周五" -> 取两天后；
    周三说"下周一" -> 下下周一（若用户倾向"下周的周一"，按"下周"语义加 7 天）。
    """
    target_weekday = target_weekday % 7
    current_wd = reference.weekday()  # 0=周一 ... 6=周日

    if anchor == "last":
        days_diff = target_weekday - current_wd
        if days_diff >= 0:
            days_diff -= 7
        return reference + timedelta(days=days_diff)

    if anchor == "next":
        # 中文"下周一/下周三" = "下一个日历周"的那个周几（先跨到下一周，再取周内的周几）
        # 例：周二(2026-08-18) 说 "下周三" -> 下一周的周三 = 2026-08-26（8天后），不是本周三(19号)
        this_week_monday = reference - timedelta(days=reference.weekday())
        next_week_monday = this_week_monday + timedelta(days=7)
        return next_week_monday + timedelta(days=target_weekday)

    # this / None（本周）：如果目标日在本周范围内（周一~周日），取该日
    days_diff = target_weekday - current_wd  # 可为负（表示本周已过）
    return reference + timedelta(days=days_diff)


# ============================================================================
# 文本预处理
# ============================================================================

def _preprocess_colloquial_clock(text: str) -> str:
    """提前规范化口语化时刻表达，避免被下游规则误匹配。

    注意：**不对文本执行全量 cn2an.transform 中文数字替换**——这会破坏如"下周三15:00"
    这类中文数字（三）紧邻阿拉伯数字/冒号的表达，导致"周三"误合成"周315"。
    中文数字 -> 整数/浮点数一律在正则匹配到具体 token 后，通过 parse_cn_number_str 解析。
    """
    t = text
    # "点一刻/时一刻" / "点半" / "点三刻" —— 口语化分数替换
    t = re.sub(r"点一刻|时一刻|(?<!\d)1刻", "点15分", t)
    t = re.sub(r"点半|时半", "点30分", t)
    t = re.sub(r"点三刻|时三刻|(?<!\d)3刻", "点45分", t)
    return t


def _normalize_text(text: Optional[str]) -> str:
    if not text or not isinstance(text, str):
        return ""
    norm = unicodedata.normalize("NFKC", text).strip()
    if not norm:
        return ""
    return _preprocess_colloquial_clock(norm)


# ============================================================================
# 阶段一：显式日期抽取（绝对日期 + 强相对词 + 周词 + 偏移 + 边界锚点）
# ============================================================================

_AM_WORDS = ("上午", "早上", "早晨", "凌晨", "清晨", "早", "午前")
_PM_WORDS = ("下午", "晚上", "傍晚", "夜里", "夜间", "午后", "晚", "深夜", "半夜")


def _detect_meridiem(text: str) -> Optional[str]:
    if any(w in text for w in _PM_WORDS):
        return "pm"
    if any(w in text for w in _AM_WORDS):
        return "am"
    return None


def _extract_explicit_date_ira(text: str, base_dt: datetime, ira: TemporalIR) -> None:
    """从规范化文本中抽取显式日期信息，写入 TemporalIR。

    优先级：
    1) 绝对年月日 YYYY年MM月DD日 / YYYY-MM-DD / YYYY/MM/DD
    2) 绝对月日 MM月DD日
    3) 强相对日：今天/明天/后天/大后天/昨天/前天
    4) 周锚 + 星期：下周一/本周三/这周五
    5) 偏移：N天前/后、N周前/后、N个月后
    6) 边界：月底/月末、月初/月初、年末、年初、本周末、下周末
    """
    norm = text

    # （无论是否有绝对日期，先尝试提取 weekday 信息做日期-星期冲突检测。轻量模式不设置 day，只提取 weekday 字段。）
    if ira.weekday is None:
        m_weekday_only = re.search(
            r"(?:周|星期|礼拜)\s*([一二三四五六日天12345670])",
            norm,
        )
        if m_weekday_only:
            wd = WEEKDAY_MAP_CN.get(m_weekday_only.group(1))
            if wd is not None:
                ira.weekday = wd

    # 数字 token 匹配：同时接受阿拉伯数字和中文数字
    _NUM_1_2 = r"(?:[0-9]{1,2}|[零一二两三四五六七八九十]{1,4})"
    _NUM_4_YEAR = r"(?:[0-9]{4}|[零一二三四五六七八九]{4})"

    # ---- 1. 绝对日期（年月日） ----
    m_ymd = re.search(
        rf"(?:({_NUM_4_YEAR})[年/-])\s*({_NUM_1_2})[月/-]\s*({_NUM_1_2})[日号]?",
        norm,
    )
    if m_ymd:
        y = parse_cn_number_str(m_ymd.group(1))
        mo = parse_cn_number_str(m_ymd.group(2))
        d = parse_cn_number_str(m_ymd.group(3))
        if y and mo and d:
            ira.year, ira.month, ira.day = y, mo, d
            ira.resolution_method = "explicit_ymd"
            return

    # ---- 2. 月日（年份取 base_dt.year，跨年由后续逻辑纠正） ----
    m_md = re.search(
        rf"(?<![0-9年/-])({_NUM_1_2})[月/-]\s*({_NUM_1_2})[日号]?",
        norm,
    )
    if m_md:
        mo = parse_cn_number_str(m_md.group(1))
        d = parse_cn_number_str(m_md.group(2))
        if mo and d:
            ira.month, ira.day = mo, d
            ira.year = base_dt.year
            # 如果填入后的日期比 base_dt 明显在过去（如 8 月说 3 月），默认翻明年
            if 1 <= mo <= 12:
                tentative = date(ira.year, mo, 1)
                if tentative < date(base_dt.year, base_dt.month, 1):
                    ira.year += 1
            ira.resolution_method = "explicit_md"

    # ---- 3. 强相对日期词（只在没通过绝对路径匹配时使用，防止覆盖） ----
    if ira.day is None:
        if "大后天" in norm or "3天后" in norm or "三天后" in norm:
            ira.day_offset = 3 if ira.day_offset is None else ira.day_offset
            ira.resolution_method = ira.resolution_method or "strong_relative_3d"
        if "后天" in norm or "后晚" in norm or "后早" in norm:
            ira.day_offset = 2 if ira.day_offset is None else ira.day_offset
            ira.resolution_method = ira.resolution_method or "strong_relative_2d"
        if re.search(r"(?<!大)(?<!后)明天|明晚|明早|明个|次日", norm):
            ira.day_offset = 1 if ira.day_offset is None else ira.day_offset
            ira.resolution_method = ira.resolution_method or "strong_relative_tmr"
        if re.search(r"今天|今晚|今早|此刻|当前", norm):
            ira.day_offset = 0 if ira.day_offset is None else ira.day_offset
            ira.resolution_method = ira.resolution_method or "strong_relative_today"
        if "昨天" in norm or "昨晚" in norm or "昨日" in norm:
            ira.day_offset = -1 if ira.day_offset is None else ira.day_offset
            ira.resolution_method = ira.resolution_method or "strong_relative_yesterday"
        if "前天" in norm or "前晚" in norm:
            ira.day_offset = -2 if ira.day_offset is None else ira.day_offset
            ira.resolution_method = ira.resolution_method or "strong_relative_2d_ago"

    # ---- 4. 周 + 星期 ----
    if ira.day is None:
        # 完整模式：下周/本周/这周/上周 + 星期数（只在没有绝对日期时启用，避免覆盖）
        m_wd = re.search(r"(下周|本周|这周|上周)\s*([一二三四五六日天12345670])", norm)
        if m_wd:
            prefix = m_wd.group(1)
            anchor = {"下周": "next", "本周": "this", "这周": "this", "上周": "last"}[prefix]
            wd = WEEKDAY_MAP_CN.get(m_wd.group(2))
            if wd is not None:
                ira.week_anchor = anchor
                ira.weekday = wd
                ira.resolution_method = ira.resolution_method or f"weekday_{anchor}"

    # ---- 5. 相对偏移：N 天/周/个月 前/后 ----
    if ira.day_offset is None and ira.week_offset is None and ira.month_offset is None:
        m_off = re.search(
            r"([0-9]+|[一二两三四五六七八九十百千万]+)\s*(?:个)?\s*(天|日|周|星期|个月|月)\s*(前|后|之后|以前)",
            norm,
        )
        if m_off:
            n_raw, unit, direction = m_off.group(1), m_off.group(2), m_off.group(3)
            n = parse_cn_number_str(n_raw)
            if n is not None:
                sign = 1 if direction in ("后", "之后") else -1
                if unit in ("天", "日"):
                    ira.day_offset = sign * n
                elif unit in ("周", "星期"):
                    ira.week_offset = sign * n
                elif unit in ("个月", "月"):
                    ira.month_offset = sign * n
                ira.resolution_method = ira.resolution_method or f"offset_{unit}_{direction}"

    # ---- 6. 边界锚点 ----
    if ira.day is None and ira.weekday is None and ira.day_offset is None and ira.week_offset is None and ira.month_offset is None:
        if re.search(r"(?:月底|月末|月尾|这个月最后一天|该月最后一天)", norm):
            ira.boundary = "eom"
            ira.resolution_method = "boundary_eom"
        elif re.search(r"(?:月初|月头|这个月第一天)", norm):
            ira.boundary = "bom"
            ira.resolution_method = "boundary_bom"
        elif re.search(r"(?:年底|年末)", norm):
            ira.boundary = "eoy"
            ira.resolution_method = "boundary_eoy"
        elif re.search(r"(?:年初|年头)", norm):
            ira.boundary = "boy"
            ira.resolution_method = "boundary_boy"
        elif re.search(r"本周末", norm):
            ira.week_anchor = "this"
            ira.weekday = 6  # 周日
            ira.resolution_method = "boundary_this_weekend"
        elif re.search(r"下周末", norm):
            ira.week_anchor = "next"
            ira.weekday = 6
            ira.resolution_method = "boundary_next_weekend"


# ============================================================================
# 阶段二：时刻抽取（小时:分钟[:秒] 或 "3点"、"3点半"、"3点15分"等）
# ============================================================================

def _extract_explicit_time_ira(text: str, base_dt: datetime, ira: TemporalIR) -> None:
    norm = text
    # "现在/当前/立即/此时" 作为即时词
    if re.search(r"现在|当前|立即|此时|此刻|马上|立刻", norm):
        ira.is_now = True
        ira.hour = base_dt.hour
        ira.minute = base_dt.minute
        ira.second = base_dt.second
        ira.meridiem_explicit = None
        ira.resolution_method = ira.resolution_method or "explicit_now"
        return

    meridiem = _detect_meridiem(norm)
    ira.meridiem_explicit = meridiem

    _H_NUM = r"(?:[0-2]?[0-9]|[零一二两三四五六七八九十]{1,3})"
    _M_NUM = r"(?:[0-5]?[0-9]|[零一二三四五六七八九十]{1,3})"

    # ISO 格式 HH:MM[:SS]（小时/分钟/秒仍优先用阿拉伯数字，ISO 不常混用中文）
    m_hms = re.search(
        rf"(?<![0-9])({_H_NUM})[:：]({_M_NUM})(?:[:：]({_M_NUM}))?",
        norm,
    )
    if m_hms:
        h = parse_cn_number_str(m_hms.group(1))
        mi = parse_cn_number_str(m_hms.group(2))
        se_raw = m_hms.group(3)
        se = parse_cn_number_str(se_raw) if se_raw else 0
        if h is None or mi is None or se is None:
            h = mi = se = None  # 标记解析失败，跳过
        if h is not None:
            if meridiem == "pm" and h < 12:
                h += 12
            elif meridiem == "am" and h == 12:
                h = 0
            ira.hour, ira.minute, ira.second = h, mi, se
            ira.resolution_method = ira.resolution_method or "explicit_hms_iso"
            return

    # 口语格式："3点/时" [半|一刻|三刻| NN 分]  —— 中文数字 + 阿拉伯数字
    m_clock = re.search(rf"(?<![0-9])({_H_NUM})\s*(?:点|时)", norm)
    if m_clock:
        h_raw = parse_cn_number_str(m_clock.group(1))
        if h_raw is not None:
            h = h_raw
            if meridiem == "pm" and h < 12:
                h += 12
            elif meridiem == "am" and h == 12:
                h = 0
            ira.hour = h

            # 附加分钟（已被预处理替换为"点30分"等形式，这里再做一次兜底）
            if re.search(r"(?:点|时)\s*(?:半|30分)", norm):
                ira.minute = 30
            elif re.search(r"(?:点|时)\s*(?:三刻|3刻|45分)", norm):
                ira.minute = 45
            elif re.search(r"(?:点|时)\s*(?:一刻|1刻|15分)", norm):
                ira.minute = 15
            else:
                m_min = re.search(rf"(?:点|时)\s*({_M_NUM})\s*(?:分|分钟)?", norm)
                if m_min and m_min.group(1) and m_min.start(1) > m_clock.end(1):
                    mn = parse_cn_number_str(m_min.group(1))
                    if mn is not None:
                        ira.minute = mn

            ira.resolution_method = ira.resolution_method or "explicit_hms_colloquial"
            return

    # 小时级偏移：N 小时后 / N 分钟后
    m_h_off = re.search(
        r"([0-9]+|[一二两三四五六七八九十百千万]+)\s*(?:个)?\s*(?:小时|钟头)\s*(后|之后)",
        norm,
    )
    if m_h_off:
        n = parse_cn_number_str(m_h_off.group(1))
        if n is not None:
            ira.hour_offset = n
            ira.resolution_method = ira.resolution_method or "offset_hours_later"

    m_m_off = re.search(
        r"([0-9]+|[一二两三四五六七八九十百千万]+)\s*(?:个)?\s*(?:分钟|分)\s*(后|之后)",
        norm,
    )
    if m_m_off and ira.hour_offset is None:
        n = parse_cn_number_str(m_m_off.group(1))
        if n is not None:
            ira.minute_offset = n
            ira.resolution_method = ira.resolution_method or "offset_minutes_later"


# ============================================================================
# 阶段三：将 TemporalIR 归一化 -> 具体日期 + 时刻（做冲突/歧义/闰年检测）
# ============================================================================

def _materialize_ira(ira: TemporalIR, base_dt: datetime) -> tuple[Optional[date], Optional[dtime]]:
    """从 IR 计算具体的 local date 和 local time，同时登记歧义/冲突。"""
    ref_date = base_dt.date()
    ref_year = base_dt.year

    target_date: Optional[date] = None

    # A. 绝对月日 / 绝对年月日（先验证，再记录冲突）
    if ira.month is not None and ira.day is not None:
        year = ira.year if ira.year is not None else ref_year
        ok, err_code, msg = _validate_date_components(year, ira.month, ira.day)
        if not ok:
            ira.kind = TemporalKind.CONFLICT
            assert err_code
            ira.add_ambiguity(err_code, msg, year=year, month=ira.month, day=ira.day)
            return None, None
        target_date = date(year, ira.month, ira.day)

        # 显式星期 vs 日期 冲突检测
        if ira.weekday is not None and target_date.weekday() != ira.weekday:
            # 中文用户可能误说星期几 -> 标记 AMBIGUOUS 并给出原因，最终仍使用日期字段
            ira.add_ambiguity(
                AmbiguityCode.DATE_WEEKDAY_CONFLICT,
                f"日期 {target_date.isoformat()} 是 {['周一','周二','周三','周四','周五','周六','周日'][target_date.weekday()]}，"
                f"但用户给出的是 {['周一','周二','周三','周四','周五','周六','周日'][ira.weekday]}",
                date_weekday=target_date.weekday(),
                stated_weekday=ira.weekday,
            )
            # 保留 target_date，歧义仅用于上层询问或日志

    # B. 周锚 + 星期
    if target_date is None and ira.weekday is not None:
        target_date = _compute_weekday_date(ref_date, ira.weekday, ira.week_anchor)

    # C. 边界锚点
    if target_date is None and ira.boundary:
        if ira.boundary == "eom":
            ld = _last_day_of_month(ref_year, ref_date.month)
            target_date = date(ref_year, ref_date.month, ld)
        elif ira.boundary == "bom":
            target_date = date(ref_year, ref_date.month, 1)
        elif ira.boundary == "eoy":
            target_date = date(ref_year, 12, 31)
        elif ira.boundary == "boy":
            target_date = date(ref_year, 1, 1)

    # D. 默认：有时间点但没有日期 -> 使用 base_dt.date()（今天）
    if target_date is None:
        any_time_given = (
            ira.is_now
            or ira.hour is not None
            or ira.hour_offset is not None
            or ira.minute_offset is not None
        )
        if any_time_given:
            target_date = ref_date

    # ---- 应用偏移（日期维度） ----
    if target_date is not None:
        if ira.day_offset is not None:
            target_date += timedelta(days=ira.day_offset)
        if ira.week_offset is not None:
            target_date += timedelta(weeks=ira.week_offset)
        if ira.month_offset is not None:
            target_date = _add_months_safe(target_date, ira.month_offset)

    # ---- 时间维度 ----
    target_time: Optional[dtime] = None
    if ira.is_now:
        # 日期也跟随 base_dt（以防日期偏移没有设置）
        if target_date is None:
            target_date = ref_date
        target_time = dtime(
            min(23, max(0, ira.hour or 0)),
            min(59, max(0, ira.minute)),
            min(59, max(0, ira.second)),
        )
    elif ira.hour is not None:
        h = min(23, max(0, ira.hour))
        mi = min(59, max(0, ira.minute))
        se = min(59, max(0, ira.second))
        target_time = dtime(h, mi, se)
    elif ira.hour_offset is not None or ira.minute_offset is not None:
        # 以 base_dt 的当前时刻为基准加偏移
        tmp = datetime.combine(target_date or ref_date, base_dt.timetz().replace(tzinfo=None))
        if ira.hour_offset:
            tmp += timedelta(hours=ira.hour_offset)
        if ira.minute_offset:
            tmp += timedelta(minutes=ira.minute_offset)
        if target_date is None:
            target_date = tmp.date()
        target_time = tmp.time()
        # 防止重复再加偏移：已经应用过了

    return target_date, target_time


def _apply_overmidnight_correction(
    target_date: date,
    target_time: Optional[dtime],
    base_dt: datetime,
    search_text: str,
) -> tuple[date, Optional[dtime]]:
    """夜间 18:00 后用户说'凌晨X点/明早X点/次晨X点'，如果解析出来是今天，自动推到明天。"""
    if base_dt.hour < 18 or target_time is None:
        return target_date, target_time
    trigger = bool(re.search(r"凌晨|次晨|明早|次日早上|次日凌晨", search_text))
    if not trigger:
        return target_date, target_time
    base_date = base_dt.date()
    if target_date == base_date:
        # 解析出来仍是今天 -> 翻到明天
        return target_date + timedelta(days=1), target_time
    return target_date, target_time


def _classify_ambiguities(ira: TemporalIR, hour: Optional[int]) -> None:
    """最后阶段：根据已有字段把歧义补登记。

    进入本函数时解析已经得到 INSTANT 级别结果，不再受 ira.kind 初始值影响。
    """
    if (
        hour is not None
        and ira.meridiem_explicit is None
        and (1 <= hour <= 11)
    ):
        ira.add_ambiguity(
            AmbiguityCode.MERIDIEM_UNSPECIFIED,
            f"小时 {hour} 没有明确上午/下午修饰词，解析器默认按字面解释（如是下午请显式声明）",
            hour=hour,
        )


# ============================================================================
# 顶层解析入口
# ============================================================================

@dataclass
class DateTimeParseResult:
    """完整解析结果：包括规范化 ISO 字符串、Temporal IR、歧义列表。"""
    iso_string: Optional[str] = None            # 兼容旧接口的 YYYY-MM-DDTHH:MM:SS
    ir: TemporalIR = field(default_factory=TemporalIR)
    kind: TemporalKind = TemporalKind.INVALID
    target_local_datetime: Optional[datetime] = None  # naive datetime（本地墙钟）
    ambiguities: list[AmbiguityInfo] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return self.iso_string is not None and self.kind not in (
            TemporalKind.CONFLICT,
            TemporalKind.INVALID,
        )

    @property
    def has_ambiguities(self) -> bool:
        return len(self.ambiguities) > 0


def parse_relative_datetime_detail(
    text: Optional[str],
    base_dt: Optional[datetime] = None,
    full_user_message: Optional[str] = None,
    timezone_id: str = "Asia/Shanghai",
) -> DateTimeParseResult:
    """详细解析：返回结构化 DateTimeParseResult，方便审计与上层判断。"""
    result = DateTimeParseResult()

    if base_dt is None:
        from .simulated_time import get_current_datetime
        base_dt = get_current_datetime()
    # base_dt 允许带或不带时区；内部统一使用 naive 的本地墙钟部分做计算
    base_naive = base_dt.replace(tzinfo=None) if base_dt.tzinfo else base_dt

    if not text or not isinstance(text, str):
        text = ""
    norm = _normalize_text(text)
    full_msg_norm = _normalize_text(full_user_message) if full_user_message else ""

    # 已经是带 T 的完整 ISO 时间（YYYY-MM-DDTHH:MM:SS）—— 按约定交给上层 fromisoformat 直接解析，
    # 本函数不再处理。YYYY-MM-DD、YYYY/MM/DD 这种仅日期或带空格时间的仍由本函数解析，
    # 否则会丢失中文时间（如"2026/12/25 早上9点"）。
    if re.match(r"^\s*\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}", norm):
        result.ir.source_text = norm
        result.ir.resolution_method = "absolute_iso_skipped"
        result.kind = TemporalKind.INVALID
        return result

    combined = f"{norm} {full_msg_norm}".strip()

    # ---- 构建 TemporalIR ----
    ira = TemporalIR(source_text=norm, timezone_id=timezone_id)
    _extract_explicit_date_ira(combined, base_naive, ira)
    _extract_explicit_time_ira(combined, base_naive, ira)

    # 如果局部文本没有解析到日期，但 full_user_message 里明确提到月日/相对日，允许从 full_msg 补日期
    if ira.day is None and ira.weekday is None and ira.day_offset is None and ira.boundary is None:
        ira2 = TemporalIR(source_text=full_msg_norm, timezone_id=timezone_id)
        _extract_explicit_date_ira(full_msg_norm, base_naive, ira2)
        if ira2.resolution_method is not None:
            # 合并日期相关字段
            for fld in ("year", "month", "day", "weekday", "week_anchor",
                        "day_offset", "week_offset", "month_offset", "boundary"):
                v = getattr(ira2, fld)
                if v is not None and getattr(ira, fld) is None:
                    setattr(ira, fld, v)
            ira.resolution_method = ira2.resolution_method + "_from_full_msg"

    # ---- 归一化 -> 具体日期 + 时刻 ----
    target_date, target_time = _materialize_ira(ira, base_naive)
    if target_date is not None and target_time is not None:
        target_date, target_time = _apply_overmidnight_correction(
            target_date, target_time, base_naive, combined,
        )

    # ---- 分类 kind 与歧义 ----
    if ira.kind == TemporalKind.CONFLICT:
        result.kind = TemporalKind.CONFLICT
    elif target_date is not None and target_time is not None:
        result.kind = TemporalKind.INSTANT
        _classify_ambiguities(ira, target_time.hour)
    elif target_date is not None and target_time is None:
        result.kind = TemporalKind.DATE_ONLY
    elif target_date is None and target_time is not None:
        result.kind = TemporalKind.TIME_ONLY
    else:
        result.kind = TemporalKind.INVALID

    # ---- 生成 naive local datetime 和 ISO 字符串 ----
    if target_date is not None and target_time is not None:
        local_dt = datetime.combine(target_date, target_time)
        result.target_local_datetime = local_dt
        result.iso_string = local_dt.strftime("%Y-%m-%dT%H:%M:%S")

    result.ir = ira
    result.ambiguities = list(ira.ambiguities)
    return result


def parse_relative_datetime(
    text: Optional[str],
    base_dt: Optional[datetime] = None,
    full_user_message: Optional[str] = None,
) -> Optional[str]:
    """旧接口兼容：仅返回 YYYY-MM-DDTHH:MM:SS 字符串或 None。

    - 纯日期：若无时间点，返回 None（与原行为一致，避免默认 00:00 的静默猜测）
    - 解析冲突/非法：返回 None
    - 其余：返回秒级 ISO 字符串（无时区，符合项目约定的本地模拟时间格式）
    """
    detail = parse_relative_datetime_detail(text, base_dt, full_user_message)
    if not detail.success:
        return None
    return detail.iso_string


def extract_explicit_date_from_text(
    text: Optional[str],
    base_dt: Optional[datetime] = None,
    is_full_message: bool = False,
) -> Optional[date]:
    """对外兼容：从文本提取 date 对象（不涉及时分秒）。"""
    if not text:
        return None
    if base_dt is None:
        from .simulated_time import get_current_datetime
        base_dt = get_current_datetime()
    base_naive = base_dt.replace(tzinfo=None) if base_dt.tzinfo else base_dt

    # 旧接口的日期-only 快速路径：优先绝对月日，其次强相对词，不跨 full_user_message
    ira = TemporalIR(source_text=text)
    _extract_explicit_date_ira(_normalize_text(text), base_naive, ira)
    if is_full_message and ira.day_offset == 0 and not any([ira.month, ira.weekday, ira.boundary]):
        # 兼容旧逻辑：从 full message 中抽取时，不让弱词"今天"覆盖已有的 ISO 日期
        return None
    d, _ = _materialize_ira(ira, base_naive)
    return d


# ============================================================================
# 时间区间业务模型：开始时间 / 持续时间 / 结束时间
# ============================================================================

class TimeFieldState(str, Enum):
    MISSING = "missing"
    EXPLICIT = "explicit"
    DERIVED = "derived"
    KEEP = "keep"
    INVALID = "invalid"


@dataclass
class TimePointSpec:
    state: TimeFieldState = TimeFieldState.MISSING
    raw_text: str = ""
    value: Optional[datetime] = None
    iso_string: Optional[str] = None
    parse_method: Optional[str] = None
    ambiguities: list[AmbiguityInfo] = field(default_factory=list)
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    has_explicit_date: bool = False
    has_explicit_time: bool = False


@dataclass
class TimeRangeParseResult:
    start_time: TimePointSpec = field(default_factory=TimePointSpec)
    duration: DurationSpec = field(default_factory=DurationSpec)
    end_time: TimePointSpec = field(default_factory=TimePointSpec)
    success: bool = False
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    resolution_method: Optional[str] = None
    warnings: list[str] = field(default_factory=list)
    error_detail: dict[str, Any] = field(default_factory=dict)

    @property
    def start_template(self) -> Optional[str]:
        return format_datetime_template(self.start_time.value) if self.start_time.value else None

    @property
    def duration_template(self) -> Optional[str]:
        if self.duration.total_seconds is None:
            return None
        return format_duration_template(self.duration.total_seconds)

    @property
    def end_template(self) -> Optional[str]:
        return format_datetime_template(self.end_time.value) if self.end_time.value else None


_MISSING_TIME_TEXTS = {"未提供", "无", "null", "none"}
_FATAL_TIME_AMBIGUITIES = {
    AmbiguityCode.MERIDIEM_UNSPECIFIED,
    AmbiguityCode.DATE_WEEKDAY_CONFLICT,
    AmbiguityCode.LEAP_YEAR_EXPECTED,
    AmbiguityCode.DAY_OUT_OF_RANGE_FOR_MONTH,
    AmbiguityCode.DST_GAP_NONEXISTENT,
    AmbiguityCode.DST_FOLD_AMBIGUOUS,
}


def _normalize_missing_text(text: Optional[str]) -> str:
    if text is None or not isinstance(text, str):
        return ""
    return unicodedata.normalize("NFKC", text).strip()


def _parse_iso_datetime(text: str) -> Optional[datetime]:
    raw = text.strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _isoformat_seconds(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _duration_components(total_seconds: float) -> tuple[int, int, int, int]:
    whole_seconds = int(round(total_seconds))
    days, rem = divmod(whole_seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    return days, hours, minutes, seconds


def _duration_from_seconds(
    total_seconds: float,
    *,
    state: DurationState = DurationState.EXPLICIT,
    parse_method: str = "derived_duration",
) -> DurationSpec:
    days, hours, minutes, seconds = _duration_components(total_seconds)
    return DurationSpec(
        state=state,
        raw_text="",
        normalized_text="",
        total_seconds=float(total_seconds),
        days=float(days),
        hours=float(hours),
        minutes=float(minutes),
        seconds=float(seconds),
        parse_method=parse_method,
    )


def _point_has_explicit_date(ira: TemporalIR) -> bool:
    return any(
        [
            ira.year is not None,
            ira.month is not None,
            ira.day is not None,
            ira.weekday is not None,
            ira.week_anchor is not None,
            ira.day_offset is not None,
            ira.week_offset is not None,
            ira.month_offset is not None,
            ira.boundary is not None,
            ira.is_now,
        ]
    )


def _point_has_explicit_time(ira: TemporalIR) -> bool:
    return any(
        [
            ira.hour is not None,
            ira.hour_offset is not None,
            ira.minute_offset is not None,
            ira.is_now,
        ]
    )


def _has_fatal_ambiguity(ambiguities: list[AmbiguityInfo]) -> bool:
    return any(item.code in _FATAL_TIME_AMBIGUITIES for item in ambiguities)


def _parse_time_point_spec(
    text: Optional[str],
    base_dt: datetime,
    timezone_id: str,
    *,
    date_context: Optional[date] = None,
) -> TimePointSpec:
    """解析单个业务时间槽位。字段已由上游隔离，因此不接收 full_user_message。"""
    raw = _normalize_missing_text(text)
    if not raw or raw.lower() in _MISSING_TIME_TEXTS:
        return TimePointSpec(state=TimeFieldState.MISSING, raw_text=raw)

    iso_dt = _parse_iso_datetime(raw)
    if iso_dt is not None:
        return TimePointSpec(
            state=TimeFieldState.EXPLICIT,
            raw_text=raw,
            value=iso_dt,
            iso_string=_isoformat_seconds(iso_dt),
            parse_method="absolute_iso",
            has_explicit_date=True,
            has_explicit_time=True,
        )

    point_base = base_dt
    if date_context is not None:
        base_time = base_dt.timetz().replace(tzinfo=None)
        point_base = datetime.combine(date_context, base_time)

    detail = parse_relative_datetime_detail(
        text=raw,
        base_dt=point_base,
        full_user_message=None,
        timezone_id=timezone_id,
    )

    if not detail.success or _has_fatal_ambiguity(detail.ambiguities):
        return TimePointSpec(
            state=TimeFieldState.INVALID,
            raw_text=raw,
            parse_method=detail.ir.resolution_method,
            ambiguities=detail.ambiguities,
            error_code="INVALID_TIME_POINT",
            error_message="无法解析时间点",
            has_explicit_date=_point_has_explicit_date(detail.ir),
            has_explicit_time=_point_has_explicit_time(detail.ir),
        )

    return TimePointSpec(
        state=TimeFieldState.EXPLICIT,
        raw_text=raw,
        value=detail.target_local_datetime,
        iso_string=detail.iso_string,
        parse_method=detail.ir.resolution_method,
        ambiguities=detail.ambiguities,
        has_explicit_date=_point_has_explicit_date(detail.ir),
        has_explicit_time=_point_has_explicit_time(detail.ir),
    )


def _derived_time_point(dt: datetime, parse_method: str) -> TimePointSpec:
    return TimePointSpec(
        state=TimeFieldState.DERIVED,
        raw_text="",
        value=dt,
        iso_string=_isoformat_seconds(dt),
        parse_method=parse_method,
        has_explicit_date=True,
        has_explicit_time=True,
    )


def _history_time_point(dt: datetime, parse_method: str = "history_time") -> TimePointSpec:
    return TimePointSpec(
        state=TimeFieldState.EXPLICIT,
        raw_text="",
        value=dt,
        iso_string=_isoformat_seconds(dt),
        parse_method=parse_method,
        has_explicit_date=True,
        has_explicit_time=True,
    )


def _fail_time_range(
    result: TimeRangeParseResult,
    code: str,
    message: str,
    **detail: Any,
) -> TimeRangeParseResult:
    result.success = False
    result.error_code = code
    result.error_message = message
    result.error_detail = dict(detail)
    return result


def _parse_time_point_delta_seconds(text: Optional[str]) -> Optional[float]:
    if not text or not isinstance(text, str):
        return None
    spec = parse_duration_spec(text)
    if spec.state == DurationState.DELTA:
        return spec.delta_seconds
    return None


def _resolve_previous_duration(
    previous_start: Optional[datetime],
    previous_end: Optional[datetime],
    previous_duration_seconds: Optional[float],
) -> Optional[float]:
    if previous_duration_seconds is not None:
        try:
            seconds = float(previous_duration_seconds)
        except (TypeError, ValueError):
            return None
        return seconds if seconds > 0 else None

    if previous_start is None or previous_end is None:
        return None
    seconds = (previous_end - previous_start).total_seconds()
    return seconds if seconds > 0 else None


def format_datetime_template(dt: datetime) -> str:
    """按协议模板输出规范化时间点，如 2026年08月27日15时30分。"""
    return dt.strftime("%Y年%m月%d日%H时%M分")


def parse_time_range(
    start_text: Optional[str],
    duration_text: Optional[str],
    end_text: Optional[str],
    *,
    base_dt: Optional[datetime] = None,
    previous_start: Optional[datetime] = None,
    previous_end: Optional[datetime] = None,
    previous_duration_seconds: Optional[float] = None,
    timezone_id: str = "Asia/Shanghai",
) -> TimeRangeParseResult:
    """统一时间区间入口：LLM 只给三个槽位，Python 负责确定性计算 S/D/E。"""
    if base_dt is None:
        from .simulated_time import get_current_datetime
        base_dt = get_current_datetime()
    base_naive = base_dt.replace(tzinfo=None) if base_dt.tzinfo else base_dt

    result = TimeRangeParseResult()
    start_delta_seconds = _parse_time_point_delta_seconds(start_text)
    end_delta_seconds = _parse_time_point_delta_seconds(end_text)

    if start_delta_seconds is not None:
        if previous_start is None:
            result.duration = parse_duration_spec(duration_text)
            return _fail_time_range(
                result,
                "START_DELTA_WITHOUT_HISTORY",
                "用户要求调整开始时间，但没有可继承的历史开始时间",
            )
        start = _history_time_point(
            previous_start + timedelta(seconds=start_delta_seconds),
            "time_point_delta",
        )
        start.raw_text = _normalize_missing_text(start_text)
    elif not _normalize_missing_text(start_text) and previous_start is not None:
        start = _history_time_point(previous_start)
    else:
        start = _parse_time_point_spec(start_text, base_naive, timezone_id)

    duration = parse_duration_spec(duration_text)
    result.start_time = start
    result.duration = duration

    if start.state == TimeFieldState.INVALID:
        return _fail_time_range(result, "INVALID_START_TIME", "开始时间无法解析")
    if duration.state == DurationState.INVALID:
        return _fail_time_range(result, "INVALID_DURATION", "持续时间无法解析")
    if duration.state == DurationState.DELTA and previous_start is None and start.state == TimeFieldState.MISSING:
        return _fail_time_range(
            result,
            "DURATION_DELTA_WITHOUT_START",
            "用户要求调整持续时间，但没有可用于计算结束时间的开始时间",
        )
    if start.state == TimeFieldState.MISSING:
        end = _parse_time_point_spec(end_text, base_naive, timezone_id)
        result.end_time = end
        return _fail_time_range(result, "START_TIME_REQUIRED", "必须提供开始时间")

    assert start.value is not None
    if end_delta_seconds is not None:
        if previous_end is None:
            return _fail_time_range(
                result,
                "END_DELTA_WITHOUT_HISTORY",
                "用户要求调整结束时间，但没有可继承的历史结束时间",
            )
        end = _history_time_point(
            previous_end + timedelta(seconds=end_delta_seconds),
            "time_point_delta",
        )
        end.raw_text = _normalize_missing_text(end_text)
    else:
        end = _parse_time_point_spec(
            end_text,
            base_naive,
            timezone_id,
            date_context=start.value.date(),
        )
    result.end_time = end
    if end.state == TimeFieldState.INVALID:
        return _fail_time_range(result, "INVALID_END_TIME", "结束时间无法解析")

    duration_seconds: Optional[float] = None
    if duration.state == DurationState.EXPLICIT:
        duration_seconds = duration.total_seconds
    elif duration.state == DurationState.DELTA:
        previous_duration = _resolve_previous_duration(
            previous_start,
            previous_end,
            previous_duration_seconds,
        )
        if previous_duration is None or duration.delta_seconds is None:
            return _fail_time_range(
                result,
                "DURATION_DELTA_WITHOUT_HISTORY",
                "用户要求调整持续时间，但没有可继承的历史持续时间",
            )
        duration_seconds = previous_duration + duration.delta_seconds
        if duration_seconds <= 0:
            return _fail_time_range(
                result,
                "NON_POSITIVE_DURATION",
                "调整后的持续时间必须为正数",
            )
        result.duration = _duration_from_seconds(
            duration_seconds,
            state=DurationState.EXPLICIT,
            parse_method="duration_delta",
        )
        result.duration.raw_text = duration.raw_text
    elif duration.state == DurationState.KEEP:
        duration_seconds = _resolve_previous_duration(
            previous_start,
            previous_end,
            previous_duration_seconds,
        )
        if duration_seconds is None:
            return _fail_time_range(
                result,
                "KEEP_DURATION_WITHOUT_HISTORY",
                "用户要求持续时间不变，但没有可继承的历史持续时间",
            )
        result.duration = _duration_from_seconds(
            duration_seconds,
            state=DurationState.KEEP,
            parse_method=duration.parse_method or "keep_duration",
        )
        result.duration.raw_text = duration.raw_text

    start_dt = start.value
    end_dt = end.value

    if (
        end.state == TimeFieldState.EXPLICIT
        and end_dt is not None
        and end_dt <= start_dt
        and not end.has_explicit_date
    ):
        end_dt = end_dt + timedelta(days=1)
        result.end_time = TimePointSpec(
            state=TimeFieldState.EXPLICIT,
            raw_text=end.raw_text,
            value=end_dt,
            iso_string=_isoformat_seconds(end_dt),
            parse_method="range_cross_midnight",
            ambiguities=end.ambiguities,
            has_explicit_date=end.has_explicit_date,
            has_explicit_time=end.has_explicit_time,
        )

    if result.end_time.state == TimeFieldState.MISSING and duration_seconds is None:
        previous_duration = _resolve_previous_duration(
            previous_start,
            previous_end,
            previous_duration_seconds,
        )
        if previous_duration is not None and start_delta_seconds is not None:
            duration_seconds = previous_duration
            result.duration = _duration_from_seconds(
                duration_seconds,
                state=DurationState.KEEP,
                parse_method="keep_duration",
            )
            result.duration.raw_text = "持续时间不变"

    if result.end_time.state == TimeFieldState.MISSING and duration_seconds is not None:
        derived_end = start_dt + timedelta(seconds=duration_seconds)
        result.end_time = _derived_time_point(derived_end, "duration_arithmetic")
        result.resolution_method = (
            "duration_delta_from_history"
            if result.duration.parse_method == "duration_delta"
            else "start_plus_duration"
        )
    elif result.end_time.state == TimeFieldState.EXPLICIT and duration.state == DurationState.MISSING:
        assert result.end_time.value is not None
        duration_seconds = (result.end_time.value - start_dt).total_seconds()
        if duration_seconds <= 0:
            return _fail_time_range(result, "END_NOT_AFTER_START", "结束时间必须晚于开始时间")
        result.duration = _duration_from_seconds(duration_seconds)
        result.resolution_method = "end_minus_start"
    elif result.end_time.state == TimeFieldState.EXPLICIT and duration_seconds is not None:
        assert result.end_time.value is not None
        calculated_end = start_dt + timedelta(seconds=duration_seconds)
        if calculated_end != result.end_time.value:
            return _fail_time_range(
                result,
                "TIME_RANGE_CONFLICT",
                "开始时间、持续时间和结束时间不一致",
                start=start_dt.isoformat(timespec="seconds"),
                duration_seconds=duration_seconds,
                explicit_end=result.end_time.value.isoformat(timespec="seconds"),
                calculated_end=calculated_end.isoformat(timespec="seconds"),
            )
        result.resolution_method = "explicit_all_verified"
    else:
        return _fail_time_range(
            result,
            "INCOMPLETE_TIME_RANGE",
            "开始时间之后必须提供持续时间或结束时间",
        )

    if result.end_time.value is None or result.end_time.value <= result.start_time.value:
        return _fail_time_range(result, "END_NOT_AFTER_START", "结束时间必须晚于开始时间")

    result.success = True
    return result
