"""
src/handlers/knowledge_reply_grounder.py — 向后兼容别名模块，指向 src/handlers/routing/knowledge_reply_grounder.py
"""
import sys
from .routing import knowledge_reply_grounder as _impl
sys.modules[__name__] = _impl
