"""
src/knowledge/unit_resolver.py — 向后兼容别名模块，指向 src/knowledge/selection/unit_resolver.py
"""
import sys
from .selection import unit_resolver as _impl
sys.modules[__name__] = _impl
