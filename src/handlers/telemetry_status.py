"""
src/handlers/telemetry_status.py — 向后兼容别名模块，指向 src/handlers/routing/telemetry_status.py
"""
import sys
from .routing import telemetry_status as _impl
sys.modules[__name__] = _impl
