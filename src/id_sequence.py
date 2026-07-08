"""Daily incremental ID helpers for task and intent identifiers."""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Iterable


_LOCK = threading.Lock()
_COUNTERS: dict[str, int] = {}


def next_daily_id(prefix: str, date_text: str, width: int, scan_specs: Iterable[tuple[Path, str]]) -> str:
    """Return prefix + date + a monotonically increasing daily sequence."""
    counter_key = f"{prefix}{date_text}"
    with _LOCK:
        current = _COUNTERS.get(counter_key)
        if current is None:
            current = _max_existing_sequence(prefix, date_text, width, scan_specs)
        current += 1
        _COUNTERS[counter_key] = current
        return f"{prefix}{date_text}{current:0{width}d}"


def _max_existing_sequence(
    prefix: str,
    date_text: str,
    width: int,
    scan_specs: Iterable[tuple[Path, str]],
) -> int:
    max_seq = 0
    pattern = re.compile(rf"{re.escape(prefix)}{re.escape(date_text)}(\d{{{width},}})")
    for directory, json_key in scan_specs:
        if not directory.exists():
            continue
        for path in directory.glob("*.json"):
            max_seq = max(max_seq, _sequence_from_text(path.stem, pattern))
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
