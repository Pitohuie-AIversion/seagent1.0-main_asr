"""
src/web/routes_time_history.py — 透明兼容别名模块
真实实现已下沉至 src/web/routes/time_history.py。
"""
import sys
from .routes import time_history as _impl

sys.modules[__name__] = _impl
