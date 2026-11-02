"""
src/handlers/slot_extraction_pipeline.py — 向后兼容别名模块，指向 src/handlers/extraction/slot_extraction_pipeline.py
"""
import sys
from .extraction import slot_extraction_pipeline as _impl
sys.modules[__name__] = _impl
