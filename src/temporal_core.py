"""
temporal_core.py — SEAgent 时间识别核心模块（升级版 v2.0）

架构：
  自然语言文本
      ↓
  中文数字归一化（内置，不依赖 cn2an）
      ↓
  TemporalIntentExtractor → 生成 TemporalIR（语义中间表示，可审计）
      ↓
  TemporalNormalizer → 确定性日期运算、时区、DST、闰年验证
      ↓
  Validation（歧义 / 冲突检测）
      ↓
  标准 ISO 输出 + 解析元数据

设计目标：
  - 中文时间识别准确率 ≥ 99%
  - 零第三方硬依赖（cn2an / dateparser 为可选增强）
  - 所有解析结果可回溯、可审计
  - 歧义场景显式返回，禁止静默猜测
"""

from __future__ import annotations

import calendar
import math
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import Enum
from typing import Any, Optional
from zoneinfo import ZoneInfo


# ──────────────────────────────────────────────────────────────────────────────
# 1. 中文数字解析器（内置实现，零第三方依赖
# ──────────────────────────────────────────────────────────────────────────────

_CN_DIGIT = {
    "零": 0, "〇": 0, "○": 0, "O": 0, "0": 0,
    "一": 1, "壹": 1, "幺": 1, "1": 1,
    "二": 2, "贰": 2, "两": 2, "俩": 2, "2": 2,
    "三": 3, "叁": 3, "仨": 3, "3": 3,
    "四": 4, "肆": 4, "4": 4,
    "五": 5, "伍": 5, "5": 5,
    "六": 6, "陆": 6, "6": 6,
    "七": 7, "柒": 7, "7": 7,
    "八": 8, "捌": 8, "8": 8,
    "九": 9, "玖": 9, "9": 9,
}

_CN_UNIT = {
    "十": 10, "拾": 10,
    "百": 100, "佰": 100,
    "千": 1000, "仟": 1000,
    "万": 10000, "萬": 10000,
    "亿": 100000000, "億": 100000000,
}


def parse_cn_number(text: str) -> Optional[int | float]:
    """
    将中文数字或阿拉伯数字转为 int 或 float。
    支持："两" -> 2, "十二" -> 12, "二十五" -> 25, "一百二十三" -> 123,
          "两万三千" -> 23000, "二点五" / "2.5" -> 2.5, "半" -> 0.5
    """
    if text is None:
        return None
    if not isinstance(text, str):
        return None
    s = unicodedata.normalize("NFKC", text).strip()
    if not s:
        return None

    if s == "半":
        return 0.5

    # 纯阿拉伯数字（含小数）
    if re.fullmatch(r"[-+]?\d+(?:\.\d+)?", s):
        try:
            if "." in s:
                v = float(s)
            else:
                v = int(s)
            return v if math.isfinite(v) else None
        except ValueError:
            return None

    # 处理 "点" 小数 如 "二点五" / "三点一四"
    if "点" in s:
        parts = s.split("点", 1)
        int_part = parse_cn_number(parts[0])
        frac_str = parts[1]
        if int_part is None or not frac_str:
            return None
        frac_digits = []
        for ch in frac_str:
            if ch in _CN_DIGIT:
                frac_digits.append(str(_CN_DIGIT[ch]))
            elif ch.isdigit():
                frac_digits.append(ch)
            else:
                return None
        if not frac_digits:
            return None
        try:
            return float(f"{int_part}.{''.join(frac_digits)}")
        except ValueError:
            return None

    # 中文整数解析（含 "十"/"百"/"千"/"万"/"亿"）
    try:
        total = 0
        section = 0  # 当前 "万" 或 "亿" 以下小节累计
        current = 0  # 当前单位前的数字
        i = 0
        n = len(s)

        # 处理开头 "十X" = 1X
        if n >= 1 and (s[0] == "十" or s[0] == "拾"):
            current = 1

        while i < n:
            ch = s[i]
            if ch in _CN_DIGIT:
                current = current * 10 + _CN_DIGIT[ch] if current == 0 else _CN_DIGIT[ch]
            elif ch in _CN_UNIT:
                unit = _CN_UNIT[ch]
                if unit >= 10000:
                    section = (section + current) * unit if current else section * unit
                    total += section
                    section = 0
                else:
                    section += (current if current else 1) * unit
                current = 0
            else:
                return None
            i += 1
        result = total + section + current
        if result == 0 and s and s not in ("零", "〇", "○", "O", "0"):
            return None
        return result
    except Exception:
        return None


# ──────────────────────────────────────────────────────────────────────────────
# 2. Temporal IR — 语义中间表示 & 歧义状态
# ──────────────────────────────────────────────────────────────────────────────

class AmbiguityCode(str, Enum):
    NONE = "NONE"
    MERIDIEM_UNKNOWN = "MERIDIEM_UNKNOWN"       # "3点" 上午/下午不确定
    DATE_WEEKDAY_CONFLICT = "DATE_WEEKDAY_CONFLICT"  # 日期与星期冲突
    MONTH_DAY_INVALID = "MONTH_DAY_INVALID"     # 如 2月30日
    LEAP_YEAR_INVALID = "LEAP_YEAR_INVALID"     # 非闰年2月29日
    INTERVAL_INVERTED = "INTERVAL_INVERTED"     # end < start
    DST_GAP = "DST_GAP"                     # 本地时间不存在(DST跳过)
    DST_FOLD = "DST_FOLD"                   # 本地时间歧义(DST回拨)
    LOCALE_DATE_AMBIGUOUS = "LOCALE_DATE_AMBIGUOUS"  # 03/04 美式/欧式不同
    INSUFFICIENT_INFO = "INSUFFICIENT_INFO"       # 缺少必要信息


