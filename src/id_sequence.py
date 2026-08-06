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


def validate_task_prefix(prefix: Any) -> bool:
    """验证任务类别前缀，排除空白、路径片段及非法字符。"""
    if type(prefix) is not str:
        return False
    if not prefix or prefix.strip() != prefix:
        return False
    if "/" in prefix or "\\" in prefix or ".." in prefix:
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9_]+", prefix))


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
    """生成跨进程安全、可持久化恢复的每日递增 ID (专用于 intent_id 等非连接线格式)。"""
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


def next_daily_task_id(
    prefix: str,
    date_text: str,
    width: int = 3,
    scan_specs: Iterable[tuple[Path | Callable[[], Path], str]] = (),
    allowed_prefixes: Iterable[str] | None = None,
) -> str:
    """生成确定性、跨进程安全、可持久化恢复的任务业务编号 (<PREFIX>-YYYYMMDD-NNN)。

    同一天内所有任务类别共享同一个全局递增序号 counter_key `TASK:{date_text}`。
    """
    if not validate_task_prefix(prefix):
        raise IdReservationError(f"Invalid task prefix: {prefix!r}")
    if not date_text or len(date_text) != 8 or not date_text.isdigit():
        raise IdReservationError(f"Invalid date_text for task ID: {date_text!r}")

    if allowed_prefixes is None:
        raise IdReservationError("allowed_prefixes must be provided from task schema whitelist")

    valid_prefixes = {p for p in allowed_prefixes if validate_task_prefix(p)}
    if not valid_prefixes:
        raise IdReservationError("allowed_prefixes contains no valid task prefixes")

    if prefix not in valid_prefixes:
        raise IdReservationError(f"Task prefix {prefix!r} is not in allowed_prefixes whitelist: {sorted(valid_prefixes)}")

    scan_specs_list = list(scan_specs)
    counter_key = f"TASK:{date_text}"
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
                    disk_max = _max_existing_task_sequence(date_text, width, scan_specs_list, allowed_prefixes)
                    memory_seq = _COUNTERS.get(counter_key, 0)
                    next_seq = max(persistent_seq, disk_max, memory_seq) + 1

                    updated_counters = dict(persistent_counters)
                    updated_counters[counter_key] = next_seq
                    _persist_counters(counter_file, updated_counters, counter_key)

                    _COUNTERS[counter_key] = next_seq
                    return f"{prefix}-{date_text}-{next_seq:0{width}d}"
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
            logger.error("Task ID reservation failed for %s: %s", counter_key, exc, exc_info=True)
            raise IdReservationError(f"Task ID reservation failed for {counter_key}: {exc}") from exc


def peek_daily_task_id(
    prefix: str,
    date_text: str,
    width: int = 3,
    scan_specs: Iterable[tuple[Path | Callable[[], Path], str]] = (),
    allowed_prefixes: Iterable[str] | None = None,
) -> str:
    """预览下一个任务业务编号 (<PREFIX>-YYYYMMDD-NNN)，**只读操作**。

    计算若现在正式 reserve 所会分配的下一个序号，但：
      - 不更新持久化 counter 文件；
      - 不更新内存 _COUNTERS；
      - 不创建任何任务文件；
      - 不占用序号。

    重要：preview 只是当前时刻的下一编号估算。
    由于 preview 到 reserve 之间可能有其他会话已完成正式预约，
    最终正式编号以发布时 reserve_task_id() 的返回值为准，
    对用户展示时必须说明该编号为预估值。

    counter 文件损坏时 fail closed（抛出 IdReservationError），不吞异常。
    """
    if not validate_task_prefix(prefix):
        raise IdReservationError(f"Invalid task prefix: {prefix!r}")
    if not date_text or len(date_text) != 8 or not date_text.isdigit():
        raise IdReservationError(f"Invalid date_text for task ID: {date_text!r}")

    if allowed_prefixes is None:
        raise IdReservationError("allowed_prefixes must be provided from task schema whitelist")

    valid_prefixes = {p for p in allowed_prefixes if validate_task_prefix(p)}
    if not valid_prefixes:
        raise IdReservationError("allowed_prefixes contains no valid task prefixes")

    if prefix not in valid_prefixes:
        raise IdReservationError(
            f"Task prefix {prefix!r} is not in allowed_prefixes whitelist: {sorted(valid_prefixes)}"
        )

    scan_specs_list = list(scan_specs)
    counter_key = f"TASK:{date_text}"
    counter_file = _get_counter_file_path()

    # 只读取，不获取写锁，不修改任何状态
    with _LOCK:
        # _load_persistent_counters 在损坏时会抛出 IdReservationError（fail closed）
        persistent_counters = _load_persistent_counters(counter_file)
        persistent_seq = persistent_counters.get(counter_key, 0)
        disk_max = _max_existing_task_sequence(date_text, width, scan_specs_list, allowed_prefixes)
        memory_seq = _COUNTERS.get(counter_key, 0)
        next_seq = max(persistent_seq, disk_max, memory_seq) + 1
        # 注意：不写回 _COUNTERS，不写回 counter_file
        return f"{prefix}-{date_text}-{next_seq:0{width}d}"


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
    directory_fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def validate_task_id(task_id: Any) -> bool:
    """验证 task_id 格式，排除空白、路径片段并要求前缀符合确定性格式。"""
    if type(task_id) is not str:
        return False
    if not task_id or task_id.strip() != task_id:
        return False
    if "/" in task_id or "\\" in task_id or ".." in task_id:
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9_]+-\d{8}-\d{3,}", task_id) or re.fullmatch(r"[A-Za-z0-9_]+\d{10,}", task_id))


