"""Shim entrypoint for mcp/core.

对话闭环触发下发到生产桥接层，桥接层以 `UI接口协议.md` 的主协议为准。
"""

from mcp.core.dialogue_mcp_integration import attach_mcp_bridge, dispatch_dialogue_result

__all__ = ["attach_mcp_bridge", "dispatch_dialogue_result"]