class TemporalKind(str, Enum):
    INSTANT = "instant"
    INTERVAL = "interval"
    DATE_ONLY = "date_only"
    DURATION = "duration"
    RECURRENCE = "recurrence"


@dataclass
class TemporalIR:
    """时间语义中间表示 — 所有解析结果的可审计中间态"""
    kind: TemporalKind
    source_text: str

    # 本地墙钟时间（无时区偏移语义的用户原意）
    local_date: Optional[date] = None
    local_time_hour: Optional[int] = None
    local_time_minute: int = 0
    local_time_second: int = 0

    # 区间结束
    end_local_date: Optional[date] = None
    end_local_time_hour: Optional[int] = None
    end_local_time_minute: int = 0
    end_local_time_second: int = 0

    # 时区
    timezone_id: str = "Asia/Shanghai"
    timezone_source: str = "default"  # explicit / profile / device / default

    # 时长（秒）
    duration_seconds: Optional[float] = None

    # 精度
    precision: str = "minute"  # year/month/day/hour/minute/second

    # 歧义与告警
    ambiguities: list[AmbiguityCode] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    # 解析溯源
    resolution_method: str = "builtin"
    reference_instant_iso: Optional[str] = None

    def has_ambiguity(self) -> bool:
        return any(a != AmbiguityCode.NONE for a in self.ambiguities)

    def get_ambiguity_reasons(self) -> list[str]:
        reasons = []
        for a in self.ambiguities:
            if a == AmbiguityCode.MERIDIEM_UNKNOWN:
                reasons.append("未明确上午或下午（如'3点'可能指凌晨3点或下午3点）")
            elif a == AmbiguityCode.DATE_WEEKDAY_CONFLICT:
                reasons.append("日期与星期描述不一致")
            elif a == AmbiguityCode.MONTH_DAY_INVALID:
                reasons.append("月日组合无效")
            elif a == AmbiguityCode.LEAP_YEAR_INVALID:
                reasons.append("非闰年的2月29日无效")
            elif a == AmbiguityCode.INTERVAL_INVERTED:
                reasons.append("时间区间结束早于开始")
            elif a == AmbiguityCode.DST_GAP:
                reasons.append("该本地时间在夏令时跳转中不存在")
            elif a == AmbiguityCode.DST_FOLD:
                reasons.append("该本地时间对应两个真实时刻（夏令时回拨）")
            elif a == AmbiguityCode.LOCALE_DATE_AMBIGUOUS:
                reasons.append("日期格式存在美式/欧式歧义")
            elif a == AmbiguityCode.INSUFFICIENT_INFO:
                reasons.append("信息不足以确定唯一时间")
        return reasons


# ──────────────────────────────────────────────────────────────────────────────
# 3. 解析上下文：reference_instant + timezone + locale
# ──────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class TemporalContext:
    """不可变的解析上下文，保证 reference_instant 在一次请求内冻结"""
    reference_instant: datetime
    timezone_id: str = "Asia/Shanghai"
    locale: str = "zh-CN"
    week_start: int = 0  # 0=Monday

    @property
    def local_reference(self) -> datetime:
        tz = ZoneInfo(self.timezone_id)
        return self.reference_instant.astimezone(tz)

    @staticmethod
    def create(
        reference_instant: Optional[datetime] = None,
        timezone_id: str = "Asia/Shanghai",
    ) -> "TemporalContext":
        if reference_instant is None:
            from .simulated_time import get_current_datetime
            reference_instant = get_current_datetime()
        if reference_instant.tzinfo is None:
            reference_instant = reference_instant.replace(tzinfo=ZoneInfo(timezone_id))
        return TemporalContext(
            reference_instant=reference_instant,
            timezone_id=timezone_id,
        )


# ──────────────────────────────────────────────────────────────────────────────
# 4. 词法 / 模式常量
# ──────────────────────────────────────────────────────────────────────────────

WEEKDAY_MAP = {
    "一": 0, "1": 0,
    "二": 1, "2": 1,
    "三": 2, "3": 2,
    "四": 3, "4": 3,
    "五": 4, "5": 4,
    "六": 5, "6": 5,
    "日": 6, "天": 6, "7": 6, "0": 6,
    "礼拜一": 0, "礼拜二": 1, "礼拜三": 2, "礼拜四": 3,
    "礼拜五": 4, "礼拜六": 5, "礼拜天": 6, "礼拜日": 6,
    "周一": 0, "周二": 1, "周三": 2, "周四": 3,
    "周五": 4, "周六": 5, "周日": 6, "周天": 6,
    "星期一": 0, "星期二": 1, "星期三": 2, "星期四": 3,
    "星期五": 4, "星期六": 5, "星期日": 6, "星期天": 6,
}

