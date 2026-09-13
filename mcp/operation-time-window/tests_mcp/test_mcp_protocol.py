"""Protocol and integration tests for FastMCP server and stdio client."""

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest

from seagent_marine_current.client import discover, make_client, query_current
from seagent_marine_current.contracts import CurrentQuery


@pytest.fixture
def server_script():
    src_dir = Path(__file__).resolve().parents[1] / "src"
    return str(src_dir / "seagent_marine_current" / "server.py")


@pytest.mark.asyncio
async def test_mcp_tool_discovery(server_script):
    """Test standard MCP tools/list discovery."""
    async with make_client(server_python=sys.executable, server_script=server_script) as client:
        tool_info = await discover(client)
        assert tool_info["name"] == "get_current_forecast"
        schema = tool_info["inputSchema"]
        assert "properties" in schema
        props = schema["properties"]
        assert "latitude" in props
        assert "longitude" in props
        assert "operation_depth_m" in props
        assert "start_time" in props
        assert "end_time" in props


@pytest.mark.asyncio
async def test_mcp_unconfigured_credentials_returns_not_evaluable(server_script):
    """Ensure server fails fast with NOT_CONFIGURED when no Copernicus credentials are set."""
    env = {
        "COPERNICUSMARINE_SERVICE_USERNAME": "",
        "COPERNICUSMARINE_SERVICE_PASSWORD": "",
        "SEAGENT_CURRENT_USE_SYNTHETIC": "0",
    }
    st = datetime.now(timezone.utc) + timedelta(hours=10)
    et = st + timedelta(hours=24)
    query = CurrentQuery(
        latitude=19.6,
        longitude=113.0,
        operation_depth_m=100.0,
        start_time=st,
        end_time=et,
    )
    async with make_client(server_python=sys.executable, server_script=server_script, env=env) as client:
        reply = await query_current(client, query)
        assert reply.status == "NOT_EVALUABLE"
        assert reply.error is not None
        assert reply.error.code == "NOT_CONFIGURED"
        assert reply.error.retryable is False


@pytest.mark.asyncio
async def test_mcp_synthetic_query_returns_ok_data(server_script):
    """Test full roundtrip over FastMCP stdio using synthetic provider."""
    env = {
        "SEAGENT_CURRENT_USE_SYNTHETIC": "1",
    }
    st = datetime.now(timezone.utc) + timedelta(hours=10)
    et = st + timedelta(hours=36)
    query = CurrentQuery(
        latitude=19.6,
        longitude=113.0,
        operation_depth_m=130.0,
        start_time=st,
        end_time=et,
    )
    async with make_client(server_python=sys.executable, server_script=server_script, env=env) as client:
        reply = await query_current(client, query)
        assert reply.status == "OK"
        assert reply.data is not None
        assert reply.data.request_fingerprint == query.fingerprint
        assert len(reply.data.native_time_steps) >= 2
        assert len(reply.data.native_depth_layers_m) in (1, 2)
        assert reply.data.grid_distance_km >= 0.0


@pytest.mark.asyncio
async def test_backend_busy_concurrency_lock():
    """Verify that concurrent requests under lock receive BUSY immediately."""
    import asyncio
    from seagent_marine_current.backend import WorkerBackend
    from seagent_marine_current.provider import SyntheticCopernicusProvider
    from seagent_marine_current.worker import CurrentWorker

    # Slow worker simulating 1 second query
    class SlowWorker(CurrentWorker):
        async def run_query(self, query):
            await asyncio.sleep(0.5)
            return SyntheticCopernicusProvider().fetch(query)

    backend = WorkerBackend(worker=SlowWorker())
    st = datetime.now(timezone.utc) + timedelta(hours=1)
    q = CurrentQuery(latitude=19.6, longitude=113.0, operation_depth_m=50.0, start_time=st, end_time=st + timedelta(hours=6))

    # Launch task 1
    task1 = asyncio.create_task(backend.query(q))
    await asyncio.sleep(0.05)  # Let task 1 acquire lock

    # Task 2 attempts while task 1 is running
    res2 = await backend.query(q)
    assert res2.status == "NOT_EVALUABLE"
    assert res2.error is not None
    assert res2.error.code == "BUSY"
    assert res2.error.retryable is True

    res1 = await task1
    assert res1.status == "OK"
