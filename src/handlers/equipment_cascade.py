"""
src/handlers/equipment_cascade.py — 向后兼容别名模块，指向 src/handlers/equipment/equipment_cascade.py
"""
import sys
from .equipment import equipment_cascade as _impl
sys.modules[__name__] = _impl
