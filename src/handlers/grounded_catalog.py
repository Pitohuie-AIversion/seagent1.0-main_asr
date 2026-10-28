"""
src/handlers/grounded_catalog.py — 向后兼容别名模块，指向 src/handlers/routing/grounded_catalog.py
"""
import sys
from .routing import grounded_catalog as _impl
sys.modules[__name__] = _impl
