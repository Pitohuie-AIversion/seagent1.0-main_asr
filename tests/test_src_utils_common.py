"""
单元测试：src/utils/common.py 公共工具函数
验证所有抽离的工具函数行为正确，与原重复实现保持一致。
"""

import pytest

from src.utils import (
    validate_uuid4,
    validate_regex_fullmatch,
    is_path_safe,
    is_finite_number,
    is_exact_int,
)


# ── validate_uuid4 ──────────────────────────────────────────────────────────

class TestValidateUUID4:
    def test_valid_uuid4(self):
        valid = "a1b2c3d4-1234-4567-89ab-cdef01234567"
        assert validate_uuid4(valid) is True

    def test_valid_uuid4_zeros(self):
        assert validate_uuid4("00000000-0000-4000-8000-000000000000") is True

    def test_wrong_version_uuid1(self):
        # version=1, not 4
        assert validate_uuid4("a1b2c3d4-1234-1100-89ab-cdef01234567") is False

    def test_empty_string(self):
        assert validate_uuid4("") is False

    def test_none(self):
        assert validate_uuid4(None) is False

    def test_non_string(self):
        assert validate_uuid4(12345) is False
        assert validate_uuid4(["a1b2c3d4-1234-4567-89ab-cdef01234567"]) is False

    def test_malformed_uuid(self):
        assert validate_uuid4("not-a-uuid-at-all") is False

    def test_uppercase_rejected(self):
        # 必须规范小写：大写被 str(parsed) 规范化但原值不匹配
        assert validate_uuid4("A1B2C3D4-1234-4567-89AB-CDEF01234567") is False


# ── validate_regex_fullmatch ────────────────────────────────────────────────

class TestValidateRegexFullmatch:
    def test_match_digits(self):
        assert validate_regex_fullmatch("TI1234567890", r"TI\d{10,}") is True

    def test_no_match(self):
        assert validate_regex_fullmatch("AB123", r"TI\d{10,}") is False

    def test_partial_match_rejected(self):
        assert validate_regex_fullmatch("TI123extra", r"TI\d+") is False

    def test_leading_trailing_ws_rejected(self):
        assert validate_regex_fullmatch(" TI1234567890 ", r"TI\d{10,}") is False

    def test_non_string_rejected(self):
        assert validate_regex_fullmatch(None, r".*") is False
        assert validate_regex_fullmatch(12345, r"\d+") is False


# ── is_path_safe ────────────────────────────────────────────────────────────

class TestIsPathSafe:
    def test_simple_string(self):
        assert is_path_safe("PI-20260825-001") is True

    def test_dotdot_attack(self):
        assert is_path_safe("../../etc/passwd") is False
        assert is_path_safe("..") is False

    def test_forward_slash(self):
        assert is_path_safe("folder/file") is False

    def test_backslash(self):
        assert is_path_safe("folder\\file") is False

    def test_non_string(self):
        assert is_path_safe(None) is False
        assert is_path_safe(123) is False


# ── is_finite_number ────────────────────────────────────────────────────────

class TestIsFiniteNumber:
    def test_int(self):
        assert is_finite_number(0) is True
        assert is_finite_number(42) is True
        assert is_finite_number(-1) is True

    def test_float(self):
        assert is_finite_number(3.14) is True
        assert is_finite_number(0.0) is True

    def test_bool_rejected(self):
        # 严格排除 bool (True/False 是 int 的子类，但语义上不应视为数字)
        assert is_finite_number(True) is False
        assert is_finite_number(False) is False

    def test_none_rejected(self):
        assert is_finite_number(None) is False

    def test_string_rejected(self):
        assert is_finite_number("123") is False

    def test_inf_nan_rejected(self):
        assert is_finite_number(float("inf")) is False
        assert is_finite_number(float("-inf")) is False
        assert is_finite_number(float("nan")) is False


# ── is_exact_int ─────────────────────────────────────────────────────────────

class TestIsExactInt:
    def test_exact_match(self):
        assert is_exact_int(1, 1) is True
        assert is_exact_int(0, 0) is True
        assert is_exact_int(2, 2) is True

    def test_value_mismatch(self):
        assert is_exact_int(1, 2) is False

    def test_bool_rejected_even_if_numeric_equal(self):
        # 严格类型：True == 1，但不是 int 类型
        assert is_exact_int(True, 1) is False
        assert is_exact_int(False, 0) is False

    def test_float_rejected(self):
        # 1.0 == 1，但不是严格 int
        assert is_exact_int(1.0, 1) is False

    def test_none_string_rejected(self):
        assert is_exact_int(None, 0) is False
        assert is_exact_int("1", 1) is False


# ── 向后兼容 re-export 验证 ──────────────────────────────────────────────────

class TestBackwardsCompatibility:
    def test_id_sequence_reexports_validate_uuid4(self):
        from src.id_sequence import validate_uuid4 as id_validate
        # 同一函数对象或行为一致即可
        assert id_validate("a1b2c3d4-1234-4567-89ab-cdef01234567") is True

    def test_task_intent_builder_reexports_validate_uuid4(self):
        from src.task_intent_builder import validate_uuid4 as tib_validate
        assert tib_validate("a1b2c3d4-1234-4567-89ab-cdef01234567") is True
