"""
history_manager.py — 对话历史快照的保存与加载
"""

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Any, Optional

# 结果输出目录下的 history 文件夹
HISTORY_DIR = Path("/root/autodl-tmp/result/history")

def _ensure_dir() -> None:
    """确保 history 目录存在"""
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)

def save_conversation(
    session_id: str,
    conversation_history: List[Dict[str, str]],
    task_state: Dict[str, Any],
    built_json: Dict[str, Any],
    mode: str,
    phase: str,
    intent_id: Optional[str] = None,
    slot_store: Optional[Dict[str, Any]] = None,
) -> str:
    """
    保存一次对话快照，返回保存的文件名（不含路径）
    包含全量 slot_store 结构与 snapshot_version: 2。
    """
    _ensure_dir()

    if intent_id:
        filename = f"history_{intent_id}.json"
    else:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{session_id}_{timestamp}.json"

    filepath = HISTORY_DIR / filename

    snapshot = {
        "snapshot_version": 2,
        "session_id": session_id,
        "saved_at": datetime.now().isoformat(),
        "conversation_history": conversation_history,
        "slot_store": slot_store or {},
        "task_state": task_state,
        "built_json": built_json,
        "mode": mode,
        "phase": phase,
        "task_id": built_json.get("task_id", "unknown"),
        "task_type": task_state.get("task_type_key", "unknown"),
        "intent_id": intent_id,
    }
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)
    return filename

def list_history() -> List[Dict[str, Any]]:
    """
    返回历史记录列表，按保存时间倒序排列
    每条记录包含: id(文件名), saved_at, task_id, task_type, session_id
    """
    _ensure_dir()
    records = []
    for f in HISTORY_DIR.glob("*.json"):
        try:
            with open(f, "r", encoding="utf-8") as fp:
                data = json.load(fp)
            records.append({
                "id": f.name,
                "saved_at": data.get("saved_at", ""),
                "task_id": data.get("task_id", "unknown"),
                "task_type": data.get("task_type", "unknown"),
                "session_id": data.get("session_id", ""),
            })
        except Exception:
            continue
    # 按保存时间倒序
    records.sort(key=lambda x: x["saved_at"], reverse=True)
    return records

def load_history(history_id: str) -> Optional[Dict[str, Any]]:
    """
    根据文件名加载完整的快照数据
    """
    filepath = HISTORY_DIR / history_id
    if not filepath.exists():
        return None
    with open(filepath, "r", encoding="utf-8") as f:
        return json.load(f)