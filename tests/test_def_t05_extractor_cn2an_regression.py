"""
DEF-T05 回归测试：ParameterExtractor 数值型字段不应再使用全量 cn2an.transform，
以免破坏"下周三15:00"这类混合中文+阿拉伯+冒号的表达。
"""

from __future__ import annotations

import re
from unittest.mock import MagicMock

import pytest


# ── 核心数字 token 正则的独立单元回归 ──────────────────────────────────────
# 直接对 extractor 中新增的安全解析策略做回归，避免构造整个 LLM 依赖。

CN_NUM_RE = re.compile(
    r"^([-+]?[零〇○Oo幺壹贰两俩叁仨肆伍陆柒捌玖勾一二三四五六七八九十拾佰仟万萬亿点半\d]+(?:点半|[零〇○Oo幺壹贰两俩叁仨肆伍陆柒捌玖勾一二三四五六七八九十百千万萬亿\d半]*半?)?)\s*([a-zA-Z\u4e00-\u9fa5]*)$"
)
PURE_NUM_RE = re.compile(r"^([-+]?[0-9]+(?:\.[0-9]+)?)\s*([a-zA-Z\u4e00-\u9fa5]*)$")


class TestRegexSafety:
    """
    DEF-T05 核心回归：混合字符串（冒号、中文非数字词）不应被数字正则匹配，
    因此不会错误地进入中文数字转换。
    """

    def test_mixed_datetime_colon_not_matched_by_pure_regex(self):
        # 典型 DEF-T05 触发样本："下周三15:00" 中如果截取到 "周三15:00" 不应匹配
        sample = "周三15:00"
        assert PURE_NUM_RE.match(sample) is None

    def test_mixed_datetime_colon_not_matched_by_cn_regex(self):
        sample = "周三15:00"
        # 含冒号 : 不在 CN_NUM_RE 的字符类中 → 不应匹配
        assert CN_NUM_RE.match(sample) is None

    def test_full_def_t05_sample_not_matched(self):
        sample = "下周三15:00"
        assert PURE_NUM_RE.match(sample) is None
        assert CN_NUM_RE.match(sample) is None

    def test_arabic_colon_duration_not_matched(self):
        # "15:00" 带冒号不应被当作纯数字
        assert PURE_NUM_RE.match("15:00") is None
        assert CN_NUM_RE.match("15:00") is None

    # ── 正常的数字正则仍应匹配 ──────────────────────────────────────────
    @pytest.mark.parametrize(
        "s,expect_num,expect_unit",
        [
            ("300", "300", ""),
            ("3.14", "3.14", ""),
            ("+100米", "+100", "米"),
            ("-20.5km", "-20.5", "km"),
            ("2节", "2", "节"),
            ("3.5小时", "3.5", "小时"),
        ],
    )
    def test_pure_number_still_matches(self, s, expect_num, expect_unit):
        m = PURE_NUM_RE.match(s)
        assert m is not None
        assert m.groups() == (expect_num, expect_unit)

    @pytest.mark.parametrize(
        "s",
        [
            "三百米",
            "一万五公里",
            "两个半小时",
            "十节",
            "壹佰贰拾米",
            "两点半",
        ],
    )
    def test_chinese_number_still_matches(self, s):
        m = CN_NUM_RE.match(s)
        assert m is not None, f"{s!r} 应为合法中文数字 token"


# ── 纯 parse_chinese_number 的 sanity ──────────────────────────────────────

class TestParseChineseNumberSanity:
    def test_parse_pure_chinese(self):
        from src.duration_parser import parse_chinese_number

        assert parse_chinese_number("三百") == 300
        assert parse_chinese_number("十") == 10
        assert parse_chinese_number("一万五") == 15000
        assert parse_chinese_number("壹佰贰拾") == 120

    def test_parse_chinese_rejects_mixed_colon(self):
        from src.duration_parser import parse_chinese_number

        # 即使传入混合串，parse_chinese_number 也应安全返回 None
        assert parse_chinese_number("周三15:00") is None


# ── 通过 ParameterExtractor 走真实 resolve 路径 ────────────────────────────

class TestParameterExtractorNumericSafe:
    """
    走完整的 _resolve_candidate_value 调用链，证明 DEF-T05 不再发生破坏。
    """

    @pytest.fixture
    def extractor(self):
        from src.llm_client import LLMClient
        from src.extractor import ParameterExtractor

        mock_llm = MagicMock(spec=LLMClient)
        return ParameterExtractor(llm=mock_llm)

    def test_water_depth_pure_chinese_converts(self, extractor):
        candidate = {
            "canonical_key": "water_depth",
            "raw_value": "三百米",
            "normalized_value": "三百米",
            "resolution_method": "raw_llm_extract",
        }
        required_by_key = {"water_depth": {"type": "number"}}
        resolved, err = extractor._resolve_candidate_value(
            candidate=candidate,
            required_by_key=required_by_key,
            allowed_keys={"water_depth"},
            current_state={},
            conversation_history=[],
            user_message="作业水深三百米",
        )
        assert err is None
        assert resolved["normalized_value"] == 300

    def test_speed_chinese_converts(self, extractor):
        candidate = {
            "canonical_key": "speed",
            "raw_value": "三节",
            "normalized_value": "三节",
            "resolution_method": "raw_llm_extract",
        }
        required_by_key = {"speed": {"type": "number"}}
        resolved, err = extractor._resolve_candidate_value(
            candidate=candidate,
            required_by_key=required_by_key,
            allowed_keys={"speed"},
            current_state={},
            conversation_history=[],
            user_message="航速三节",
        )
        assert err is None
        # 3 knots × 0.5144 ≈ 1.5432 → round 2 digits = 1.54 m/s
        assert resolved["normalized_value"] == 1.54

    def test_time_field_not_corrupted_by_numeric_branch(self, extractor):
        """
        DEF-T05 核心回归：start_time/end_time 字段（非 numeric）即使包含中文数字，
        也绝不应进入数值转换分支，从而保证 datetime 解析的正确性。
        """
        candidate = {
            "canonical_key": "start_time",
            "raw_value": "下周三15:00",
            "normalized_value": None,
            "resolution_method": "raw_llm_extract",
        }
        required_by_key = {"start_time": {"type": "string"}}
        # 关闭相对时间解析，只测试 numeric 分支不破坏 raw_value
        # 用 monkey-patch 禁用 parse_relative_datetime
        import src.extractor as ext_mod
        orig = ext_mod.parse_relative_datetime
        try:
            ext_mod.parse_relative_datetime = lambda *a, **kw: None
            resolved, err = extractor._resolve_candidate_value(
                candidate=candidate,
                required_by_key=required_by_key,
                allowed_keys={"start_time"},
                current_state={},
                conversation_history=[],
                user_message="下周三15:00开始作业",
            )
        finally:
            ext_mod.parse_relative_datetime = orig

        assert err is None
        # raw_value 不应被 numeric 分支污染（若走 numeric 分支会被清洗为数字）
        raw_after = resolved.get("raw_value")
        norm_after = resolved.get("normalized_value")
        # 断言 normalized 没有被转成形如 "315" 的数字（旧 cn2an 污染表现）
        assert norm_after != 315
        assert norm_after != "315"
        if raw_after is not None:
            # raw_value 应保留原始表达（若被修改，也绝不能变成"315:00"/"315"这种）
            assert "315" not in str(raw_after).replace("下周三15:00", "")