def validate_task_id_for_task_type(task_id: Any, task_type_key: str, task_schemas: dict) -> bool:
    """验证 task_id 强符合当前 task_type_key 对应模板权威 code 前缀。"""
    if not validate_task_id(task_id):
        return False
    if not isinstance(task_schemas, dict):
        return False
    templates = task_schemas.get("task_templates", {})
    if task_type_key not in templates:
        return False
    expected_code = templates[task_type_key].get("code")
    if not expected_code or not validate_task_prefix(expected_code):
        return False
    sid = str(task_id)
    new_pattern = rf"^{re.escape(expected_code)}-\d{{8}}-\d{{3,}}$"
    legacy_pattern = rf"^{re.escape(expected_code)}\d{{10,}}$"
    return bool(re.fullmatch(new_pattern, sid) or re.fullmatch(legacy_pattern, sid))


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


def _max_existing_task_sequence(
    date_text: str,
    width: int,
    scan_specs: Iterable[tuple[Path | Callable[[], Path], str]],
    allowed_prefixes: Iterable[str] | None = None,
) -> int:
    max_seq = 0
    if allowed_prefixes:
        prefixes_list = [p for p in allowed_prefixes if validate_task_prefix(p)]
    else:
        prefixes_list = []
    if not prefixes_list:
        raise IdReservationError("allowed_prefixes must be provided from task schemas whitelist")

    escaped_prefixes = "|".join(re.escape(p) for p in sorted(set(prefixes_list)))
    pattern_new = re.compile(rf"(?:^|[^\w])({escaped_prefixes})-{re.escape(date_text)}-(\d{{{width},}})")
    pattern_old = re.compile(rf"(?:^|[^\w])({escaped_prefixes}){re.escape(date_text)}(\d+)")

    for entry, json_key in scan_specs:
        directory = entry() if callable(entry) else entry
        if not directory or not directory.exists():
            continue
        for path in directory.iterdir():
            if not path.is_file():
                continue
            max_seq = max(max_seq, _sequence_from_text(path.name, pattern_new))
            max_seq = max(max_seq, _sequence_from_text(path.name, pattern_old))
            if path.suffix == ".json":
                value = _read_json_key(path, json_key)
                if value is not None and validate_task_id(value):
                    max_seq = max(max_seq, _sequence_from_text(value, pattern_new))
                    max_seq = max(max_seq, _sequence_from_text(value, pattern_old))
    return max_seq


def _sequence_from_text(text: str, pattern: re.Pattern[str]) -> int:
    match = pattern.search(text)
    if not match:
        return 0
    if match.lastindex and match.lastindex >= 2:
        prefix = match.group(1)
        if prefix in ("TI", "task_intent_TI", "history_TI") or prefix.startswith("TI") or "task_intent_TI" in text:
            return 0
        seq_str = match.group(2)
    else:
        seq_str = match.group(1)
    try:
        return int(seq_str)
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
