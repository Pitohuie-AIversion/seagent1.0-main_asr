"""
src/temporal/duration_parser.py — 向后兼容别名模块，指向 src/temporal/parsers/duration_parser.py
"""
import sys
from .parsers import duration_parser as _impl

sys.modules[__name__] = _impl
