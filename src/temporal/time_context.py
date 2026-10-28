"""
src/temporal/time_context.py — 向后兼容别名模块，指向 src/temporal/context/time_context.py
"""
import sys
from .context import time_context as _impl

sys.modules[__name__] = _impl
