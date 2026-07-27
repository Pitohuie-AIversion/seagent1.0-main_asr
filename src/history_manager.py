"""
history_manager.py — 对话历史快照的保存与加载
"""

import copy
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .result_paths import get_history_dir


logger = logging.getLogger(__name__)
SNAPSHOT_VERSION = 2


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


def save_conversation(
    session_id: str,
    conversation_history: List[Dict[str, str]],
    task_state: Dict[str, Any],
    built_json: Dict[str, Any],
    mode: str,
    phase: str,
    intent_id: Optional[str] = None,
    slot_store: Any = None,
) -> str:
    """保存 v2 对话快照，并返回不含路径的文件名。"""
    history_dir = _ensure_dir()

    if intent_id:
        safe_intent_id = _safe_filename_component(intent_id, "intent_id")
        filename = f"history_{safe_intent_id}.json"
    else:
        safe_session_id = _safe_filename_component(session_id, "session_id")
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{safe_session_id}_{timestamp}.json"

    filepath = history_dir / filename
    if filepath.is_symlink():
        raise ValueError("Refusing to overwrite a symbolic-link history file")

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
        "task_id": built_json.get("task_id", "unknown"),
        "task_type": task_state.get("task_type_key", "unknown"),
        "intent_id": intent_id,
    }
    with open(filepath, "w", encoding="utf-8") as file:
        json.dump(snapshot, file, ensure_ascii=False, indent=2)
    return filename


def list_history() -> List[Dict[str, Any]]:
    """返回按保存时间倒序排列的历史记录摘要。"""
    history_dir = get_history_dir(create=False)
    if not history_dir.exists():
        return []

    records = []
    for filepath in history_dir.glob("*.json"):
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


def load_history(history_id: str) -> Optional[Dict[str, Any]]:
    """根据安全文件名加载完整快照；不存在时返回 None。"""
    filepath = _resolve_history_file(history_id)
    if not filepath.exists() or not filepath.is_file():
        return None
    with open(filepath, "r", encoding="utf-8") as file:
        data = json.load(file)
    if not isinstance(data, dict):
        raise ValueError("History snapshot must be a JSON object")
    return data
