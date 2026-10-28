"""
src/slots/task_slot_filter.py — 透明兼容别名模块
真实实现已下沉至 src/slots/mutation/task_slot_filter.py。
"""
import sys
from .mutation import task_slot_filter as _impl

sys.modules[__name__] = _impl
