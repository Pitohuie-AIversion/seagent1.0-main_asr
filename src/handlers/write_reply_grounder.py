"""
src/handlers/write_reply_grounder.py — 向后兼容别名模块，指向 src/handlers/grounding/write_reply_grounder.py
"""
import sys
from .grounding import write_reply_grounder as _impl
sys.modules[__name__] = _impl
