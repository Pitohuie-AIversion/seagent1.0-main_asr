"""
src/handlers/equipment_collapse.py — 向后兼容别名模块，指向 src/handlers/equipment/equipment_collapse.py
"""
import sys
from .equipment import equipment_collapse as _impl
sys.modules[__name__] = _impl
