"""Worker abstraction to isolate blocking Copernicus I/O execution."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from .contracts import CurrentForecastData, CurrentQuery
from .provider import CopernicusProvider, ProviderError, SyntheticCopernicusProvider


class CurrentWorker:
    """Worker handling isolated execution of synchronous provider I/O."""

    def __init__(self, provider: Optional[CopernicusProvider | SyntheticCopernicusProvider] = None):
        self.provider = provider or CopernicusProvider()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="marine_worker")

    async def run_query(self, query: CurrentQuery) -> CurrentForecastData:
        """Run provider.fetch in isolated worker thread."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, self.provider.fetch, query)

    def shutdown(self) -> None:
        """Gracefully release worker thread pool."""
        self._executor.shutdown(wait=False)
