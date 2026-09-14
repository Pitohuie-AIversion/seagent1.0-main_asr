"""Live integration test against official Copernicus Marine API.

Strict rules from Task Spec Section 17:
- Must NOT use mock to pretend real data test.
- If credentials or internet unavailable, marks test as skipped with explicit reason.
- When credentials present, executes real remote dataset retrieval.
"""

import os
from datetime import datetime, timedelta, timezone
import pytest

from seagent_marine_current.contracts import CurrentQuery
from seagent_marine_current.provider import CopernicusProvider, ProviderError


@pytest.mark.integration
def test_live_copernicus_remote_forecast():
    """Real live test querying Copernicus Marine Toolbox if credentials provided."""
    username = os.environ.get("COPERNICUSMARINE_SERVICE_USERNAME")
    password = os.environ.get("COPERNICUSMARINE_SERVICE_PASSWORD")

    if not username or not password:
        if os.environ.get("RUN_COPERNICUS_LIVE_TEST") == "1":
            pytest.fail(
                "BLOCKED: RUN_COPERNICUS_LIVE_TEST=1 requested, but Copernicus credentials "
                "(COPERNICUSMARINE_SERVICE_USERNAME / COPERNICUSMARINE_SERVICE_PASSWORD) are not configured."
            )
        pytest.skip(
            "Live Copernicus integration test skipped: COPERNICUSMARINE_SERVICE_USERNAME "
            "or COPERNICUSMARINE_SERVICE_PASSWORD not configured in environment."
        )

    # Future 24-hour interval
    now_utc = datetime.now(timezone.utc)
    st = now_utc + timedelta(days=1)
    et = st + timedelta(hours=24)

    query = CurrentQuery(
        latitude=19.6,
        longitude=113.0,
        operation_depth_m=130.0,
        start_time=st,
        end_time=et,
    )

    provider = CopernicusProvider(username=username, password=password)
    try:
        data = provider.fetch(query)
        assert data is not None
        assert data.provider == "copernicus_marine"
        assert data.is_synthetic is False
        assert len(data.native_time_steps) >= 2
        assert len(data.native_depth_layers_m) in (1, 2)
        assert len(data.uo) == len(data.native_time_steps)

        # Algorithm penetration validation: verify real data passes CHECK and SEARCH
        from seagent_marine_current.windows import OperationWindowService
        check_res = OperationWindowService.check_window(
            forecast=data,
            operation_depth_m=130.0,
            start_time=data.native_time_steps[0],
            end_time=data.native_time_steps[-1],
            current_limit_mps=0.5,
        )
        assert check_res.status in ("AVAILABLE", "UNAVAILABLE")
        assert check_res.v_max_mps is not None

        search_res = OperationWindowService.search_windows(
            forecast=data,
            operation_depth_m=130.0,
            search_start=data.native_time_steps[0],
            search_end=data.native_time_steps[-1],
            duration_hours=4.0,
            current_limit_mps=0.5,
        )
        assert search_res.status in ("AVAILABLE", "UNAVAILABLE")
    except ProviderError as err:
        pytest.fail(f"Live Copernicus provider failed: [{err.code}] {err.message}")
    except Exception as exc:
        pytest.fail(f"Live Copernicus unexpected connection exception: {exc}")