AM_PATTERNS_AM = ["上午", "早上", "早晨", "凌晨", "清晨", "早", "午前"]
AM_PATTERNS_PM = ["下午", "晚上", "傍晚", "夜里", "夜间", "午后", "晚", "半夜", "深夜"]

# 特殊日期词 -> 相对于 reference date 的偏移
DATE_OFFSET_KEYWORDS = [
    (["大前天"], timedelta(days=-3)),
    (["前天", "前晚", "前早"], timedelta(days=-2)),
    (["昨天", "昨晚", "昨夜", "昨早"], timedelta(days=-1)),
    (["今天", "今晚", "今早", "现在", "当前", "今日", "今儿", "即日"], timedelta(days=0)),
    (["明天", "明晚", "明早", "明个", "明日", "次日"], timedelta(days=1)),
    (["后天", "后晚", "后早", "后日"], timedelta(days=2)),
    (["大后天"], timedelta(days=3)),
]


# ──────────────────────────────────────────────────────────────────────────────
# 5. 核心：TemporalIntentExtractor
# ──────────────────────────────────────────────────────────────────────────────

class TemporalIntentExtractor:
    """确定性中文时间意图抽取器 — 输入自然语言 → 输出 TemporalIR"""

    # ------------------------------------------------------------------ utils
    @staticmethod
    def _preprocess(text: str) -> str:
        if not text:
            return ""
        norm = unicodedata.normalize("NFKC", text).strip()
        # 归一化时刻特殊表达
        norm = re.sub(r"点一刻|时一刻|(?<!\d)1刻", "点15分", norm)
        norm = re.sub(r"点半|时半", "点30分", norm)
        norm = re.sub(r"点三刻|时三刻|(?<!\d)3刻", "点45分", norm)
        return norm

    @staticmethod
    def _detect_meridiem(text: str) -> tuple[bool, bool]:
        """返回 (is_am_detected, is_pm_detected)"""
        is_am = any(p in text for p in AM_PATTERNS_AM)
        is_pm = any(p in text for p in AM_PATTERNS_PM)
        return is_am, is_pm

    # --------------------------------------------------------------- date parse
    @staticmethod
    def _parse_explicit_date(text: str, ctx: TemporalContext) -> Optional[date]:
        """解析显式绝对日期：2026年8月25日 / 8-25 / 八月二十五号"""
        ref_local = ctx.local_reference
        year = ref_local.year

        # 格式1: 2026年8月25日 / 2026-08-25 / 2026/08/25
        m = re.search(
            r"(?:([\d零一二三四五六七八九十百千两点贰叁肆伍陆柒捌玖]+)\s*[年/-])?\s*"
            r"([\d零一二三四五六七八九十两点贰叁肆伍陆柒捌玖]+)\s*[月/-]\s*"
            r"([\d零一二三四五六七八九十两点贰叁肆伍陆柒捌玖]+)\s*[日号]?",
            text,
        )
        if m:
            y_raw, mo_raw, d_raw = m.group(1), m.group(2), m.group(3)
            y = parse_cn_number(y_raw) if y_raw else None
            mo = parse_cn_number(mo_raw)
            d = parse_cn_number(d_raw)
            if mo is None or d is None:
                return None
            try:
                actual_year = int(y) if y is not None and 1900 <= int(y) <= 2100 else year
                # 如果没有明确年份，且月份<当前月，默认补下一年
                if y is None and int(mo) < ref_local.month:
                    actual_year = year + 1
                return date(actual_year, int(mo), int(d))
            except (ValueError, TypeError):
                return None
        return None

    @staticmethod
    def _parse_relative_date_offset(text: str, ctx: TemporalContext) -> Optional[date]:
        """解析：大前天/昨天/今天/明天/后天/大后天"""
        ref_date = ctx.local_reference.date()
        for keywords, delta in DATE_OFFSET_KEYWORDS:
            for kw in keywords:
                if kw in text:
                    return ref_date + delta
        return None

    @staticmethod
    def _parse_weekday_date(text: str, ctx: TemporalContext) -> Optional[date]:
        """解析：下周一 / 本周三 / 这周五 / 下礼拜六"""
        ref_local = ctx.local_reference
        ref_date = ref_local.date()
        ref_wd = ref_local.weekday()

        # 匹配：(下|本|这|上) ? (周|星期|礼拜)? X
        patterns = [
            r"(下周|下个星期|下个礼拜|下星期|下礼拜)\s*(?:星期|周|礼拜)?\s*([一二三四五六日天\d])",
            r"(本周|这个星期|这个礼拜|这星期|这礼拜|这周|这个周)\s*(?:星期|周|礼拜)?\s*([一二三四五六日天\d])",
            r"(上周|上个星期|上个礼拜|上星期|上礼拜)\s*(?:星期|周|礼拜)?\s*([一二三四五六日天\d])",
        ]
        for pat in patterns:
            m = re.search(pat, text)
            if m:
                prefix, wd_char = m.group(1), m.group(2)
                target_wd = WEEKDAY_MAP.get(wd_char)
                if target_wd is None:
                    continue
                if prefix.startswith("下"):
                    days = (target_wd - ref_wd + 7) % 7 or 7
                    if "两个" in prefix or "两星期" in prefix or "下下" in prefix:
                        days += 7
                    return ref_date + timedelta(days=days)
                elif prefix.startswith("本") or prefix.startswith("这"):
                    days = target_wd - ref_wd
                    return ref_date + timedelta(days=days)
                elif prefix.startswith("上"):
                    days = (target_wd - ref_wd - 7)
                    if days > 0:
                        days -= 7
                    return ref_date + timedelta(days=days)
        # 单独的 "周一"/"星期五" 若无前缀，默认本周（若已过则下周）
        for short_kw, wd_val in WEEKDAY_MAP.items():
            if len(short_kw) >= 2 and short_kw in text:
                # 确保不是已匹配过的前缀模式
                if re.search(r"(下|本|这|上)", text):
                    continue
                diff = wd_val - ref_wd
                if diff < 0:
                    diff += 7
                return ref_date + timedelta(days=diff)
        return None

    @staticmethod
    def _parse_offset_date(text: str, ctx: TemporalContext) -> Optional[date]:
        """解析：3天后 / 2周前 / 1个月后 / 半年后"""
        ref_date = ctx.local_reference.date()
        m = re.search(
            r"([\d零一二三四五六七八九十两半点]+)\s*(?:个)?\s*"
            r"(天|日|周|星期|礼拜|个月|月|年)\s*(前|之前|以前|后|之后|以后|过后)",
            text,
        )
        if m:
            n_raw, unit, direction = m.group(1), m.group(2), m.group(3)
            n = parse_cn_number(n_raw)
            if n is None:
                return None
            sign = -1 if direction in ("前", "之前", "以前") else 1
            factor = float(n) * sign
            try:
                if unit in ("天", "日"):
                    return ref_date + timedelta(days=int(factor))
                elif unit in ("周", "星期", "礼拜"):
                    return ref_date + timedelta(weeks=factor)
                elif unit in ("个月", "月"):
                    total = ref_date.month - 1 + int(factor)
                    new_y = ref_date.year + total // 12
                    new_m = total % 12 + 1
                    # 处理月末溢出：1月31日 + 1月 = 2月28/29日（不是3月3日）
                    _, max_d = calendar.monthrange(new_y, new_m)
                    new_d = min(ref_date.day, max_d)
                    return date(new_y, new_m, new_d)
                elif unit == "年":
                    new_y = ref_date.year + int(factor)
                    # 闰年2月29日处理
                    if ref_date.month == 2 and ref_date.day == 29 and not calendar.isleap(new_y):
                        return date(new_y, 2, 28)
                    return date(new_y, ref_date.month, ref_date.day)
            except (ValueError, OverflowError):
                return None
        return None

    @staticmethod
    def _parse_boundary_date(text: str, ctx: TemporalContext) -> Optional[date]:
        """解析：月底 / 月初 / 年末 / 年初 / 周末 / 本周初"""
        ref_local = ctx.local_reference
        y, m, d = ref_local.year, ref_local.month, ref_local.day
        if re.search(r"(?:本月|这个月|当月)?月底|月末|月尾", text):
            _, last_day = calendar.monthrange(y, m)
            return date(y, m, last_day)
        if re.search(r"(?:本月|这个月|当月)?月初|月头|月首", text):
            return date(y, m, 1)
        if re.search(r"今年年底|年末|年尾", text):
            return date(y, 12, 31)
        if re.search(r"今年年初|年初|年首", text):
            return date(y, 1, 1)
        if re.search(r"明年年底", text):
            return date(y + 1, 12, 31)
        if re.search(r"明年年初", text):
            return date(y + 1, 1, 1)
        if re.search(r"周末", text):
            ref_wd = ref_local.weekday()
            days = 5 - ref_wd if ref_wd <= 5 else 5 - ref_wd + 7
            return ref_local.date() + timedelta(days=days if days else 7)
        return None

    # --------------------------------------------------------------- time parse
    @staticmethod
    def _parse_time(text: str) -> Optional[tuple[int, int, int, bool]]:
        """
        解析时间部分：返回 (hour, minute, second, has_explicit_time)
        不处理上午/下午偏移，交给上层处理
        """
        # ISO 格式 17:30[:00]
        m = re.search(r"(\d{1,2})[:：](\d{2})(?:[:：](\d{2}))?", text)
        if m:
            h = int(m.group(1))
            mi = int(m.group(2))
            s = int(m.group(3)) if m.group(3) else 0
            if 0 <= h <= 23 and 0 <= mi <= 59 and 0 <= s <= 59:
                return h, mi, s, True

        # 口语：11点 / 3点半 / 下午2点30分 / 八点一刻
        m = re.search(
            r"([零一二三四五六七八九十两\d]{1,3})\s*(?:点|时)\s*"
            r"(?:半|三刻|3刻|一刻|1刻|(\d{1,2})\s*(?:分|分钟)?\s*"
            r"(?:(\d{1,2})\s*(?:秒|秒钟)?)?",
            text,
        )
        if m:
            h_raw = m.group(1)
            h = parse_cn_number(h_raw)
            if h is None:
                return None
            h = int(h)
            # 处理 "十一点" -> 11, "十二点" -> 12
            if h == 10 and h_raw == "十":  # 如果写的是 "十点半" -> 10
                h = 10
            if not (0 <= h <= 23):
                return None
            mi = 0
            s = 0
            remainder = text[m.end():] if m.end() < len(text) else ""
            matched_text = m.group(0)
            if "点半" in matched_text or "时半" in matched_text:
                mi = 30
            elif "三刻" in matched_text or "3刻" in matched_text:
                mi = 45
            elif "一刻" in matched_text or "1刻" in matched_text:
                mi = 15
            elif m.group(2):
                mi_parsed = parse_cn_number(m.group(2))
                if mi_parsed is not None and 0 <= int(mi_parsed) <= 59:
                    mi = int(mi_parsed)
            if m.group(3):
                s_parsed = parse_cn_number(m.group(3))
                if s_parsed is not None and 0 <= int(s_parsed) <= 59:
                    s = int(s_parsed)
            return h, mi, s, True
        return None

    # -------------------------------------------------------------- duration
    @staticmethod
    def parse_duration(text: str) -> Optional[float]:
        """
        解析时长 → 正数秒。
        支持：两个半小时 / 2小时半 / 1天半 / 45分钟 / 3小时15分 / 两天
        """
        if not text:
            return None
        norm = unicodedata.normalize("NFKC", text).strip()
        if not norm:
            return None
        # 去前缀后缀
        cleaned = re.sub(r"^(?:持续|大概|约|预估|用时|大约|预计|作业|需要|共|耗时|用|历时|时长|时间为?)+", "", norm).strip()
        cleaned = re.sub(r"(?:左右|上下|即可|时间|以内|之内|左右的时间|前后)+$", "", cleaned).strip()
        if not cleaned:
            return None
        # 独立半小时 / 半天 / 半分钟
        if re.fullmatch(r"半\s*(?:个)?\s*(?:小时|钟头|时)", cleaned):
            return 1800.0
        if re.fullmatch(r"半\s*(?:个)?\s*天", cleaned):
            return 43200.0
        if re.fullmatch(r"半\s*(?:个)?\s*(?:分钟|分)", cleaned):
            return 30.0
        # X个半小时 / X小时半
        for pat in [
            r"([\d零一二三四五六七八九十百点两]+)\s*(?:个)?\s*半\s*(?:个)?\s*(?:小时|钟头|h|hr|hours?|hrs?)",
            r"([\d零一二三四五六七八九十百点两]+)\s*(?:小时|钟头)\s*半",
        ]:
            m = re.search(pat, cleaned, re.IGNORECASE)
            if m:
                num = parse_cn_number(m.group(1))
                if num is not None and float(num) > 0:
                    return (float(num) + 0.5) * 3600.0
        # X天半
        for pat in [
            r"([\d零一二三四五六七八九十百点两]+)\s*(?:个)?\s*半\s*(?:个)?\s*天",
            r"([\d零一二三四五六七八九十百点两]+)\s*天\s*半",
        ]:
            m = re.search(pat, cleaned, re.IGNORECASE)
            if m:
                num = parse_cn_number(m.group(1))
                if num is not None and float(num) > 0:
                    return (float(num) + 0.5) * 86400.0
        # 组合：X天 Y小时 Z分钟 A秒
        day_val = 0.0
        hour_val = 0.0
        min_val = 0.0
        sec_val = 0.0
        matched = False
        week_val = 0.0
        m = re.search(r"([\d零一二三四五六七八九十百点两]+)\s*(?:周|星期|礼拜)", cleaned)
        if m:
            v = parse_cn_number(m.group(1))
            if v is not None:
                week_val = float(v)
                matched = True
        m = re.search(r"([\d零一二三四五六七八九十百点两]+)\s*(?:天|日|days?)", cleaned, re.IGNORECASE)
        if m:
            v = parse_cn_number(m.group(1))
            if v is not None:
                day_val = float(v)
                matched = True
        m = re.search(r"([\d零一二三四五六七八九十百点两]+)\s*(?:个)?\s*(?:小时|钟头|h|hr|hours?|hrs?)", cleaned, re.IGNORECASE)
        if m:
            v = parse_cn_number(m.group(1))
            if v is not None:
                hour_val = float(v)
                matched = True
        m = re.search(r"([\d零一二三四五六七八九十百点两]+)\s*(?:个)?\s*(?:分钟|分|mins?|minutes?)", cleaned, re.IGNORECASE)
        if m:
            v = parse_cn_number(m.group(1))
            if v is not None:
                min_val = float(v)
                matched = True
        m = re.search(r"([\d零一二三四五六七八九十百点两]+)\s*(?:个)?\s*(?:秒钟|秒|secs?|seconds?)", cleaned, re.IGNORECASE)
        if m:
            v = parse_cn_number(m.group(1))
            if v is not None:
                sec_val = float(v)
                matched = True
        if matched:
            total = (
                week_val * 604800.0
                + day_val * 86400.0
                + hour_val * 3600.0
                + min_val * 60.0
                + sec_val
            )
            if total > 0 and math.isfinite(total):
                return total
        return None

    # --------------------------------------------------------- main extractor
    def extract(
        self,
        text: str,
        ctx: TemporalContext,
        full_user_message: Optional[str] = None,
    ) -> Optional[TemporalIR]:
        """主入口：文本 → TemporalIR"""
        if not text or not isinstance(text, str):
            return None
        text_norm = self._preprocess(text)
        full_norm = self._preprocess(full_user_message or "")
        combined = f"{text_norm} {full_norm}".strip()
        if not text_norm and not full_norm:
            return None
        ir = TemporalIR(
            kind=TemporalKind.INSTANT,
            source_text=text,
            timezone_id=ctx.timezone_id,
            reference_instant_iso=ctx.reference_instant.isoformat(),
        )
        ref_local = ctx.local_reference
        found_date = None
        # 绝对 ISO 开头 → 不走相对路径
        if re.match(r"^\d{4}[-/]\d{2}[-/]\d{2}", text_norm):
            try:
                iso_m = re.match(r"^(\d{4})[-/](\d{2})[-/](\d{2})", text_norm)
                if iso_m:
                    found_date = date(int(iso_m.group(1)), int(iso_m.group(2)), int(iso_m.group(3)))
                    ir.resolution_method = "explicit_iso_date"
            except ValueError:
                found_date = None
        # 按优先级尝试各日期解析器
        if found_date is None:
            for parser_name, parser_fn in [
                ("explicit_cn", self._parse_explicit_date),
                ("offset_date", self._parse_offset_date),
                ("boundary", self._parse_boundary_date),
                ("relative_kw", self._parse_relative_date_offset),
                ("weekday", self._parse_weekday_date),
            ]:
                d = parser_fn(text_norm, ctx)
                if d is None and full_norm:
                    d = parser_fn(full_norm, ctx)
                if d is not None:
                    found_date = d
                    ir.resolution_method = parser_name
                    break
        # 日期验证：月日合法？闰年？
        if found_date is not None:
            try:
                # 验证月日合法性（date构造时已做，但再次检查闰年情况）
                _ = date(found_date.year, found_date.month, found_date.day)
            except ValueError as e:
                if "day is out of range" in str(e) or "day must be in" in str(e):
                    if found_date.month == 2 and found_date.day == 29:
                        ir.ambiguities.append(AmbiguityCode.LEAP_YEAR_INVALID)
                    else:
                        ir.ambiguities.append(AmbiguityCode.MONTH_DAY_INVALID)
                    found_date = None
        # 日期 vs 星期 冲突检测
        if found_date is not None:
            # 提取文本中声明的星期
            for wk, wd_val in WEEKDAY_MAP.items():
                if len(wk) >= 2 and wk in combined:
                    actual_wd = found_date.weekday()
                    if actual_wd != wd_val:
                        ir.ambiguities.append(AmbiguityCode.DATE_WEEKDAY_CONFLICT)
                        ir.warnings.append(
                            f"文本声明{wk}({wd_val})与实际日期{found_date}的星期({actual_wd})不一致"
                        )
                    break
        # 解析时间
        search_text = text_norm if re.search(r"(?:点|时|[:：])|现在|当前|立即|此时", text_norm) else combined
        time_result = self._parse_time(search_text)
        is_am, is_pm = self._detect_meridiem(search_text)
        has_explicit_time = False
        if time_result:
            h_raw, mi, s, explicit_flag = time_result
            if explicit_flag:
                has_explicit_time = True
                hour = h_raw
                # AM/PM 归一化
                if is_pm and hour < 12:
                    hour += 12
                elif is_am and hour == 12:
                    hour = 0
                # 12制无 AM/PM，且小时=12且说"中午"则12，否则歧义
                if not is_am and not is_pm and 1 <= h_raw <= 11:
                    # 0-5 默认上午，6-11 默认上午且如果h<8默认上午但歧义
                    if 1 <= h_raw <= 5:
                        pass  # 凌晨，无需修改
                    elif 6 <= h_raw <= 11:
                        # 上午无需转换，但标记歧义（用户可能想说下午）
                        hour = h_raw
                ir.local_time_hour = min(23, max(0, hour))
                ir.local_time_minute = min(59, max(0, mi))
                ir.local_time_second = min(59, max(0, s))
                # 歧义：未明确AM/PM且1-11点间标记
                if not is_am and not is_pm and 1 <= h_raw <= 11:
                    ir.ambiguities.append(AmbiguityCode.MERIDIEM_UNKNOWN)
                    ir.warnings.append(f"'{h_raw}点'未明确上午/下午")
        # 如果没有显式时间但有 "现在/当前"
        if not has_explicit_time and re.search(r"现在|当前|立即|此时", search_text):
            ir.local_time_hour = ref_local.hour
            ir.local_time_minute = ref_local.minute
            ir.local_time_second = ref_local.second
            has_explicit_time = True
        # 如果有日期无时间 → 保留 kind=DATE_ONLY；如果无日期但有时间 → 用参考日期
        if found_date is None and has_explicit_time:
            found_date = ref_local.date()
            ir.resolution_method = ir.resolution_method + "+default_today"
        ir.local_date = found_date
        # 夜间跨天纠偏：基准>=18点且说"凌晨/明早/次日"自动+1天
        if (
            found_date is not None
            and ir.local_time_hour is not None
            and ref_local.hour >= 18
            and re.search(r"凌晨|次日|明早|次晨|明", combined)
        ):
            if found_date == ref_local.date() and 0 <= ir.local_time_hour <= 10:
                ir.local_date = found_date + timedelta(days=1)
        # 时长表达：X小时/分钟后
        m_offset = re.search(
            r"([\d零一二三四五六七八九十两半点]+)\s*(?:个)?\s*(?:小时|钟头|分钟|分|秒|秒钟)\s*(后|之后|以后)",
            combined,
        )
        if m_offset and ir.local_date is not None and ir.local_time_hour is not None:
            n_raw = m_offset.group(1)
            unit_token = m_offset.group(2) if False else None
            n = parse_cn_number(n_raw)
            if n is not None:
                unit_text = m_offset.group(0)
                delta_td = None
                if "小时" in unit_text or "钟头" in unit_text:
                    delta_td = timedelta(hours=float(n))
                elif "分钟" in unit_text or unit_text.count("分") > 0:
                    delta_td = timedelta(minutes=float(n))
                elif "秒" in unit_text:
                    delta_td = timedelta(seconds=float(n))
                if delta_td is not None:
                    base_dt = datetime.combine(ir.local_date, datetime.min.time())
                    base_dt = base_dt.replace(
                        hour=ir.local_time_hour or ref_local.hour,
                        minute=ir.local_time_minute or ref_local.minute,
                        second=ir.local_time_second or ref_local.second,
                    )
                    new_dt = base_dt + delta_td
                    ir.local_date = new_dt.date()
                    ir.local_time_hour = new_dt.hour
                    ir.local_time_minute = new_dt.minute
                    ir.local_time_second = new_dt.second
                    ir.resolution_method = "elapsed_duration_offset"
        # 时长
        dur = self.parse_duration(combined)
        if dur is not None and dur > 0:
            ir.duration_seconds = dur
        # 确定kind
        if ir.local_date is None and ir.duration_seconds is None and ir.local_time_hour is None:
            return None
        if ir.local_date is not None and ir.local_time_hour is not None:
            ir.kind = TemporalKind.INSTANT
        elif ir.local_date is not None:
            ir.kind = TemporalKind.DATE_ONLY
        elif ir.duration_seconds is not None:
            ir.kind = TemporalKind.DURATION
        else:
            return None
        # 去重 NONE ambiguity
        ir.ambiguities = [a for a in ir.ambiguities if a != AmbiguityCode.NONE]
        ir.ambiguities = list(dict.fromkeys(ir.ambiguities))  # 去重保留顺序
        return ir


