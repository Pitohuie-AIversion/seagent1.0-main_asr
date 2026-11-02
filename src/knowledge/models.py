"""
src/knowledge/models.py — 向后兼容别名模块，指向 src/knowledge/selection/models.py
"""
import sys
from .selection import models as _impl

sys.modules[__name__] = _impl
