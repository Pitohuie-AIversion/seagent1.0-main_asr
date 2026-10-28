"""
src/temporal/time_range_engine.py — 向后兼容别名模块，指向 src/temporal/context/time_range_engine.py
"""
import sys
from .context import time_range_engine as _impl

sys.modules[__name__] = _impl
