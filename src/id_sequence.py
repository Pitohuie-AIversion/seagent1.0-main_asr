"""Daily incremental ID helpers for task and intent identifiers."""

from __future__ import annotations

import fcntl
import json
import re
import threading
from pathlib import Path
from typing import Callable, Iterable

from .result_paths import get_result_dir

_LOCK = threading.Lock()
_COUNTERS: dict[str, int] = {}


def _get_lock_file_path() -> Path:
    base = get_result_dir(create=True)
    return base / ".id_sequence.lock"


def next_daily_id(
    prefix: str,
    date_text: str,
    width: int,
    scan_specs: Iterable[tuple[Path | Callable[[], Path], str]],
) -> str:
    """Return prefix + date + a monotonically increasing daily sequence with cross-process flock."""
    counter_key = f"{prefix}{date_text}"
    lock_file = _get_lock_file_path()

    with _LOCK:
        # Cross-process lock file
        with open(lock_file, "a+", encoding="utf-8") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                disk_max = _max_existing_sequence(prefix, date_text, width, scan_specs)
                mem_curr = _COUNTERS.get(counter_key, 0)
                next_seq = max(disk_max, mem_curr) + 1
                _COUNTERS[counter_key] = next_seq
                res_id = f"{prefix}{date_text}{next_seq:0{width}d}"

                scan_list = list(scan_specs)
                if scan_list:
                    primary_entry, _ = scan_list[0]
                    target_dir = primary_entry() if callable(primary_entry) else primary_entry
                    if target_dir and target_dir.exists():
                        res_file = target_dir / f".res_{res_id}"
                        try:
                            res_file.touch(exist_ok=True)
                        except Exception:
                            pass
                return res_id
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def _max_existing_sequence(
    prefix: str,
    date_text: str,
    width: int,
    scan_specs: Iterable[tuple[Path | Callable[[], Path], str]],
) -> int:
    max_seq = 0
    pattern = re.compile(rf"{re.escape(prefix)}{re.escape(date_text)}(\d{{{width},}})")
    for entry, json_key in scan_specs:
        directory = entry() if callable(entry) else entry
        if not directory or not directory.exists():
            continue
        # Scan all files in directory including *.json, *.staging_*, *.tmp_*
        for path in directory.iterdir():
            if not path.is_file():
                continue
            max_seq = max(max_seq, _sequence_from_text(path.name, pattern))
            if path.suffix == ".json":
                value = _read_json_key(path, json_key)
                if isinstance(value, str):
                    max_seq = max(max_seq, _sequence_from_text(value, pattern))
    return max_seq


def _sequence_from_text(text: str, pattern: re.Pattern[str]) -> int:
    match = pattern.search(text)
    if not match:
        return 0
    try:
        return int(match.group(1))
    except ValueError:
        return 0


def _read_json_key(path: Path, key: str) -> str | None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    value = data.get(key)
    if value is None and isinstance(data.get("built_json"), dict):
        value = data["built_json"].get(key)
    return value if isinstance(value, str) else None
