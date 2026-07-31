"""
history_manager.py — 对话历史与控制审计快照的原子持久化与加载 (v3)
"""

import copy
import fcntl
import json
import logging
import os
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .result_paths import get_history_dir

logger = logging.getLogger(__name__)
SNAPSHOT_VERSION = 3
_file_lock = threading.RLock()


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
    filepath = (history_dir / safe_id).resolve()
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


def _atomic_durable_write(target_path: Path, snapshot_data: Dict[str, Any]) -> None:
    """以 0600 权限、同目录临时文件、flock、fsync、原子替换及父目录 fsync 安全写入快照。"""
    history_dir = target_path.parent
    history_dir.mkdir(parents=True, exist_ok=True)

    temp_filename = f".tmp_{target_path.name}_{uuid.uuid4().hex[:8]}"
    temp_path = history_dir / temp_filename

    payload_bytes = json.dumps(snapshot_data, ensure_ascii=False, indent=2).encode("utf-8")

    fd = os.open(str(temp_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            os.write(fd, payload_bytes)
            os.fsync(fd)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)

    temp_path.replace(target_path)

    try:
        dir_fd = os.open(str(history_dir), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError as e:
        logger.debug("Parent directory fsync skipped: %s", e)

    # 写后校验：读取验证 JSON 完整性
    with open(target_path, "r", encoding="utf-8") as f:
        read_back = json.load(f)
    if not isinstance(read_back, dict) or read_back.get("snapshot_version") != SNAPSHOT_VERSION:
        raise RuntimeError(f"Read-after-write verification failed for {target_path}")


def save_conversation(
    session_id: str,
    conversation_history: List[Dict[str, str]],
    task_state: Dict[str, Any],
    built_json: Dict[str, Any],
    mode: str,
    phase: str,
    intent_id: Optional[str] = None,
    slot_store: Any = None,
    dialogue_mode: str = "task_collection",
    control_state: str = "idle",
    last_control_request: Optional[Dict[str, Any]] = None,
    request_id: Optional[str] = None,
) -> str:
    """保存 v3 对话快照。如果是控制事件，生成独立 control audit 文件。"""
    with _file_lock:
        history_dir = _ensure_dir()
        safe_session_id = _safe_filename_component(session_id, "session_id")

        snapshot = {
            "snapshot_version": SNAPSHOT_VERSION,
            "session_id": session_id,
            "saved_at": datetime.now().isoformat(),
            "conversation_history": copy.deepcopy(conversation_history),
            "slot_store": _serialize_slot_store(slot_store),
            "task_state": copy.deepcopy(task_state),
            "built_json": copy.deepcopy(built_json),
            "mode": mode,
            "phase": phase,
            "dialogue_mode": dialogue_mode,
            "control_state": control_state,
            "last_control_request": copy.deepcopy(last_control_request),
            "task_id": built_json.get("task_id", "unknown") if isinstance(built_json, dict) else "unknown",
            "task_type": task_state.get("task_type_key", "unknown") if isinstance(task_state, dict) else "unknown",
            "intent_id": intent_id,
        }

        written_filename = None

        # 控制请求审计文件 (写独立文件，不覆盖)
        if request_id:
            safe_req_id = _safe_filename_component(request_id, "request_id")
            if intent_id:
                safe_intent_id = _safe_filename_component(intent_id, "intent_id")
                audit_filename = f"control_{safe_intent_id}_{safe_req_id}.json"
            else:
                audit_filename = f"control_{safe_session_id}_{safe_req_id}.json"
            _atomic_durable_write(history_dir / audit_filename, snapshot)
            written_filename = audit_filename

        # 主历史文件更新
        if intent_id:
            safe_intent_id = _safe_filename_component(intent_id, "intent_id")
            main_filename = f"history_{safe_intent_id}.json"
        else:
            main_filename = f"history_{safe_session_id}.json"

        _atomic_durable_write(history_dir / main_filename, snapshot)

        return written_filename or main_filename


def list_history() -> List[Dict[str, Any]]:
    """返回按保存时间倒序排列的历史记录摘要。"""
    history_dir = get_history_dir(create=False)
    if not history_dir.exists():
        return []

    records = []
    for filepath in history_dir.glob("*.json"):
        if filepath.name.startswith(".tmp_"):
            continue
        try:
            with open(filepath, "r", encoding="utf-8") as file:
                data = json.load(file)
            records.append(
                {
                    "id": filepath.name,
                    "saved_at": data.get("saved_at", ""),
                    "task_id": data.get("task_id", "unknown"),
                    "task_type": data.get("task_type", "unknown"),
                    "session_id": data.get("session_id", ""),
                }
            )
        except (OSError, json.JSONDecodeError, TypeError, AttributeError) as exc:
            logger.warning("Skipping invalid history file %s: %s", filepath, exc)

    records.sort(key=lambda item: item["saved_at"], reverse=True)
    return records


def migrate_snapshot_to_v3(data: Dict[str, Any]) -> Dict[str, Any]:
    """迁移旧版快照至 snapshot_version = 3，绝不伪造缺失的控制元数据。"""
    migrated = copy.deepcopy(data)
    migrated["snapshot_version"] = SNAPSHOT_VERSION

    c_state = migrated.get("control_state")
    last_req = migrated.get("last_control_request")

    if c_state != "idle":
        if not isinstance(last_req, dict) or "request_id" not in last_req or "requested_at" not in last_req:
            # 旧版快照缺乏真实控制元数据：不伪造，降级为 idle
            migrated["control_state"] = "idle"
            migrated["last_control_request"] = None

    return migrated


def load_history(history_id: str) -> Optional[Dict[str, Any]]:
    """根据安全文件名加载完整快照；做读校验与旧版本平滑迁移。"""
    filepath = _resolve_history_file(history_id)
    if not filepath.exists() or not filepath.is_file():
        return None
    with open(filepath, "r", encoding="utf-8") as file:
        data = json.load(file)
    if not isinstance(data, dict):
        raise ValueError("History snapshot must be a JSON object")

    ver = data.get("snapshot_version")
    if ver != SNAPSHOT_VERSION:
        data = migrate_snapshot_to_v3(data)
    return data
