"""
src/slots/candidate_resolver.py — 透明兼容别名模块
真实实现已下沉至 src/slots/resolution/candidate_resolver.py。
"""
import sys
from .resolution import candidate_resolver as _impl

sys.modules[__name__] = _impl
