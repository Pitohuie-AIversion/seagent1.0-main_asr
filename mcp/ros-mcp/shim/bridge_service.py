"""Shim entrypoint for mcp/core.

该入口转接至生产核心实现，任务主链路采用 `sealien_ctrlpilot_llmbridge` UI 协议定义。
"""

from mcp.core.bridge_service import SEAgentMCPBridgeService

__all__ = ["SEAgentMCPBridgeService"]
