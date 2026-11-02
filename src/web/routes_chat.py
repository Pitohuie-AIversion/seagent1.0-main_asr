"""
src/web/routes_chat.py — 透明兼容别名模块
真实实现已下沉至 src/web/routes/chat.py。
"""
import sys
from .routes import chat as _impl

sys.modules[__name__] = _impl
