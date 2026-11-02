"""Project-local MCP package.

This package keeps SEAgent MCP implementations under `mcp/` (such as `ros-mcp`
and `operation-time-window`) while preserving backward-compatible submodule
lookups (like `mcp.shim`, `mcp.core`, `mcp.mock`) and selected exports from the
upstream `mcp` package.
"""

from __future__ import annotations

from pathlib import Path
import sys


def _setup_mcp_package() -> None:
    local_root = Path(__file__).resolve().parent

    # Extend __path__ with sub-packages like ros-mcp
    ros_mcp_root = local_root / "ros-mcp"
    if ros_mcp_root.exists() and str(ros_mcp_root) not in __path__:
        __path__.append(str(ros_mcp_root))

    time_window_root = local_root / "operation-time-window"
    if time_window_root.exists() and str(time_window_root) not in __path__:
        __path__.append(str(time_window_root))

    # Find upstream package root and keep it on this package path for submodule lookups.
    upstream_root = None
    for entry in sys.path:
        if not entry:
            continue
        candidate = Path(entry).resolve() / "mcp" / "__init__.py"
        try:
            if candidate.exists() and candidate.resolve() != local_root / "__init__.py":
                upstream_root = str(candidate.parent)
                break
        except OSError:
            continue

    if upstream_root is not None and upstream_root not in __path__:
        __path__.append(upstream_root)  # type: ignore[attr-defined]

    # Import symbols from upstream modules that are actually used in the repository
    # and by `fastmcp` runtime dependencies.
    try:
        from .client.session import ClientSession
        from .client.stdio import StdioServerParameters, stdio_client
        from .server.session import ServerSession
        from .server.stdio import stdio_server
        from .shared.exceptions import McpError, UrlElicitationRequiredError
        import mcp.types as types

        # Re-export all type symbols used via `from mcp import ...` paths.
        for _name, _value in types.__dict__.items():
            if _name.startswith("_"):
                continue
            globals()[_name] = _value

        # Re-export client/server/session primitives.
        globals().update(
            {
                "ClientSession": ClientSession,
                "StdioServerParameters": StdioServerParameters,
                "stdio_client": stdio_client,
                "ServerSession": ServerSession,
                "stdio_server": stdio_server,
                "McpError": McpError,
                "UrlElicitationRequiredError": UrlElicitationRequiredError,
                "types": types,
            }
        )
    except Exception:
        pass

    globals()["__all__"] = [
        name
        for name in globals()
        if not name.startswith("_")
    ]


_setup_mcp_package()
