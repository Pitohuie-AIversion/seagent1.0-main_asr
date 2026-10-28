"""
src/web/routes_dev.py — 透明兼容别名模块
真实实现已下沉至 src/web/routes/dev.py。
"""
import sys
from .routes import dev as _impl

sys.modules[__name__] = _impl
