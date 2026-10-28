"""
src/web/routes_mcp.py — 透明兼容别名模块
真实实现已下沉至 src/web/routes/mcp.py。
"""
import sys
from .routes import mcp as _impl

sys.modules[__name__] = _impl
