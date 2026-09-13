"""Backend management handling concurrency limits, timeouts, and cancellation cleanup."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

from .contracts import CurrentForecastData, CurrentQuery, ForecastError, ForecastReply
from .provider import CopernicusProvider, ProviderError, SyntheticCopernicusProvider
from .worker import CurrentWorker

logger = logging.getLogger(__name__)


class WorkerBackend:
    """Orchestrates query execution with strict single-concurrency and timeout bounds."""

    def __init__(
        self,
        worker: Optional[CurrentWorker] = None,
        timeout_seconds: Optional[float] = None,
    ):
        if worker is None:
            env_mode = os.getenv("SEAGENT_CURRENT_ENV", os.getenv("APP_ENV", "production")).lower()
            use_synth = os.getenv("SEAGENT_CURRENT_USE_SYNTHETIC", "0") == "1"

            if use_synth:
                if env_mode == "production":
                    raise RuntimeError(
                        "Security Violation: Synthetic test fixture is strictly forbidden in production environment. "
                        "v0.1 single source of truth must be Copernicus Marine."
                    )
                logger.warning("WorkerBackend instantiated with SyntheticCopernicusProvider in %s mode.", env_mode)
                worker = CurrentWorker(SyntheticCopernicusProvider())
            else:
                worker = CurrentWorker(CopernicusProvider())

        self.worker = worker
        self.timeout_seconds = timeout_seconds or float(os.getenv("SEAGENT_CURRENT_TIMEOUT_SECONDS", "30.0"))
        self._lock = asyncio.Lock()

    async def query(self, request: CurrentQuery) -> ForecastReply:
        """Execute query under concurrency lock and timeout protection."""
        # Non-blocking lock check: if locked, immediately return BUSY
        if self._lock.locked():
            return ForecastReply(
                status="NOT_EVALUABLE",
                error=ForecastError(
                    code="BUSY",
                    message="Server is currently executing another ocean current query. Please retry shortly.",
                    retryable=True,
                ),
            )

        async with self._lock:
            try:
                # Wrap with timeout
                forecast_data: CurrentForecastData = await asyncio.wait_for(
                    self.worker.run_query(request),
                    timeout=self.timeout_seconds,
                )
                return ForecastReply(status="OK", data=forecast_data)

            except asyncio.TimeoutError:
                return ForecastReply(
                    status="NOT_EVALUABLE",
                    error=ForecastError(
                        code="TIMEOUT",
                        message=f"Query timed out after {self.timeout_seconds:.1f} seconds.",
                        retryable=True,
                    ),
                )

            except ProviderError as pe:
                return ForecastReply(
                    status="NOT_EVALUABLE",
                    error=ForecastError(
                        code=pe.code,  # type: ignore[arg-type]
                        message=pe.message,
                        retryable=pe.retryable,
                    ),
                )

            except Exception as exc:
                logger.exception("Unexpected error in WorkerBackend: %s", exc)
                return ForecastReply(
                    status="NOT_EVALUABLE",
                    error=ForecastError(
                        code="PROVIDER_ERROR",
                        message=f"Internal execution failure: {exc}",
                        retryable=False,
                    ),
                )
