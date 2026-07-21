"""Daily incremental ID helpers for task and intent identifiers."""

from __future__ import annotations

import fcntl
import json
import os
import re
import threading
from pathlib import Path
from typing import Any, Callable, Iterable

from .exceptions import IdReservationError
from .result_paths import get_result_dir

_LOCK = threading.Lock()
_COUNTERS: dict[str, int] = {}


def validate_intent_id(intent_id: Any) -> bool:
    """
    验证 intent_id 格式及路径安全性。
    规则：
    1. 必须是 str 类型（bool/int/list/dict 均非法）
    2. strip() 后不能为空且与原串一致
    3. 不能包含 '/', '\\', '..'
    4. 必须使用 re.fullmatch 严格匹配 ASCII 数字：r"TI[0-9]{10,}"
    """
    if type(intent_id) is not str:
        return False
    if not intent_id or intent_id.strip() != intent_id:
        return False
    if "/" in intent_id or "\\" in intent_id or ".." in intent_id:
        return False
    return bool(re.fullmatch(r"TI[0-9]{10,}", intent_id))



def _get_lock_file_path() -> Path:
    base = get_result_dir(create=True)
    return base / ".id_sequence.lock"


def _get_counter_file_path() -> Path:
    base = get_result_dir(create=True)
    return base / ".id_sequences.json"


import logging
logger = logging.getLogger("backend.id_sequence")


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
        try:
            try:
                f_lock = open(lock_file, "a+", encoding="utf-8")
            except Exception as e:
                logger.error(f"Failed to open lock file {lock_file}: {e}", exc_info=True)
                raise IdReservationError(f"Failed to open lock file {lock_file}: {e}") from e

            try:
                try:
                    fcntl.flock(f_lock.fileno(), fcntl.LOCK_EX)
                except Exception as e:
                    logger.error(f"Failed to acquire flock on {lock_file}: {e}", exc_info=True)
                    raise IdReservationError(f"Failed to acquire flock on {lock_file}: {e}") from e

                try:
                    persistent_counters: dict[str, int] = {}
                    if counter_file.exists():
                        try:
                            with open(counter_file, "r", encoding="utf-8") as f_cnt:
                                data = json.load(f_cnt)
                        except Exception as e:
                            logger.error(f"Failed to read counter file {counter_file}: {e}", exc_info=True)
                            raise IdReservationError(f"Failed to read counter file {counter_file}: {e}") from e

                        if not isinstance(data, dict):
                            logger.error(f"Counter file {counter_file} is corrupted: top-level is not a dict")
                            raise IdReservationError(f"Counter file {counter_file} is corrupted: top-level is not a dictionary")

                        for k, v in data.items():
                            if not isinstance(k, str):
                                logger.error(f"Counter key in {counter_file} is not a string: {k!r}")
                                raise IdReservationError(f"Counter key in {counter_file} is not a string: {k!r}")
                            if isinstance(v, bool):
                                logger.error(f"Counter value for key '{k}' in {counter_file} is a boolean: {v!r}")
                                raise IdReservationError(f"Counter value for key '{k}' in {counter_file} is a boolean: {v!r}")
                            if isinstance(v, int):
                                if v < 0:
                                    logger.error(f"Counter value for key '{k}' in {counter_file} is negative: {v}")
                                    raise IdReservationError(f"Counter value for key '{k}' in {counter_file} is negative: {v}")
                                persistent_counters[k] = v
                            elif isinstance(v, str) and v.isdigit():
                                persistent_counters[k] = int(v)
                            else:
                                logger.error(f"Counter value for key '{k}' in {counter_file} is invalid: {v!r}")
                                raise IdReservationError(f"Counter value for key '{k}' in {counter_file} is invalid: {v!r}")

                    persistent_seq = persistent_counters.get(counter_key, 0)
                    disk_max = _max_existing_sequence(prefix, date_text, width, scan_specs_list)
                    mem_curr = _COUNTERS.get(counter_key, 0)

                    next_seq = max(persistent_seq, disk_max, mem_curr) + 1
                    updated_persistent = dict(persistent_counters)
                    updated_persistent[counter_key] = next_seq

                    # Save updated counter dictionary to .id_sequences.json atomically
                    tmp_counter_file = counter_file.parent / f".id_sequences.tmp_{os.getpid()}_{threading.get_ident()}"
                    try:
                        with open(tmp_counter_file, "w", encoding="utf-8") as f_tmp:
                            json.dump(updated_persistent, f_tmp, ensure_ascii=False, indent=2)
                            f_tmp.flush()
                            os.fsync(f_tmp.fileno())
                        os.replace(tmp_counter_file, counter_file)
                        try:
                            dir_fd = os.open(counter_file.parent, os.O_RDONLY)
                            try:
                                os.fsync(dir_fd)
                            finally:
                                os.close(dir_fd)
                        except Exception:
                            pass
                    except Exception as e:
                        if tmp_counter_file.exists():
                            try:
                                tmp_counter_file.unlink()
                            except Exception:
                                pass
                        logger.error(f"Failed to persist ID sequence counter for {counter_key}: {e}", exc_info=True)
                        raise IdReservationError(f"Failed to persist ID sequence counter for {counter_key}: {e}") from e

                    _COUNTERS[counter_key] = next_seq
                    return f"{prefix}{date_text}{next_seq:0{width}d}"

                finally:
                    try:
                        fcntl.flock(f_lock.fileno(), fcntl.LOCK_UN)
                    except Exception:
                        pass
            finally:
                f_lock.close()
        except IdReservationError:
            raise
        except Exception as e:
            logger.error(f"ID reservation failed for {prefix}{date_text}: {e}", exc_info=True)
            raise IdReservationError(f"ID reservation failed for {prefix}{date_text}: {e}") from e


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
