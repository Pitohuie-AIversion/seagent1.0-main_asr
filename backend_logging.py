"""Backend logging setup for the Flask service."""

from __future__ import annotations

import atexit
from datetime import datetime
import logging
from pathlib import Path
import sys
import threading
from typing import TextIO


DEFAULT_LOG_DIR = Path("/root/autodl-tmp/log")
DEFAULT_APP_NAME = "seagent_backend"


class TeeStream:
    """Mirror writes to the original stream and a log file."""

    def __init__(self, stream: TextIO, log_file: TextIO):
        self._stream = stream
        self._log_file = log_file
        self._lock = threading.RLock()
        self.encoding = getattr(stream, "encoding", "utf-8")
        self.errors = getattr(stream, "errors", "replace")

    def write(self, data: str | bytes) -> int:
        if not data:
            return 0
        written = len(data)
        if isinstance(data, bytes):
            data = data.decode(self.encoding or "utf-8", errors="replace")
        with self._lock:
            self._stream.write(data)
            self._log_file.write(data)
        return written

    def flush(self) -> None:
        with self._lock:
            self._stream.flush()
            self._log_file.flush()

    def isatty(self) -> bool:
        return bool(getattr(self._stream, "isatty", lambda: False)())

    def fileno(self) -> int:
        return self._stream.fileno()


def setup_backend_logging(
    log_dir: str | Path = DEFAULT_LOG_DIR,
    app_name: str = DEFAULT_APP_NAME,
) -> Path:
    """Write backend stdout/stderr and logging output to a timestamped log file.

    The service currently prints important startup and dialogue status messages
    directly to stdout. Mirroring stdout/stderr keeps those messages in the log
    file while preserving console output for interactive runs.
    """

    target_dir = Path(log_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = target_dir / f"{app_name}_{timestamp}.log"
    log_file = log_path.open("a", encoding="utf-8", buffering=1)

    original_stdout = sys.stdout
    original_stderr = sys.stderr
    sys.stdout = TeeStream(original_stdout, log_file)
    sys.stderr = TeeStream(original_stderr, log_file)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        force=False,
    )

    def _shutdown_logging() -> None:
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        finally:
            sys.stdout = original_stdout
            sys.stderr = original_stderr
            log_file.close()

    atexit.register(_shutdown_logging)
    print(f"Backend log file: {log_path}")
    return log_path
