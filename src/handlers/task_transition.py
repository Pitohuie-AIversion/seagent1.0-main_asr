"""
src/handlers/task_transition.py — 向后兼容别名模块，指向 src/handlers/lifecycle/task_transition.py
"""
import sys
from .lifecycle import task_transition as _impl
sys.modules[__name__] = _impl
