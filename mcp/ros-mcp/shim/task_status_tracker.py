"""Shim entrypoint for mcp/core.

任务状态追踪与遥测解析仅对接 `llmbridge` 主协议 `SysStatus`（`/task/system_status`）。
"""

from mcp.core import task_status_tracker as _impl

for _name, _value in _impl.__dict__.items():
    if _name.startswith("__"):
        continue
    globals()[_name] = _value

__all__ = [n for n in globals() if not n.startswith("__")]
