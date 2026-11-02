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
from .integration import (
    MarineCurrentBridge,
    extract_current_query,
    get_deterministic_current_limit,
    apply_candidate_window_to_slots,
    confirm_candidate_window_in_slots,
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
    "MarineCurrentBridge",
    "extract_current_query",
    "get_deterministic_current_limit",
    "apply_candidate_window_to_slots",
    "confirm_candidate_window_in_slots",
]
