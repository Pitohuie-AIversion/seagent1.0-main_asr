"""
history_manager.py — 对话历史快照的保存与加载
"""

import copy
import errno
import fcntl
import json
import logging
import os
import stat
import threading
import uuid
import weakref
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.dispatch.result_paths import get_history_dir


logger = logging.getLogger(__name__)
SNAPSHOT_VERSION = 2

_DIR_LOCK_STATES_GUARD = threading.Lock()
_DIR_LOCK_STATES: "weakref.WeakValueDictionary[Path, _DirLockState]" = weakref.WeakValueDictionary()
_THREAD_LOCAL_LOCKS = threading.local()


class _DirLockState:
    def __init__(self) -> None:
        self.rlock = threading.RLock()


def _get_dir_lock_state(history_dir: Path) -> _DirLockState:
    key = history_dir.resolve()
    with _DIR_LOCK_STATES_GUARD:
        state = _DIR_LOCK_STATES.get(key)
        if state is None:
            state = _DirLockState()
            _DIR_LOCK_STATES[key] = state
        return state


def _get_held_locks_map() -> Dict[Path, List[Any]]:
    held = getattr(_THREAD_LOCAL_LOCKS, "held", None)
    if held is None:
        held = {}
        _THREAD_LOCAL_LOCKS.held = held
    return held


class _HistoryLock:
    """Thread-safe and process-safe advisory lock for a history directory."""

    def __init__(self, history_dir: Path | str, *, exclusive: bool = True) -> None:
        self.history_dir = Path(history_dir)
        self.exclusive = exclusive
        self.lock_path = self.history_dir / ".history_manager.lock"
        self._dir_key = self.history_dir.resolve()
        self._dir_state: Optional[_DirLockState] = None
        self._fd: Optional[int] = None
        self._acquired_flock = False

    def _close_fd(self) -> None:
        fd, self._fd = self._fd, None
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                logger.debug("Failed to close history lock %s", self.lock_path, exc_info=True)

    def __enter__(self) -> "_HistoryLock":
        state = _get_dir_lock_state(self._dir_key)
        state.rlock.acquire()
        try:
            held_map = _get_held_locks_map()
            entry = held_map.get(self._dir_key)
            if entry is not None:
                if self.exclusive and not entry[1]:
                    raise RuntimeError("Cannot upgrade shared history lock to exclusive lock")
                entry[0] += 1
                self._dir_state = state
                self._acquired_flock = False
                return self

            if self.lock_path.is_symlink():
                raise OSError(errno.ELOOP, "Refusing to use symbolic-link history lock file", str(self.lock_path))

            flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(self.lock_path, flags, 0o600)
            self._fd = fd
            lock_stat = os.fstat(fd)
            if not stat.S_ISREG(lock_stat.st_mode):
                raise OSError(errno.EINVAL, "History lock path must be a regular file", str(self.lock_path))
            if stat.S_IMODE(lock_stat.st_mode) != 0o600:
                os.fchmod(fd, 0o600)

            fcntl.flock(fd, fcntl.LOCK_EX if self.exclusive else fcntl.LOCK_SH)
            held_map[self._dir_key] = [1, self.exclusive]
            self._dir_state = state
            self._acquired_flock = True
            return self
        except BaseException:
            self._close_fd()
            state.rlock.release()
            raise

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        state = self._dir_state
        if state is None:
            return
        try:
            held_map = _get_held_locks_map()
            entry = held_map.get(self._dir_key)
            if entry is not None:
                entry[0] -= 1
                if entry[0] <= 0:
                    held_map.pop(self._dir_key, None)

            if self._acquired_flock and self._fd is not None:
                fd = self._fd
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except Exception:
                    logger.debug("Failed to unlock history lock %s", self.lock_path, exc_info=True)
                finally:
                    self._close_fd()
        finally:
            self._acquired_flock = False
            self._dir_state = None
            state.rlock.release()


def _sync_directory(directory: Path) -> None:
    """Flush directory entry changes to disk via fsync on an O_DIRECTORY fd."""
    directory_fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _ensure_dir() -> Path:
    """确保 history 目录存在并返回。"""
    return get_history_dir(create=True)


