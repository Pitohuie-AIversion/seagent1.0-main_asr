"""
src/web/routes_translate.py — 透明兼容别名模块
真实实现已下沉至 src/web/routes/translate.py。
"""
import sys
from .routes import translate as _impl

sys.modules[__name__] = _impl
