"""
src/handlers/hypothetical_time_preview.py — 向后兼容别名模块，指向 src/handlers/routing/hypothetical_time_preview.py
"""
import sys
from .routing import hypothetical_time_preview as _impl
sys.modules[__name__] = _impl
