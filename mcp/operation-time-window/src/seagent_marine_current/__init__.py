"""SEAgent Marine Current FastMCP Service and Operation Window Calculation Library."""

__version__ = "0.1.0"

from .contracts import (
    CurrentQuery,
    CurrentForecastData,
    ForecastError,
    ForecastReply,
)
from .processing import CurrentProcessor, InterpolatedPoint
from .windows import (
    OperationWindowService,
    WindowCheckResult,
    WindowSearchResult,
    CandidateWindow,
    WindowStatus,
)

__all__ = [
    "CurrentQuery",
    "CurrentForecastData",
    "ForecastError",
    "ForecastReply",
    "CurrentProcessor",
    "InterpolatedPoint",
    "OperationWindowService",
    "WindowCheckResult",
    "WindowSearchResult",
    "CandidateWindow",
    "WindowStatus",
]
