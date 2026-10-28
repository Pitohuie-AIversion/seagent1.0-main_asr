"""
src/handlers/slot_transaction.py — 向后兼容别名模块，指向 src/handlers/extraction/slot_transaction.py
"""
import sys
from .extraction import slot_transaction as _impl
sys.modules[__name__] = _impl