# ──────────────────────────────────────────────────────────────────────────────
# 6. TemporalNormalizer：IR → 最终标准输出
# ──────────────────────────────────────────────────────────────────────────────

class TemporalNormalizer:
    """将 TemporalIR 归一化为最终标准输出"""

    @staticmethod
    def is_leap_year(year: int) -> bool:
        return calendar.isleap(year)

    @staticmethod
    def validate_local_datetime(ir: TemporalIR) -> tuple[Optional[datetime], list[AmbiguityCode]]:
        """
        验证本地墙钟时间在对应时区下的 DST 合法性。
        返回 (aware_local_datetime, additional_ambiguities)
        """
        if ir.local_date is None or ir.local_time_hour is None:
            return None, []
        tz = ZoneInfo(ir.timezone_id)
        naive_local = datetime(
            ir.local_date.year,
            ir.local_date.month,
            ir.local_date.day,
            ir.local_time_hour,
            ir.local_time_minute,
            ir.local_time_second,
        )
        extra_ambiguities: list[AmbiguityCode] = []
        # DST gap/fold 检测
        candidates_utc = set()
        for fold in (0, 1):
            try:
                aware = naive_local.replace(tzinfo=tz, fold=fold)
                utc_val = aware.astimezone(ZoneInfo("UTC"))
                round_trip = utc_val.astimezone(tz).replace(tzinfo=None)
                if round_trip == naive_local:
                    candidates_utc.add(utc_val)
            except Exception:
                continue
        if len(candidates_utc) == 0:
            extra_ambiguities.append(AmbiguityCode.DST_GAP)
        elif len(candidates_utc) == 2:
            extra_ambiguities.append(AmbiguityCode.DST_FOLD)
        aware = naive_local.replace(tzinfo=tz)
        return aware, extra_ambiguities

    @classmethod
    def normalize(cls, ir: TemporalIR) -> dict[str, Any]:
        """
        返回规范化结果字典：
        {
          "success": bool,
          "iso_local": "YYYY-MM-DDTHH:MM:SS" (无时区后缀，兼容现有系统),
          "iso_utc":   "YYYY-MM-DDTHH:MM:SSZ",
          "iso_with_tz": "带时区偏移的ISO",
          "duration_seconds": float | None,
          "ambiguities": [AmbiguityCode],
          "ambiguity_reasons": [str],
          "warnings": [str],
          "metadata": { ... 溯源信息... }
        }
        """
        result: dict[str, Any] = {
            "success": False,
            "iso_local": None,
            "iso_utc": None,
            "iso_with_tz": None,
            "duration_seconds": ir.duration_seconds,
            "ambiguities": list(ir.ambiguities),
            "ambiguity_reasons": ir.get_ambiguity_reasons(),
            "warnings": list(ir.warnings),
            "metadata": {
                "kind": ir.kind.value,
                "source_text": ir.source_text,
                "timezone_id": ir.timezone_id,
                "timezone_source": ir.timezone_source,
                "resolution_method": ir.resolution_method,
                "reference_instant": ir.reference_instant_iso,
                "leap_year": None,
                "dst_checked": False,
            },
        }
        # 闰年信息
        if ir.local_date:
            result["metadata"]["leap_year"] = cls.is_leap_year(ir.local_date.year)
        # 即时/日期 类型处理
        if ir.kind in (TemporalKind.INSTANT, TemporalKind.DATE_ONLY):
            if ir.kind == TemporalKind.DATE_ONLY:
                # 纯日期：默认午夜，iso_local 以 00:00:00
                iso_local = f"{ir.local_date.isoformat()}T00:00:00"
                result["iso_local"] = iso_local
                result["success"] = True
            else:
                aware_dt, dst_amb = cls.validate_local_datetime(ir)
                result["ambiguities"].extend(dst_amb)
                result["ambiguity_reasons"].extend(
                    TemporalIR(kind=TemporalKind.INSTANT, source_text="", ambiguities=dst_amb).get_ambiguity_reasons()
                )
                result["metadata"]["dst_checked"] = True
                if aware_dt is not None:
                    iso_local = aware_dt.strftime("%Y-%m-%dT%H:%M:%S")
                    result["iso_local"] = iso_local
                    result["iso_with_tz"] = aware_dt.isoformat(timespec="seconds")
                    result["iso_utc"] = aware_dt.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")
                    result["success"] = True
        elif ir.kind == TemporalKind.DURATION:
            result["success"] = True
        # 重新合并去重
        result["ambiguities"] = list(dict.fromkeys(result["ambiguities"]))
        seen = set()
        uniq_reasons = []
        for r in result["ambiguity_reasons"]:
            if r not in seen:
                seen.add(r)
                uniq_reasons.append(r)
        result["ambiguity_reasons"] = uniq_reasons
        return result


