"""
src/dispatch/task_intent_schema_validator.py — 透明兼容别名模块
真实实现已下沉至 src/dispatch/validation/task_intent_schema_validator.py。
"""
import sys
from .validation import task_intent_schema_validator as _impl

sys.modules[__name__] = _impl
