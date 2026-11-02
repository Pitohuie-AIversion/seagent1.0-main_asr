"""
src/temporal/simulated_time.py — 向后兼容别名模块，指向 src/temporal/context/simulated_time.py
"""
import sys
from .context import simulated_time as _impl

sys.modules[__name__] = _impl
