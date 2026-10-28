"""
src/handlers/explicit_value_grounding.py — 向后兼容别名模块，指向 src/handlers/extraction/explicit_value_grounding.py
"""
import sys
from .extraction import explicit_value_grounding as _impl
sys.modules[__name__] = _impl
