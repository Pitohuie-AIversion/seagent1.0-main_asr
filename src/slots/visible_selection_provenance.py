"""
src/slots/visible_selection_provenance.py — 透明兼容别名模块
真实实现已下沉至 src/slots/resolution/visible_selection_provenance.py。
"""
import sys
from .resolution import visible_selection_provenance as _impl

sys.modules[__name__] = _impl
