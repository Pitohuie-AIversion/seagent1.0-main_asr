"""
src/slots/normalization_contract.py — 透明兼容别名模块
真实实现已下沉至 src/slots/resolution/normalization_contract.py。
"""
import sys
from .resolution import normalization_contract as _impl

sys.modules[__name__] = _impl
