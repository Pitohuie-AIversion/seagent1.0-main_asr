"""
src/knowledge/variant_evaluator.py — 向后兼容别名模块，指向 src/knowledge/selection/variant_evaluator.py
"""
import sys
from .selection import variant_evaluator as _impl
sys.modules[__name__] = _impl
