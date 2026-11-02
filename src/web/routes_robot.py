"""
src/web/routes_robot.py — 透明兼容别名模块
真实实现已下沉至 src/web/routes/robot.py。
"""
import sys
from .routes import robot as _impl

sys.modules[__name__] = _impl
