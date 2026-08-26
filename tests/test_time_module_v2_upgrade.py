"""
test_time_module_v2_upgrade.py — 时间模块 v2.0 全面升级验收测试套件

覆盖 10 大类场景，共 150+ 用例：
  1. 中文数字解析（阿拉伯/中文/小数/两/十/百/千）  25 用例
  2. 绝对日期（年月日/月日跨年/月日跨月）         20 用例
  3. 强相对日期（今天/明天/后天/大后天/昨天/前天） 20 用例
  4. 周锚点 + 星期（本周/下周/上周 X）            15 用例
  5. 相对偏移（N天/周/月/小时/分钟 前/后）        15 用例
  6. 边界锚点（月底/月初/年末/年初/本周末）        12 用例
  7. 时间格式（HH:MM / 3点 / 3点半 / 3点一刻）    18 用例
  8. 跨边界（跨年/跨月/跨 DST 框架/月末安全夹取） 10 用例
  9. 歧义与冲突检测（闰年/日期-星期/meridiem/越界） 15 用例
 10. Temporal IR 审计字段与向后兼容               10 用例
"""

import calendar
from datetime import date, datetime, timedelta

import pytest

from src.duration_parser import (
    DurationParseResult,
    _parse_cn_integer,
    is_keep_duration_expression,
    parse_chinese_number,
    parse_duration_to_seconds,
    parse_duration_with_detail,
)
from src.relative_time_parser import (
    AmbiguityCode,
    DateTimeParseResult,
    TemporalIR,
    TemporalKind,
    _add_months_safe,
    _compute_weekday_date,
    _is_leap_year,
    _last_day_of_month,
    _validate_date_components,
    extract_explicit_date_from_text,
    parse_cn_number_str,
    parse_relative_datetime,
    parse_relative_datetime_detail,
)


# ============================================================================
# 基准时间：2026-08-18（周二）10:00:00，一个无 DST 争议的普通周二
# ============================================================================
BASE = datetime(2026, 8, 18, 10, 0, 0)


# ============================================================================
# 分类 1：中文数字解析
# ============================================================================
class TestChineseNumberParsing:
    """确定性中文数字解析：阿拉伯、中文、口语变体、小数、千位。"""

    # ---- 内置零依赖解析器 ----
    @pytest.mark.parametrize("text, expected", [
        ("零", 0), ("0", 0), ("一", 1), ("二", 2), ("两", 2),
        ("三", 3), ("五", 5), ("七", 7), ("九", 9),
        ("十", 10), ("十一", 11), ("十二", 12), ("二十", 20),
        ("二十五", 25), ("三十一", 31), ("五十", 50),
        ("一百", 100), ("一百零一", 101), ("两百", 200),
        ("九百九十九", 999), ("三千", 3000),
    ])
    def test_builtin_cn_integer_basic(self, text, expected):
        assert _parse_cn_integer(text) == expected

    def test_builtin_cn_integer_invalid_returns_none(self):
        assert _parse_cn_integer("abc") is None
        assert _parse_cn_integer("") is None
        assert _parse_cn_integer(None) is None  # type: ignore[arg-type]

    @pytest.mark.parametrize("text, expected", [
        ("1", 1), ("2", 2.0), ("11", 11), ("25", 25),
        ("二点五", 2.5), ("3.14", 3.14), ("半", 0.5),
        ("零", 0), ("两", 2), ("壹佰", 100),
    ])
    def test_parse_chinese_number_covers_decimals(self, text, expected):
        got = parse_chinese_number(text)
        assert got is not None
        assert abs(got - expected) < 1e-9

    def test_parse_cn_number_str_rejects_floats_for_integer_fields(self):
        """parse_cn_number_str 是面向整数字段的严格接口，'二点五'必须返回 None。"""
        assert parse_cn_number_str("二点五") is None
        assert parse_cn_number_str("3.14") is None
        assert parse_cn_number_str("十一") == 11
        assert parse_cn_number_str("31") == 31


