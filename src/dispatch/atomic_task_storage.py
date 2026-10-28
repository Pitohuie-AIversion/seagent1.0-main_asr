"""
src/dispatch/atomic_task_storage.py — 透明兼容别名模块
真实实现已下沉至 src/dispatch/storage/atomic_task_storage.py。
"""
import sys
from .storage import atomic_task_storage as _impl

sys.modules[__name__] = _impl
