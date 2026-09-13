"""FastMCP Server providing read-only ocean current forecast tools over stdio."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

# Ensure package root is importable when executed directly via stdio
_src_dir = str(Path(__file__).resolve().parents[1])
if _src_dir not in sys.path:
    sys.path.insert(0, _src_dir)

from fastmcp import FastMCP
from pydantic import AwareDatetime

try:
    from .backend import WorkerBackend
    from .contracts import CurrentQuery, ForecastReply
except ImportError:
    from seagent_marine_current.backend import WorkerBackend
    from seagent_marine_current.contracts import CurrentQuery, ForecastReply

# Configure logging to stderr to prevent stdout protocol corruption
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("seagent_marine_current.server")

backend = WorkerBackend()
mcp = FastMCP("SEAgent Marine Current", mask_error_details=True)


@mcp.tool(
    name="get_current_forecast",
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def get_current_forecast(
    latitude: float,
    longitude: float,
    operation_depth_m: float,
    start_time: AwareDatetime,
    end_time: AwareDatetime,
) -> ForecastReply:
    """查询指定经纬度、作业深度和时间范围内的海洋流速原生预报（uo/vo）。

    注意：本工具只获取原生海流数据，不判断机器人是否准入，不自动修改或发布任务。
    """
    request = CurrentQuery(
        latitude=latitude,
        longitude=longitude,
        operation_depth_m=operation_depth_m,
        start_time=start_time,
        end_time=end_time,
    )
    return await backend.query(request)


if __name__ == "__main__":
    mcp.run(transport="stdio")
