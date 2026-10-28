"""
src/web/routes_pages.py — 透明兼容别名模块
真实实现已下沉至 src/web/routes/pages.py。
"""
import sys
from .routes import pages as _impl

sys.modules[__name__] = _impl