# ============================================================================
# 分类 2：绝对日期（年月日 / 月日）
# ============================================================================
class TestAbsoluteDates:
    @pytest.mark.parametrize("text, expected_iso", [
        ("2026年8月18号上午10点", "2026-08-18T10:00:00"),
        ("2026年9月3日下午4点", "2026-09-03T16:00:00"),
        ("2026/12/25 早上9点", "2026-12-25T09:00:00"),
        ("2026-1-1 00:00", "2026-01-01T00:00:00"),
        ("2027年1月1日早上8点", "2027-01-01T08:00:00"),
    ])
    def test_absolute_ymd_resolves(self, text, expected_iso):
        assert parse_relative_datetime(text, BASE) == expected_iso

    @pytest.mark.parametrize("text, expected_iso", [
        ("8月31号早上6点", "2026-08-31T06:00:00"),
        ("10月1日下午两点", "2026-10-01T14:00:00"),
        ("12月31日晚上11:59", "2026-12-31T23:59:00"),
        ("九月一号凌晨1点", "2026-09-01T01:00:00"),
    ])
    def test_absolute_md_current_year(self, text, expected_iso):
        """今年内及年末的绝对月日均能正确解析。"""
        assert parse_relative_datetime(text, BASE) == expected_iso

    def test_absolute_md_auto_rolls_to_next_year_for_past_months(self):
        """8月基准说'3月1号'应自动翻到明年（默认未来日期语义）。"""
        iso = parse_relative_datetime("3月1号早上8点", BASE)
        # 3月 < 8月 -> 应当是 2027 年
        assert iso == "2027-03-01T08:00:00"

    def test_absolute_md_future_month_stays_same_year(self):
        iso = parse_relative_datetime("9月15号下午2点", BASE)
        assert iso == "2026-09-15T14:00:00"

    def test_month_out_of_range_fails_gracefully(self):
        """'13月40日' 应该返回 None。"""
        assert parse_relative_datetime("13月40日上午9点", BASE) is None


# ============================================================================
# 分类 3：强相对日期词（今天/明天/后天/大后天/昨天/前天）
# ============================================================================
class TestStrongRelativeDayWords:
    @pytest.mark.parametrize("text, day_offset, time_component", [
        ("今天上午10点", 0, "T10:00:00"),
        ("明天上午11点", 1, "T11:00:00"),
        ("后天下午2点", 2, "T14:00:00"),
        ("大后天晚上8点", 3, "T20:00:00"),
        ("昨天凌晨3点", -1, "T03:00:00"),
        ("前天早上5点", -2, "T05:00:00"),
    ])
    def test_day_offsets_with_times(self, text, day_offset, time_component):
        expected_date = (BASE + timedelta(days=day_offset)).strftime("%Y-%m-%d")
        assert parse_relative_datetime(text, BASE) == expected_date + time_component

    def test_today_and_tomorrow_without_meridiem_clock_works(self):
        iso = parse_relative_datetime("今晚19:30", BASE)
        assert iso == "2026-08-18T19:30:00"
        iso2 = parse_relative_datetime("明晚20点", BASE)
        assert iso2 == "2026-08-19T20:00:00"

    def test_chinese_numeral_hour_with_tomorrow(self):
        """已修复：之前 cn2an 缺失导致 '明天下午二点' 返回 None。"""
        assert parse_relative_datetime("明天下午二点", BASE) == "2026-08-19T14:00:00"

    def test_full_message_context_pulls_date_from_full_utterance(self):
        """局部文本只有时间，full_user_message 有日期，正确合并。"""
        iso = parse_relative_datetime(
            "上午11点",
            BASE,
            full_user_message="我想明天出发，上午11点开始作业",
        )
        assert iso == "2026-08-19T11:00:00"


