"""
src/handlers/command_predicates.py — 向后兼容别名模块，指向 src/handlers/lifecycle/command_predicates.py
"""
import sys
from .lifecycle import command_predicates as _impl
sys.modules[__name__] = _impl
