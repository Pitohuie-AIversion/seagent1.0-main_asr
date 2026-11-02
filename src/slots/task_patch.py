"""
src/slots/task_patch.py — 透明兼容别名模块
真实实现已下沉至 src/slots/mutation/task_patch.py。
"""
import sys
from .mutation import task_patch as _impl

sys.modules[__name__] = _impl
