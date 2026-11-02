"""
src/handlers/off_topic_gate.py — 向后兼容别名模块，指向 src/handlers/routing/off_topic_gate.py
"""
import sys
from .routing import off_topic_gate as _impl
sys.modules[__name__] = _impl
