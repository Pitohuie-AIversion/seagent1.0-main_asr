"""MCP Client helper to discover and call get_current_forecast tool."""

from __future__ import annotations

import json
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Dict, Optional

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .contracts import CurrentQuery, ForecastError, ForecastReply


class MarineCurrentClientError(Exception):
    """Exception during MCP client communication or response parsing."""


@asynccontextmanager
async def make_client(
    server_python: Optional[str] = None,
    server_script: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
) -> AsyncIterator[ClientSession]:
    """Launch FastMCP stdio server and yield connected ClientSession."""
    py_bin = server_python or sys.executable
    if server_script is None:
        server_script = str(Path(__file__).resolve().parent / "server.py")

    # Inherit only safe/whitelisted environment variables
    whitelist = {
        "PATH",
        "PYTHONPATH",
        "COPERNICUSMARINE_SERVICE_USERNAME",
        "COPERNICUSMARINE_SERVICE_PASSWORD",
        "COPERNICUS_USERNAME",
        "COPERNICUS_PASSWORD",
        "SEAGENT_CURRENT_TIMEOUT_SECONDS",
        "SEAGENT_CURRENT_USE_SYNTHETIC",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
    }
    safe_env = {k: v for k, v in os.environ.items() if k in whitelist}
    if env:
        safe_env.update(env)

    server_params = StdioServerParameters(
        command=py_bin,
        args=[server_script],
        env=safe_env,
    )

    async with stdio_client(server_params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            yield session


async def discover(client: ClientSession) -> Dict[str, Any]:
    """Discover tools via tools/list and verify get_current_forecast schema."""
    tools_result = await client.list_tools()
    tool_map = {t.name: t for t in tools_result.tools}

    if "get_current_forecast" not in tool_map:
        raise MarineCurrentClientError(
            f"Required tool 'get_current_forecast' not found in server tools: {list(tool_map.keys())}"
        )

    target_tool = tool_map["get_current_forecast"]
    return {
        "name": target_tool.name,
        "description": target_tool.description,
        "inputSchema": target_tool.inputSchema,
    }


async def query_current(client: ClientSession, request: CurrentQuery) -> ForecastReply:
    """Call get_current_forecast and return validated ForecastReply."""
    arguments = {
        "latitude": request.latitude,
        "longitude": request.longitude,
        "operation_depth_m": request.operation_depth_m,
        "start_time": request.start_time.isoformat(),
        "end_time": request.end_time.isoformat(),
    }

    result = await client.call_tool("get_current_forecast", arguments=arguments)

    if result.isError:
        error_msg = ""
        if result.content:
            error_msg = "".join(getattr(c, "text", str(c)) for c in result.content)
        return ForecastReply(
            status="NOT_EVALUABLE",
            error=ForecastError(
                code="PROVIDER_ERROR",
                message=f"MCP tool returned error: {error_msg}",
                retryable=False,
            ),
        )

    # Extract text from content
    if not result.content:
        raise MarineCurrentClientError("Empty response content received from MCP server")

    raw_text = getattr(result.content[0], "text", None)
    if raw_text is None:
        raise MarineCurrentClientError("Missing text payload in MCP tool response content")

    try:
        parsed_json = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise MarineCurrentClientError(f"Failed to parse tool response as JSON: {raw_text}") from exc

    return ForecastReply.model_validate(parsed_json)