def _safe_filename_component(value: str, field_name: str) -> str:
    """校验用于文件名的外部标识，禁止绝对路径和目录穿越。"""
    text = str(value or "").strip()
    if not text or text in {".", ".."} or Path(text).name != text:
        raise ValueError(f"Invalid {field_name} for history filename")
    return text


def _resolve_history_file(history_id: str) -> Path:
    """把历史 ID 安全解析到 history 目录内。"""
    safe_id = _safe_filename_component(history_id, "history_id")
    if Path(safe_id).suffix.lower() != ".json":
        raise ValueError("history_id must reference a .json file")

    history_dir = get_history_dir(create=False).resolve()
    candidate = history_dir / safe_id
    if candidate.is_symlink():
        raise ValueError("Refusing to read a symbolic-link history file")
    filepath = candidate.resolve()
    if filepath.parent != history_dir:
        raise ValueError("history_id escapes history directory")
    return filepath


def _serialize_slot_store(slot_store: Any) -> Dict[str, Any]:
    """兼容新版 SlotStore、旧版 LHL SlotStore 及已导出的字典快照。"""
    if slot_store is None:
        return {}
    if isinstance(slot_store, dict):
        return copy.deepcopy(slot_store)

    exporter = getattr(slot_store, "export_snapshot", None)
    if callable(exporter):
        snapshot = exporter()
        if not isinstance(snapshot, dict):
            raise TypeError("SlotStore.export_snapshot() must return a dictionary")
        return snapshot

    slots = getattr(slot_store, "slots", None)
    unresolved = getattr(slot_store, "unresolved", None)
    version = getattr(slot_store, "version", None)
    if not isinstance(slots, dict) or not isinstance(unresolved, list):
        raise TypeError("slot_store must be a snapshot dictionary or SlotStore-like object")
    if not isinstance(version, int) or isinstance(version, bool) or version < 0:
        raise TypeError("slot_store.version must be a non-negative integer")

    serialized_slots: Dict[str, Any] = {}
    for key, slot in slots.items():
        serializer = getattr(slot, "to_dict", None)
        if not isinstance(key, str) or not callable(serializer):
            raise TypeError("slot_store contains an invalid slot entry")
        serialized_slots[key] = serializer()

    return {
        "store_version": version,
        "slots": serialized_slots,
        "unresolved": copy.deepcopy(unresolved),
    }


def save_conversation(
    session_id: str,
    conversation_history: List[Dict[str, str]],
    task_state: Dict[str, Any],
    built_json: Dict[str, Any],
    mode: str,
    phase: str,
    intent_id: Optional[str] = None,
    slot_store: Any = None,
    dialogue_mode: Optional[str] = None,
    last_mode_transition: Optional[Dict[str, Any]] = None,
    mode_transition_history: Optional[List[Dict[str, Any]]] = None,
    control_state: Optional[str] = None,
    last_control_request: Optional[Dict[str, Any]] = None,
    ros2_dispatch: Optional[Dict[str, Any]] = None,
    pending_published_intent: Optional[Dict[str, Any]] = None,
    runtime_execution_review: Optional[Dict[str, Any]] = None,
) -> str:
    """保存 v2 对话快照，并返回不含路径的文件名。"""
    if intent_id:
        safe_intent_id = _safe_filename_component(intent_id, "intent_id")
        filename = f"history_{safe_intent_id}.json"
    else:
        safe_session_id = _safe_filename_component(session_id, "session_id")
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f") + "_" + uuid.uuid4().hex[:8]
        filename = f"{safe_session_id}_{timestamp}.json"

    snapshot = {
        "snapshot_version": SNAPSHOT_VERSION,
        "session_id": session_id,
        "saved_at": datetime.now().isoformat(),
        "conversation_history": conversation_history,
        "slot_store": _serialize_slot_store(slot_store),
        "task_state": task_state,
        "built_json": built_json,
        "mode": mode,
        "phase": phase,
        "dialogue_mode": dialogue_mode or "task_collection",
        "last_mode_transition": last_mode_transition,
        "mode_transition_history": mode_transition_history or [],
        "control_state": control_state or "idle",
        "last_control_request": last_control_request,
        "ros2_dispatch": copy.deepcopy(ros2_dispatch),
        "_pending_published_intent": copy.deepcopy(pending_published_intent),
        "runtime_execution_review": copy.deepcopy(runtime_execution_review),
        "task_id": built_json.get("task_id") or task_state.get("task_id") or "unknown",
        "task_type": task_state.get("task_type_key", "unknown"),
        "intent_id": intent_id,
    }
    payload = json.dumps(snapshot, ensure_ascii=False, indent=2)

    history_dir = _ensure_dir()
    filepath = history_dir / filename

    with _HistoryLock(history_dir, exclusive=True):
        if filepath.is_symlink():
            raise ValueError("Refusing to overwrite a symbolic-link history file")

        tmp_filepath = history_dir / f".{filename}.tmp_{os.getpid()}_{threading.get_ident()}_{uuid.uuid4().hex[:8]}"
        tmp_fd: Optional[int] = None
        created_tmp = False
        replaced = False
        try:
            flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
            tmp_fd = os.open(tmp_filepath, flags, 0o600)
            created_tmp = True
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as file:
                tmp_fd = None
                file.write(payload)
                file.flush()
                os.fsync(file.fileno())
            os.replace(tmp_filepath, filepath)
            replaced = True
            _sync_directory(history_dir)
        except BaseException:
            if tmp_fd is not None:
                try:
                    os.close(tmp_fd)
                except OSError as close_err:
                    logger.debug("Failed to close temp fd for %s: %s", tmp_filepath, close_err)
            if created_tmp and not replaced:
                try:
                    tmp_filepath.unlink(missing_ok=True)
                except OSError as unlink_err:
                    logger.debug("Failed to clean up history temp file %s: %s", tmp_filepath, unlink_err)
            raise
    return filename


