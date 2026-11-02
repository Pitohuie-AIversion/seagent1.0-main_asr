"""
src/handlers/payload_mutation.py — 向后兼容别名模块，指向 src/handlers/extraction/payload_mutation.py
"""
import sys
from .extraction import payload_mutation as _impl
sys.modules[__name__] = _impl
