"""
src/handlers/execution_control.py — 向后兼容别名模块，指向 src/handlers/lifecycle/execution_control.py
"""
import sys
from .lifecycle import execution_control as _impl
sys.modules[__name__] = _impl
