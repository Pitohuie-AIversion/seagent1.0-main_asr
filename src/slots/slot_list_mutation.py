"""
src/slots/slot_list_mutation.py — 透明兼容别名模块
真实实现已下沉至 src/slots/mutation/slot_list_mutation.py。
"""
import sys
from .mutation import slot_list_mutation as _impl

sys.modules[__name__] = _impl