def list_history() -> List[Dict[str, Any]]:
    """返回按保存时间倒序排列的历史记录摘要。"""
    history_dir = get_history_dir(create=False)
    if not history_dir.is_dir():
        return []

    records = []
    open_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    with _HistoryLock(history_dir, exclusive=False):
        if not history_dir.is_dir():
            return []
        for filepath in history_dir.glob("*.json"):
            if filepath.name.startswith(".") or filepath.is_symlink():
                continue
            fd: Optional[int] = None
            try:
                fd = os.open(filepath, open_flags)
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    continue
                with os.fdopen(fd, "r", encoding="utf-8") as file:
                    fd = None
                    data = json.load(file)
                if not isinstance(data, dict):
                    continue
                saved_at = data.get("saved_at", "")
                # 必须包含 conversation_history 或有效 saved_at 结构，排除非历史快照文件（如 session_head/rev 等）
                if not isinstance(saved_at, str) or not saved_at or "conversation_history" not in data:
                    continue
                datetime.fromisoformat(saved_at)
                records.append(
                    {
                        "id": filepath.name,
                        "saved_at": saved_at,
                        "task_id": data.get("task_id", "unknown"),
                        "task_type": data.get("task_type", "unknown"),
                        "session_id": data.get("session_id", ""),
                    }
                )
            except (OSError, ValueError, TypeError, AttributeError) as exc:
                logger.warning("Skipping invalid history file %s: %s", filepath, exc)
            finally:
                if fd is not None:
                    try:
                        os.close(fd)
                    except OSError:
                        pass

    records.sort(key=lambda item: item["saved_at"], reverse=True)
    return records


def load_history(history_id: str) -> Optional[Dict[str, Any]]:
    """根据安全文件名加载完整快照；不存在时返回 None。"""
    filepath = _resolve_history_file(history_id)
    history_dir = filepath.parent
    if not history_dir.is_dir():
        return None

    open_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    with _HistoryLock(history_dir, exclusive=False):
        if not history_dir.is_dir():
            return None
        if filepath.is_symlink():
            raise ValueError("Refusing to read a symbolic-link history file")
        fd: Optional[int] = None
        try:
            try:
                fd = os.open(filepath, open_flags)
            except FileNotFoundError:
                return None
            except OSError as exc:
                if exc.errno == getattr(errno, "ELOOP", -1):
                    raise ValueError("Refusing to read a symbolic-link history file") from exc
                raise

            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return None

            with os.fdopen(fd, "r", encoding="utf-8") as file:
                fd = None
                data = json.load(file)
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass

    if not isinstance(data, dict):
        raise ValueError("History snapshot must be a JSON object")
    return data
