"""
src/knowledge/hierarchy_graph.py — 向后兼容别名模块，指向 src/knowledge/selection/hierarchy_graph.py
"""
import sys
from .selection import hierarchy_graph as _impl
sys.modules[__name__] = _impl
