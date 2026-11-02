"""
src/handlers/oilfield_confirmation.py — 向后兼容别名模块，指向 src/handlers/extraction/oilfield_confirmation.py
"""
import sys
from .extraction import oilfield_confirmation as _impl
sys.modules[__name__] = _impl
