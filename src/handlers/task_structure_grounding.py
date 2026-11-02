"""
src/handlers/task_structure_grounding.py — 向后兼容别名模块，指向 src/handlers/routing/task_structure_grounding.py
"""
import sys
from .routing import task_structure_grounding as _impl
sys.modules[__name__] = _impl