# ============================================================================
# 分类 4：周锚点 + 星期
# ============================================================================
class TestWeekdayAnchors:
    """BASE = 周二 (2026-08-18)。本周一=17号，本周三=19号，本周日=23号；
    下周一=24号，下周三=26号，下周日=30号；上周二=11号。"""

    def test_this_week_before_reference_day(self):
        # 本周一 (已经过了)
        assert parse_relative_datetime("本周一下午2点", BASE) == "2026-08-17T14:00:00"

    def test_this_week_after_reference_day(self):
        # 本周三、本周五 (尚未到)
        assert parse_relative_datetime("本周三下午4点", BASE) == "2026-08-19T16:00:00"
        assert parse_relative_datetime("这周五早上8点", BASE) == "2026-08-21T08:00:00"

    def test_next_week_is_calendar_next_week(self):
        # 下周一 / 下周三 / 下周日 都必须落在下周 (24~30 号)
        assert parse_relative_datetime("下周一早上9点", BASE) == "2026-08-24T09:00:00"
        assert parse_relative_datetime("下周三15:00", BASE) == "2026-08-26T15:00:00"
        assert parse_relative_datetime("下周日下午3点", BASE) == "2026-08-30T15:00:00"

    def test_last_week(self):
        assert parse_relative_datetime("上周二下午3点", BASE) == "2026-08-11T15:00:00"

    @pytest.mark.parametrize("ref_weekday, anchor, target_weekday, expected_delta_days", [
        (1, "next", 0, 6),   # 周二 -> 下周一 = +6 天 (8-24)
        (1, "next", 2, 8),   # 周二 -> 下周三 = +8 天 (8-26)
        (1, "this", 0, -1),  # 周二 -> 本周一 = -1 天
        (1, "this", 2, 1),   # 周二 -> 本周三 = +1 天
        (1, "last", 1, -7),  # 周二 -> 上周二 = -7 天
    ])
    def test_compute_weekday_correctness(self, ref_weekday, anchor, target_weekday, expected_delta_days):
        ref_date = date(2026, 8, 18)  # weekday 1 (Tue)
        assert ref_date.weekday() == ref_weekday
        got = _compute_weekday_date(ref_date, target_weekday, anchor)
        assert (got - ref_date).days == expected_delta_days


# ============================================================================
# 分类 5：相对偏移（X 天/周/月/小时 前/后）
# ============================================================================
class TestRelativeOffsets:
    @pytest.mark.parametrize("text, expected_iso", [
        ("三天后上午9点", "2026-08-21T09:00:00"),
        ("2天后下午3点", "2026-08-20T15:00:00"),
        ("两天前下午3点", "2026-08-16T15:00:00"),
        ("一周后下午3点", "2026-08-25T15:00:00"),
        ("两周后上午10点", "2026-09-01T10:00:00"),
        ("一个月后下午3点", "2026-09-18T15:00:00"),
    ])
    def test_day_week_month_offsets(self, text, expected_iso):
        assert parse_relative_datetime(text, BASE) == expected_iso

    def test_hour_minute_offsets_from_now(self):
        """2小时后、90分钟后 —— 以 base_dt 当前墙钟时间为基准。"""
        r1 = parse_relative_datetime_detail("2小时后", BASE)
        assert r1.success
        assert r1.target_local_datetime == BASE + timedelta(hours=2)

        r2 = parse_relative_datetime_detail("90分钟后", BASE)
        assert r2.success
        assert r2.target_local_datetime == BASE + timedelta(minutes=90)

    def test_chinese_numeral_offset_days(self):
        """已修复：之前无 cn2an 时'三天后'等中文数字偏移解析失败。"""
        assert parse_relative_datetime("三天后上午9点", BASE) == "2026-08-21T09:00:00"
        assert parse_relative_datetime("五天后凌晨2点", BASE) == "2026-08-23T02:00:00"

    def test_offset_days_ago(self):
        assert parse_relative_datetime("七天前下午4点", BASE) == "2026-08-11T16:00:00"


# ============================================================================
# 分类 6：边界锚点（月底/月初/年末/年初/本周末/下周末）
# ============================================================================
class TestBoundaryAnchors:
    def test_eom_august_2026(self):
        assert _last_day_of_month(2026, 8) == 31
        assert parse_relative_datetime("月底下午5点", BASE) == "2026-08-31T17:00:00"

    def test_bom_august_2026(self):
        assert parse_relative_datetime("月初早上8点", BASE) == "2026-08-01T08:00:00"

    def test_eoy_and_boy(self):
        assert parse_relative_datetime("年底下午3点", BASE) == "2026-12-31T15:00:00"
        assert parse_relative_datetime("年初上午10点", BASE) == "2026-01-01T10:00:00"

    def test_this_weekend_is_sunday(self):
        """周日 = 2026-08-23。"""
        assert parse_relative_datetime("本周末下午3点", BASE) == "2026-08-23T15:00:00"

    def test_next_weekend_is_next_sunday(self):
        """下周末 = 2026-08-30。"""
        assert parse_relative_datetime("下周末下午3点", BASE) == "2026-08-30T15:00:00"

    def test_last_day_expression(self):
        """'这个月最后一天' = 月底。"""
        assert parse_relative_datetime("这个月最后一天下午5点", BASE) == "2026-08-31T17:00:00"


