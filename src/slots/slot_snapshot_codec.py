"""
src/slots/slot_snapshot_codec.py — 透明兼容别名模块
真实实现已下沉至 src/slots/codec/slot_snapshot_codec.py。
"""
import sys
from .codec import slot_snapshot_codec as _impl

sys.modules[__name__] = _impl
