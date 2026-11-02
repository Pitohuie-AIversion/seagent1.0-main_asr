"""
src/web/routes_asr.py — 透明兼容别名模块
真实实现已下沉至 src/web/routes/asr.py。
"""
import sys
from .routes import asr as _impl

sys.modules[__name__] = _impl