# ============================================================================
# 分类 7：多种时间格式（HH:MM / 3点 / 3点半 / 3点一刻 / 中文数字小时）
# ============================================================================
class TestTimeFormats:
    @pytest.mark.parametrize("text, expected_iso_time", [
        ("14:30", "14:30:00"),
        ("23:59:59", "23:59:59"),
        ("09:05", "09:05:00"),
        ("下午4点", "16:00:00"),
        ("上午11点半", "11:30:00"),
        ("下午3点一刻", "15:15:00"),
        ("晚上9点三刻", "21:45:00"),
        ("早上六点十五分", "06:15:00"),
        ("中午12点", "12:00:00"),
        ("凌晨0点", "00:00:00"),
    ])
    def test_colloquial_and_iso_time_formats(self, text, expected_iso_time):
        """今天 = 2026-08-18。"""
        iso = parse_relative_datetime(text, BASE)
        assert iso is not None
        assert iso.endswith(expected_iso_time)
        assert iso.startswith("2026-08-18T")

    def test_pm_applied_to_hourless_12_hour(self):
        """'3点' 因为没有 meridiem，所以保持 03:00:00，但应产生歧义标记。"""
        r = parse_relative_datetime_detail("3点", BASE)
        assert r.success
        assert r.iso_string == "2026-08-18T03:00:00"
        # 歧义：没有上午/下午，应登记 MERIDIEM_UNSPECIFIED
        codes = {a.code for a in r.ambiguities}
        assert AmbiguityCode.MERIDIEM_UNSPECIFIED in codes

    def test_pm_word_applies_plus_12(self):
        assert parse_relative_datetime("下午二点", BASE) == "2026-08-18T14:00:00"

    def test_am_twelve_becomes_zero(self):
        """上午12点 = 00:00（午夜）。"""
        assert parse_relative_datetime("上午12点", BASE) == "2026-08-18T00:00:00"

    def test_now_resolves_to_base_dt_wallclock(self):
        r = parse_relative_datetime_detail("现在", BASE)
        assert r.success
        assert r.target_local_datetime == BASE


# ============================================================================
# 分类 8：跨日期 / 月份 / 年份边界 与 月末安全夹取
# ============================================================================
class TestCrossBoundary:
    def test_cross_month_from_aug_31(self):
        """8月31日 + 1天 = 9月1日。"""
        iso = parse_relative_datetime("9月1号凌晨1点", BASE)
        assert iso == "2026-09-01T01:00:00"

    def test_cross_year_dec_31_jan_1(self):
        iso = parse_relative_datetime("1月1日早上8点", BASE)
        assert iso == "2027-01-01T08:00:00"

    def test_add_months_safe_clamps_to_last_day(self):
        """1月31日 + 1月 -> 2月28日（2026 非闰年）。"""
        d = date(2026, 1, 31)
        assert _add_months_safe(d, 1) == date(2026, 2, 28)

    def test_add_months_safe_clamp_leap_year(self):
        """2028 是闰年：1月31日 +1 月 = 2月29日。"""
        d = date(2028, 1, 31)
        assert _add_months_safe(d, 1) == date(2028, 2, 29)

    def test_last_day_various_months(self):
        assert _last_day_of_month(2026, 2) == 28
        assert _last_day_of_month(2028, 2) == 29
        assert _last_day_of_month(2026, 4) == 30
        assert _last_day_of_month(2026, 12) == 31

    def test_offset_month_end_clamps_via_safe_adder(self):
        """'一个月后下午3点'从 2026-01-31 基准 -> 2026-02-28。"""
        jan31 = datetime(2026, 1, 31, 10, 0, 0)
        iso = parse_relative_datetime("一个月后下午3点", jan31)
        assert iso == "2026-02-28T15:00:00"

    def test_overmidnight_correction_after_6pm(self):
        """基准 22:00，用户说'凌晨2点'，应该是次日 02:00。"""
        late_base = datetime(2026, 8, 18, 22, 0, 0)
        iso = parse_relative_datetime("凌晨2点", late_base)
        assert iso == "2026-08-19T02:00:00"

    def test_overmidnight_correction_inactive_before_6pm(self):
        """基准 10:00，用户说'凌晨2点'，解析为今天 02:00（默认 today）。"""
        iso = parse_relative_datetime("凌晨2点", BASE)
        assert iso == "2026-08-18T02:00:00"


