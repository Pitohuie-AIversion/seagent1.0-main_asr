"""
通用工具函数模块

存放跨模块复用的纯函数工具，避免重复实现。
所有函数必须是无副作用、可独立测试的纯函数。
"""

from __future__ import annotations

import re
import uuid
from typing import Any


def validate_uuid4(val: Any) -> bool:
    """
    验证值是否为符合规范的 UUIDv4 字符串（必须为规范小写格式）。

    Args:
        val: 待验证的值

    Returns:
        bool: True 表示是合法的 UUIDv4 字符串
    """
    if type(val) is not str or not val:
        return False
    try:
        parsed = uuid.UUID(val)
        return parsed.version == 4 and str(parsed) == val
    except (ValueError, TypeError, AttributeError):
        return False


def validate_regex_fullmatch(val: Any, pattern: str) -> bool:
    """
    使用正则全匹配验证字符串格式，排除非字符串和空白不一致。

    Args:
        val: 待验证的值
        pattern: 正则表达式模式

    Returns:
        bool: True 表示匹配成功
    """
    if type(val) is not str:
        return False
    if not val or val.strip() != val:
        return False
    return bool(re.fullmatch(pattern, val))


def is_path_safe(val: Any) -> bool:
    """
    检查字符串是否可安全用于文件路径片段（排除路径穿越风险）。

    Args:
        val: 待检查的字符串

    Returns:
        bool: True 表示无路径穿越风险
    """
    if type(val) is not str:
        return False
    if "/" in val or "\\" in val or ".." in val:
        return False
    return True


def is_finite_number(value: Any) -> bool:
    """
    判断值是否为有限数字（严格排除 bool）。

    Args:
        value: 待判断的值

    Returns:
        bool: True 表示是合法的有限数字
    """
    import math

    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
    )


def is_exact_int(value: Any, expected: int) -> bool:
    """
    严格判断值是否为精确整数类型且数值等于 expected。
    排除 bool、float 及 None 等非严格 int 类型。

    Args:
        value: 待判断的值
        expected: 期望的整数值

    Returns:
        bool: True 表示严格匹配
    """
    return type(value) is int and value == expected
