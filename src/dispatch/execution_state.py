"""Intent-scoped execution controls, serialized across sessions and processes."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import tempfile

from src.dispatch.atomic_task_storage import TaskPublishLock
from src.dispatch.result_paths import get_task_dir


def _identity(intent: dict) -> str:
    return str(intent.get("intent_id") or json.dumps(intent, sort_keys=True, ensure_ascii=False))


@contextmanager
def intent_execution_guard(intent: dict):
    """Hold the same lock across cancellation checks and transport writes."""
    identity = _identity(intent)
    key = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    directory = get_task_dir(create=True) / ".execution_state" / key
    directory.mkdir(parents=True, exist_ok=True)
    with TaskPublishLock(directory):
        yield directory / "control.json"


def read_execution_control(path: Path, intent: dict) -> dict | None:
    if not path.exists():
        return None
    if path.is_symlink():
        raise ValueError("Execution control record must not be a symbolic link")
    record = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(record, dict) or record.get("intent_identity") != _identity(intent):
        raise ValueError("Execution control record does not match this task")
    request = record.get("request")
    if not isinstance(request, dict) or not isinstance(record.get("dispatch_blocked"), bool):
        raise ValueError("Invalid execution control record")
    return record


def write_execution_control(path: Path, intent: dict, request: dict, *, blocked: bool) -> None:
    record = {"intent_identity": _identity(intent), "dispatch_blocked": blocked, "request": request}
    fd, temporary = tempfile.mkstemp(prefix=".control-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(record, stream, ensure_ascii=False, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        Path(temporary).unlink(missing_ok=True)
