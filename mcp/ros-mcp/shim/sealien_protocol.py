"""Shim entrypoint for mcp/core."""

from mcp.core import sealien_protocol as _impl

for _name, _value in _impl.__dict__.items():
    if _name.startswith("__"):
        continue
    globals()[_name] = _value

__all__ = [n for n in globals() if not n.startswith("__")]
