"""
src/dispatch/result_paths.py — 透明兼容别名模块
真实实现已下沉至 src/dispatch/storage/result_paths.py。
"""
import sys
from .storage import result_paths as _impl

sys.modules[__name__] = _impl