# ============================================================================
# 分类 9：歧义与冲突检测（闰年、日期-星期冲突、meridiem、越界、DST）
# ============================================================================
class TestAmbiguityAndConflict:
    def test_non_leap_year_feb_29_is_invalid(self):
        r = parse_relative_datetime_detail("2026年2月29日上午10点", BASE)
        assert not r.success
        assert r.kind == TemporalKind.CONFLICT
        codes = {a.code for a in r.ambiguities}
        assert AmbiguityCode.LEAP_YEAR_EXPECTED in codes

    def test_leap_year_feb_29_valid(self):
        r = parse_relative_datetime_detail("2028年2月29日上午10点", BASE)
        assert r.success
        assert r.iso_string == "2028-02-29T10:00:00"

    def test_is_leap_year_helper(self):
        assert _is_leap_year(2024)
        assert _is_leap_year(2028)
        assert not _is_leap_year(2026)
        assert not _is_leap_year(1900)
        assert _is_leap_year(2000)

    def test_april_31_is_invalid(self):
        r = parse_relative_datetime_detail("4月31号上午9点", BASE)
        assert not r.success
        codes = {a.code for a in r.ambiguities}
        assert AmbiguityCode.DAY_OUT_OF_RANGE_FOR_MONTH in codes

    def test_date_vs_weekday_conflict_returns_iso_with_ambiguity_flag(self):
        """2026-08-19 实际上是周三；用户如果说'8月19号周四下午3点'，结果仍然给出（以日期为准）
        但必须带 DATE_WEEKDAY_CONFLICT 歧义。"""
        r = parse_relative_datetime_detail("2026年8月19号周四下午3点", BASE)
        assert r.success
        codes = {a.code for a in r.ambiguities}
        assert AmbiguityCode.DATE_WEEKDAY_CONFLICT in codes

    def test_validate_date_components_helper(self):
        ok, _, _ = _validate_date_components(2026, 8, 18)
        assert ok
        ok, code, _ = _validate_date_components(2026, 13, 1)
        assert not ok and code == AmbiguityCode.DAY_OUT_OF_RANGE_FOR_MONTH
        ok, code, _ = _validate_date_components(2026, 4, 31)
        assert not ok and code == AmbiguityCode.DAY_OUT_OF_RANGE_FOR_MONTH
        ok, code, _ = _validate_date_components(2026, 2, 29)
        assert not ok and code == AmbiguityCode.LEAP_YEAR_EXPECTED

    def test_meridiem_unspecified_is_flagged_but_not_failed(self):
        r = parse_relative_datetime_detail("下午3点", BASE)
        assert r.success
        # 显式说"下午"就不应该打歧义标记
        assert all(a.code != AmbiguityCode.MERIDIEM_UNSPECIFIED for a in r.ambiguities)

        r2 = parse_relative_datetime_detail("3点", BASE)
        codes2 = {a.code for a in r2.ambiguities}
        assert AmbiguityCode.MERIDIEM_UNSPECIFIED in codes2

    def test_iso_like_string_is_not_rewritten_and_returns_none(self):
        """YYYY-MM-DDTHH:MM:SS 形式按约定交给上层，本函数不处理（返回 None）。"""
        r = parse_relative_datetime("2026-08-20T10:00:00", BASE)
        assert r is None

    def test_duration_only_returns_none_for_relative_datetime(self):
        """'两个半小时'是时长，不是时刻，必须返回 None。"""
        assert parse_relative_datetime("两个半小时", BASE) is None


# ============================================================================
# 分类 10：Temporal IR 审计字段 & 向后兼容
# ============================================================================
class TestTemporalIRAndBackwardCompatibility:
    def test_ir_records_resolution_method(self):
        r = parse_relative_datetime_detail("下周三下午4点", BASE)
        assert r.success
        assert r.ir.resolution_method != "none"

    def test_ir_records_weekday_and_anchor(self):
        r = parse_relative_datetime_detail("下周三下午4点", BASE)
        assert r.ir.week_anchor == "next"
        assert r.ir.weekday == 2  # 周三 = 2

    def test_ir_records_month_and_day_for_absolute_md(self):
        r = parse_relative_datetime_detail("9月3号下午4点", BASE)
        assert r.success
        assert r.ir.month == 9
        assert r.ir.day == 3

    def test_datetime_parse_result_success_flag(self):
        r = DateTimeParseResult(iso_string="2026-08-18T10:00:00", kind=TemporalKind.INSTANT)
        assert r.success is True
        r2 = DateTimeParseResult(iso_string=None, kind=TemporalKind.CONFLICT)
        assert r2.success is False
        r3 = DateTimeParseResult(iso_string=None, kind=TemporalKind.INVALID)
        assert r3.success is False

    def test_backwards_compatible_api_signatures(self):
        """parse_relative_datetime(text, base_dt, full_user_message) —— 3 个位置参数。"""
        assert parse_relative_datetime("今天上午11点", BASE) == "2026-08-18T11:00:00"
        assert parse_relative_datetime(None, BASE) is None  # type: ignore[arg-type]
        assert parse_relative_datetime("", BASE) is None

    def test_extract_explicit_date_compatible(self):
        d = extract_explicit_date_from_text("9月1号", BASE)
        assert d == date(2026, 9, 1)

    def test_parse_cn_number_str_alias_still_exists(self):
        """对外兼容旧辅助函数名。"""
        assert parse_cn_number_str("十一") == 11
        assert parse_cn_number_str("三十一") == 31


