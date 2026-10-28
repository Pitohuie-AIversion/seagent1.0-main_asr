"""
src/handlers/equipment_scoping.py — 向后兼容别名模块，指向 src/handlers/equipment/equipment_scoping.py
"""
import sys
from .equipment import equipment_scoping as _impl
sys.modules[__name__] = _impl
