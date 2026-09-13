"""Shim entrypoint for mcp/core.

仅重导出生产核心 `rosbridge_client`，主协议契约统一为 `UI接口协议.md` 下的
`sealien_ctrlpilot_llmbridge/msg/SysTaskCmd|SysConfig|SysStatus`。
"""

from mcp.core import rosbridge_client as _impl
import sys as _sys

_sys.modules[__name__] = _impl