# ──────────────────────────────────────────────────────────────────────────────
# 7. 简易公开快捷函数（兼容旧 API + 新 API）
# ──────────────────────────────────────────────────────────────────────────────

_EXTRACTOR = TemporalIntentExtractor()


def parse_temporal(
    text: str,
    base_dt: Optional[datetime] = None,
    full_user_message: Optional[str] = None,
    timezone_id: str = "Asia/Shanghai",
) -> dict[str, Any]:
    """
    新 API：统一入口。
    返回 TemporalNormalizer.normalize 的结果字典。
    """
    ctx = TemporalContext.create(reference_instant=base_dt, timezone_id=timezone_id)
    ir = _EXTRACTOR.extract(text, ctx, full_user_message=full_user_message)
    if ir is None:
        # 尝试 dateparser 兜底（如果已安装）
        try:
            import dateparser
            if base_dt is None:
                from .simulated_time import get_current_datetime
                base_dt = get_current_datetime()
            dp_res = dateparser.parse(text or full_user_message or "", settings={"RELATIVE_BASE": base_dt})
            if dp_res:
                ir_fallback = TemporalIR(
                    kind=TemporalKind.INSTANT,
                    source_text=text,
                    local_date=dp_res.date(),
                    local_time_hour=dp_res.hour,
                    local_time_minute=dp_res.minute,
                    local_time_second=dp_res.second,
                    timezone_id=timezone_id,
                    resolution_method="dateparser_fallback",
                    reference_instant_iso=base_dt.isoformat(),
                )
                return TemporalNormalizer.normalize(ir_fallback)
        except Exception:
            pass
        return {
            "success": False,
            "iso_local": None,
            "iso_utc": None,
            "iso_with_tz": None,
            "duration_seconds": None,
            "ambiguities": [AmbiguityCode.INSUFFICIENT_INFO],
            "ambiguity_reasons": ["无法解析该时间表达"],
            "warnings": [],
            "metadata": {"resolution_method": "failed", "kind": "failed"},
        }
    return TemporalNormalizer.normalize(ir)


def parse_relative_datetime_v2(
    text: Optional[str],
    base_dt: Optional[datetime] = None,
    full_user_message: Optional[str] = None,
) -> Optional[str]:
    """
    兼容旧 API：返回 "YYYY-MM-DDTHH:MM:SS" 或 None。
    仅当解析成功且无致命歧义时返回字符串。
    """
    if not text:
        return None
    result = parse_temporal(text, base_dt, full_user_message)
    if not result["success"]:
        return None
    iso = result["iso_local"]
    # 存在致命歧义（除了 MERIDIEM_UNKNOWN 可继续用默认值），禁止致命歧义直接返回
    fatal = {AmbiguityCode.DATE_WEEKDAY_CONFLICT, AmbiguityCode.MONTH_DAY_INVALID,
             AmbiguityCode.LEAP_YEAR_INVALID, AmbiguityCode.INTERVAL_INVERTED,
             AmbiguityCode.DST_GAP}
    if any(a in fatal for a in result["ambiguities"]):
        return None
    return iso


def parse_duration_to_seconds_v2(text: Optional[str]) -> Optional[float]:
    """兼容旧 API：时长解析"""
    return TemporalIntentExtractor.parse_duration(text)
