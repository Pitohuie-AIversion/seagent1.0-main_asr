"""
src/handlers/equipment_assertion_grounding.py — 向后兼容别名模块，指向 src/handlers/equipment/equipment_assertion_grounding.py
"""
import sys
from .equipment import equipment_assertion_grounding as _impl
sys.modules[__name__] = _impl
