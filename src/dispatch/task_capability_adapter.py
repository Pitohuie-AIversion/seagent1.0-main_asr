"""
src/dispatch/task_capability_adapter.py — 透明兼容别名模块
真实实现已下沉至 src/dispatch/validation/task_capability_adapter.py。
"""
import sys
from .validation import task_capability_adapter as _impl

sys.modules[__name__] = _impl
