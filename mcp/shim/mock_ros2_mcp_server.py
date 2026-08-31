"""Shim entrypoint for mcp/mock."""

try:
    from mcp.mock.mock_ros2_mcp_server import mcp
except Exception:
    mcp = None

__all__ = ["mcp"]
