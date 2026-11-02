"""
extraction - SEAgent 实体抽取、坐标解析与规范化域

Public classes are loaded on demand so lightweight imports do not trigger heavy LLM/submodule chains.
"""

from importlib import import_module as _import_module
from typing import TYPE_CHECKING

from .coord_parser import parse_coord_value
from .model_profile import ModelRole

if TYPE_CHECKING:
    from .extractor import ParameterExtractor
    from .normalizer import FieldNormalizer
    from .oilfield_linker import (
        OilfieldEntityLinker,
    )

_LAZY_EXPORTS = {
    "ParameterExtractor": ".extractor",
    "FieldNormalizer": ".normalizer",
    "OilfieldEntityLinker": ".oilfield_linker",
}

__all__ = [
    "parse_coord_value",
    "ModelRole",
    "ParameterExtractor",
    "FieldNormalizer",
    "OilfieldEntityLinker",
]


def __getattr__(name: str):
    if name in _LAZY_EXPORTS:
        mod = _import_module(_LAZY_EXPORTS[name], __name__)
        val = getattr(mod, name)
        globals()[name] = val
        return val
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
