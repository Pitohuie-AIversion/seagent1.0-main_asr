"""
src/knowledge/prompt_grounder.py — 向后兼容别名模块，指向 src/knowledge/selection/prompt_grounder.py
"""
import sys
from .selection import prompt_grounder as _impl
sys.modules[__name__] = _impl
