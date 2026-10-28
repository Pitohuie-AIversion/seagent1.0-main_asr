"""
src/knowledge/selection_engine.py — 向后兼容别名模块，指向 src/knowledge/selection/selection_engine.py
"""
import sys
from .selection import selection_engine as _impl
sys.modules[__name__] = _impl
