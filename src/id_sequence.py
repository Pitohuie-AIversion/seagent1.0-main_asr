"""Daily incremental ID helpers for task and intent identifiers."""

from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import threading
from pathlib import Path
from typing import Any, Callable, Iterable

from .exceptions import IdReservationError
from .result_paths import get_result_dir


logger = logging.getLogger("backend.id_sequence")

_LOCK = threading.Lock()
_COUNTERS: dict[str, int] = {}


def validate_intent_id(intent_id: Any) -> bool:
    """验证 intent_id 格式，并排除空白、路径片段及非 ASCII 数字。"""
    if type(intent_id) is not str:
        return False
    if not intent_id or intent_id.strip() != intent_id:
        return False
    if "/" in intent_id or "\\" in intent_id or ".." in intent_id:
        return False
    return bool(re.fullmatch(r"TI[0-9]{10,}", intent_id))


def _get_lock_file_path() -> Path:
    return get_result_dir(create=True) / ".id_sequence.lock"


def _get_counter_file_path() -> Path:
    return get_result_dir(create=True) / ".id_sequences.json"


def next_daily_id(
    prefix: str,
    date_text: str,
    width: int,
    scan_specs: Iterable[tuple[Path | Callable[[], Path], str]],
) -> str:
    """生成跨进程安全、可持久化恢复的每日递增 ID。"""
    scan_specs_list = list(scan_specs)
    counter_key = f"{prefix}{date_text}"
    lock_file = _get_lock_file_path()
    counter_file = _get_counter_file_path()

    with _LOCK:
        try:
            try:
                lock_handle = open(lock_file, "a+", encoding="utf-8")
            except Exception as exc:
                logger.error("Failed to open lock file %s: %s", lock_file, exc, exc_info=True)
                raise IdReservationError(f"Failed to open lock file {lock_file}: {exc}") from exc

            try:
                try:
                    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
                except Exception as exc:
                    logger.error("Failed to acquire flock on %s: %s", lock_file, exc, exc_info=True)
                    raise IdReservationError(f"Failed to acquire flock on {lock_file}: {exc}") from exc

                try:
                    persistent_counters = _load_persistent_counters(counter_file)
                    persistent_seq = persistent_counters.get(counter_key, 0)
                    disk_max = _max_existing_sequence(prefix, date_text, width, scan_specs_list)
                    memory_seq = _COUNTERS.get(counter_key, 0)
                    next_seq = max(persistent_seq, disk_max, memory_seq) + 1

                    updated_counters = dict(persistent_counters)
                    updated_counters[counter_key] = next_seq
                    _persist_counters(counter_file, updated_counters, counter_key)

                    _COUNTERS[counter_key] = next_seq
                    return f"{prefix}{date_text}{next_seq:0{width}d}"
                finally:
                    try:
                        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
                    except Exception:
                        pass
            finally:
                lock_handle.close()
        except IdReservationError:
            raise
        except Exception as exc:
            logger.error("ID reservation failed for %s: %s", counter_key, exc, exc_info=True)
            raise IdReservationError(f"ID reservation failed for {counter_key}: {exc}") from exc


def _load_persistent_counters(counter_file: Path) -> dict[str, int]:
    if not counter_file.exists():
        return {}

    try:
        with open(counter_file, "r", encoding="utf-8") as counter_handle:
            data = json.load(counter_handle)
    except Exception as exc:
        logger.error("Failed to read counter file %s: %s", counter_file, exc, exc_info=True)
        raise IdReservationError(f"Failed to read counter file {counter_file}: {exc}") from exc

    if not isinstance(data, dict):
        raise IdReservationError(
            f"Counter file {counter_file} is corrupted: top-level is not a dictionary"
        )

    counters: dict[str, int] = {}
    for key, value in data.items():
        if not isinstance(key, str):
            raise IdReservationError(f"Counter key in {counter_file} is not a string: {key!r}")
        if isinstance(value, bool):
            raise IdReservationError(
                f"Counter value for key '{key}' in {counter_file} is a boolean: {value!r}"
            )
        if isinstance(value, int) and value >= 0:
            counters[key] = value
        elif isinstance(value, str) and value.isdigit():
            counters[key] = int(value)
        else:
            raise IdReservationError(
                f"Counter value for key '{key}' in {counter_file} is invalid: {value!r}"
            )
    return counters


def _persist_counters(counter_file: Path, counters: dict[str, int], counter_key: str) -> None:
    temporary_file = counter_file.parent / (
        f".id_sequences.tmp_{os.getpid()}_{threading.get_ident()}"
    )
    try:
        with open(temporary_file, "w", encoding="utf-8") as temporary_handle:
            json.dump(counters, temporary_handle, ensure_ascii=False, indent=2)
            temporary_handle.flush()
            os.fsync(temporary_handle.fileno())
        os.replace(temporary_file, counter_file)
        _sync_directory(counter_file.parent)
    except Exception as exc:
        try:
            temporary_file.unlink(missing_ok=True)
        except Exception:
            pass
        logger.error(
            "Failed to persist ID sequence counter for %s: %s",
            counter_key,
            exc,
            exc_info=True,
        )
        raise IdReservationError(
            f"Failed to persist ID sequence counter for {counter_key}: {exc}"
        ) from exc


def _sync_directory(directory: Path) -> None:
    try:
        directory_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        # 部分文件系统不支持目录 fsync；数据文件本身已经完成 fsync 和原子替换。
        pass


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
        # 临时文件和 staging 文件也可能已经占用了序号，文件名必须一并扫描。
        for path in directory.iterdir():
            if not path.is_file():
                continue
            max_seq = max(max_seq, _sequence_from_text(path.name, pattern))
            if path.suffix == ".json":
                value = _read_json_key(path, json_key)
                if value is not None:
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
        with open(path, "r", encoding="utf-8") as json_handle:
            data = json.load(json_handle)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    value = data.get(key)
    if value is None and isinstance(data.get("built_json"), dict):
        value = data["built_json"].get(key)
    return value if isinstance(value, str) else None
