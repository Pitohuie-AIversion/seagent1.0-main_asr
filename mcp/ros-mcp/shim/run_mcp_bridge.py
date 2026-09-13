"""Shim entrypoint for mcp/mock.

提供 mock 启动入口；生产链路主协议请参考 `mcp/shim/bridge_service.py` 与 `mcp/core`.
"""

from mcp.mock.run_mcp_bridge import main, parse_args

__all__ = ["parse_args", "main"]
