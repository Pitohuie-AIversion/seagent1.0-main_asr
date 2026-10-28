"""
src/temporal/relative_time_parser.py — 向后兼容别名模块，指向 src/temporal/parsers/relative_time_parser.py
"""
import sys
from .parsers import relative_time_parser as _impl

sys.modules[__name__] = _impl