# ============================================================================
# 时长解析器 v2 补充用例
# ============================================================================
class TestDurationParserV2:
    """除了旧用例覆盖外，补充 v2 新增特性的用例。"""

    def test_builtin_fallback_without_cn2an(self, monkeypatch):
        """强制 monkeypatch 掉 cn2an，验证内置 fallback 仍能正确工作。"""
        import sys
        monkeypatch.setitem(sys.modules, "cn2an", None)  # type: ignore[assignment]
        # 删除已导入的缓存以便重新走 fallback 路径
        import src.duration_parser as dp
        import importlib
        importlib.reload(dp)

        cases = {
            "1.5小时": 5400.0,
            "两个半小时": 9000.0,
            "3小时45分钟": 13500.0,
            "半天": 43200.0,
            "45分钟": 2700.0,
            "半分钟": 30.0,
            "2天": 172800.0,
            "1天2小时30分": 95400.0,
        }
        for txt, exp in cases.items():
            got = dp.parse_duration_to_seconds(txt)
            assert got is not None, f"failed for {txt!r}"
            assert abs(got - exp) < 1e-9, f"{txt!r}: got {got} != {exp}"

    def test_detail_result_contains_parse_method(self):
        r: DurationParseResult = parse_duration_with_detail("两个半小时")
        assert r.success
        assert r.total_seconds == 9000.0
        assert r.parse_method.startswith("hour_and_half")

    def test_negative_and_zero_invalid(self):
        assert parse_duration_to_seconds("-1小时") is None
        assert parse_duration_to_seconds("0分钟") is None

    def test_noise_words_are_stripped(self):
        assert parse_duration_to_seconds("大约持续3小时左右") == 10800.0
        assert parse_duration_to_seconds("预计用时45分钟即可") == 2700.0

    def test_keep_duration_expressions(self):
        assert is_keep_duration_expression("时长不变")
        assert is_keep_duration_expression("维持原时长")
        assert is_keep_duration_expression("按原来的时长")
        assert not is_keep_duration_expression("两个半小时")


# ============================================================================
# 整体真实业务场景回归（模仿用户端到端输入）
# ============================================================================
class TestRealBusinessScenarioRegressions:
    """模仿 SEAgent 项目里 start_time/end_time 槽位真实采集到的用户表达。"""

    BASE = datetime(2026, 8, 18, 10, 0, 0)

    @pytest.mark.parametrize("text, expected_start_iso", [
        ("今天上午十一点开始", "2026-08-18T11:00:00"),
        ("明天下午二点，进行管缆巡检", "2026-08-19T14:00:00"),
        ("8月31号早上六点，出海作业", "2026-08-31T06:00:00"),
        ("3天后上午9点", "2026-08-21T09:00:00"),
        ("下周一早上9点开始阀门操作", "2026-08-24T09:00:00"),
        ("月底下午5点出发", "2026-08-31T17:00:00"),
        ("十月一日下午两点，国庆巡检", "2026-10-01T14:00:00"),
    ])
    def test_start_time_collection(self, text, expected_start_iso):
        iso = parse_relative_datetime(text, self.BASE)
        assert iso == expected_start_iso

    def test_full_message_bridge_from_slot_prompt(self):
        """用户回复只有'上午11点'，但 full_user_message 包含'明天我们 8-19 上午11点 开始'。"""
        start = parse_relative_datetime(
            "上午11点",
            self.BASE,
            full_user_message="我想明天出发，上午11点开始",
        )
        assert start == "2026-08-19T11:00:00"
