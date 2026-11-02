"""
src/dispatch/id_sequence.py — 透明兼容别名模块
真实实现已下沉至 src/dispatch/storage/id_sequence.py。
"""
import sys
from .storage import id_sequence as _impl

sys.modules[__name__] = _impl
