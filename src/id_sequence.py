"""Daily incremental ID helpers for task and intent identifiers."""

from __future__ import annotations

import fcntl
import json
import os
import re
import threading
from pathlib import Path
from typing import Callable, Iterable

from .exceptions import IdReservationError
from .result_paths import get_result_dir

_LOCK = threading.Lock()
_COUNTERS: dict[str, int] = {}


def _get_lock_file_path() -> Path:
    base = get_result_dir(create=True)
    return base / ".id_sequence.lock"


def _get_counter_file_path() -> Path:
    base = get_result_dir(create=True)
    return base / ".id_sequences.json"


def next_daily_id(
    prefix: str,
    date_text: str,
    width: int,
    scan_specs: Iterable[tuple[Path | Callable[[], Path], str]],
) -> str:
    """Return prefix + date + a monotonically increasing daily sequence with cross-process flock and persistent counter file."""
    scan_specs_list = list(scan_specs)
    counter_key = f"{prefix}{date_text}"
    lock_file = _get_lock_file_path()
    counter_file = _get_counter_file_path()

    with _LOCK:
        with open(lock_file, "a+", encoding="utf-8") as f_lock:
            fcntl.flock(f_lock.fileno(), fcntl.LOCK_EX)
            try:
                persistent_counters: dict[str, int] = {}
                if counter_file.exists():
                    try:
                        with open(counter_file, "r", encoding="utf-8") as f_cnt:
                            data = json.load(f_cnt)
                            if isinstance(data, dict):
                                persistent_counters = {str(k): int(v) for k, v in data.items() if str(v).isdigit()}
                    except Exception:
                        persistent_counters = {}

                persistent_seq = persistent_counters.get(counter_key, 0)
                disk_max = _max_existing_sequence(prefix, date_text, width, scan_specs_list)
                mem_curr = _COUNTERS.get(counter_key, 0)

                next_seq = max(persistent_seq, disk_max, mem_curr) + 1
                _COUNTERS[counter_key] = next_seq
                persistent_counters[counter_key] = next_seq

                # Save updated counter dictionary to .id_sequences.json atomically
                tmp_counter_file = counter_file.parent / f".id_sequences.tmp_{os.getpid()}_{threading.get_ident()}"
                try:
                    with open(tmp_counter_file, "w", encoding="utf-8") as f_tmp:
                        json.dump(persistent_counters, f_tmp, ensure_ascii=False, indent=2)
                        f_tmp.flush()
                        os.fsync(f_tmp.fileno())
                    os.replace(tmp_counter_file, counter_file)
                except Exception as e:
                    if tmp_counter_file.exists():
                        try:
                            tmp_counter_file.unlink()
                        except Exception:
                            pass
                    raise IdReservationError(f"Failed to persist ID sequence counter for {counter_key}: {e}") from e

                return f"{prefix}{date_text}{next_seq:0{width}d}"
            finally:
                fcntl.flock(f_lock.fileno(), fcntl.LOCK_UN)


def _max_existing_sequence(
    prefix: str,
    date_text: str,
    width: int,
    scan_specs: list[tuple[Path | Callable[[], Path], str]],
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
