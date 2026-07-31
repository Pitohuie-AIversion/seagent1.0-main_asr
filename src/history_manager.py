"""
history_manager.py — 对话历史与控制审计快照的原子持久化与加载 (v3)
"""

import copy
import fcntl
import hashlib
import json
import logging
import os
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from .exceptions import (
    ControlAuditConflictError,
    ControlAuditPersistenceError,
    ControlAuditCommitUncertainError,
)
from .result_paths import get_history_dir

logger = logging.getLogger(__name__)
SNAPSHOT_VERSION = 3
_file_lock = threading.RLock()


def _ensure_dir() -> Path:
    """确保 history 目录存在并返回。"""
    return get_history_dir(create=True)


def _get_cross_process_lock(history_dir: Path):
    """获取/创建跨进程锁文件句柄。"""
    lock_path = history_dir / ".history.lock"
    return open(lock_path, "a+")


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


def compute_request_fingerprint(
    session_id: str,
    request_id: str,
    user_message: str,
    action: Optional[str] = None,
    task_id: Optional[str] = None,
    intent_id: Optional[str] = None,
) -> str:
    """计算控制请求的标准唯一指纹。"""
    norm_msg = str(user_message or "").strip()
    raw = f"{session_id or ''}|{request_id or ''}|{norm_msg}|{action or ''}|{task_id or ''}|{intent_id or ''}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


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


def _is_snapshot_equal(s1: Dict[str, Any], s2: Dict[str, Any], is_control_event: bool = False) -> bool:
    """比较两个快照的数据内容是否在幂等语义下相等。
    对于控制审计事件，优先校验 request_fingerprint；
    若缺少指纹，依据 (session_id, request_id, control_action, user_message, task_id, intent_id) 校验。
    """
    if not isinstance(s1, dict) or not isinstance(s2, dict):
        return s1 == s2
    if is_control_event:
        fp1 = s1.get("request_fingerprint")
        fp2 = s2.get("request_fingerprint")
        if fp1 and fp2:
            return fp1 == fp2

        sid1 = s1.get("session_id")
        sid2 = s2.get("session_id")
        req1 = s1.get("request_id") or (s1.get("last_control_request") or {}).get("request_id")
        req2 = s2.get("request_id") or (s2.get("last_control_request") or {}).get("request_id")
        act1 = s1.get("action") or (s1.get("last_control_request") or {}).get("action")
        act2 = s2.get("action") or (s2.get("last_control_request") or {}).get("action")
        msg1 = (s1.get("user_message") or "").strip()
        msg2 = (s2.get("user_message") or "").strip()
        tid1 = s1.get("task_id")
        tid2 = s2.get("task_id")
        iid1 = s1.get("intent_id")
        iid2 = s2.get("intent_id")

        return (sid1 == sid2) and (req1 == req2) and (act1 == act2) and (msg1 == msg2) and (tid1 == tid2) and (iid1 == iid2)

    c1 = copy.deepcopy(s1)
    c2 = copy.deepcopy(s2)
    c1.pop("saved_at", None)
    c2.pop("saved_at", None)
    return c1 == c2


def _atomic_durable_write(target_path: Path, snapshot_data: Dict[str, Any], is_control_event: bool = False) -> None:
    """在已持有的线程锁与跨进程锁保护下：
    - 检查已有文件（幂等判断 / 冲突拒绝）
    - 以 0600 权限、同目录临时文件、循环 os.write、fsync、原子 replace、父目录 fsync (fail closed)
    - 写后读取校验
    - 失败路径清理自有 temp 文件，Post-Replace 失败可证明 unlink 则正常回滚，不可证明则抛出 ControlAuditCommitUncertainError
    """
    history_dir = target_path.parent
    history_dir.mkdir(parents=True, exist_ok=True)

    # 1. 幂等性与冲突校验
    if target_path.exists():
        try:
            with open(target_path, "r", encoding="utf-8") as f:
                existing_data = json.load(f)
        except Exception as e:
            if is_control_event:
                raise ControlAuditConflictError(f"Control audit event {target_path.name} exists but cannot be parsed: {e}") from e
            existing_data = None

        if existing_data is not None:
            if _is_snapshot_equal(existing_data, snapshot_data, is_control_event=is_control_event):
                logger.info("Snapshot file %s already exists with identical content. Returning idempotent success.", target_path.name)
                return
            elif is_control_event:
                raise ControlAuditConflictError(f"Control audit event {target_path.name} already exists with different payload")

    # 2. 同目录临时文件
    temp_filename = f".tmp_{target_path.name}_{uuid.uuid4().hex[:8]}"
    temp_path = history_dir / temp_filename

    payload_bytes = json.dumps(snapshot_data, ensure_ascii=False, indent=2).encode("utf-8")

    fd = os.open(str(temp_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        written = 0
        total = len(payload_bytes)
        while written < total:
            n = os.write(fd, payload_bytes[written:])
            if n == 0:
                raise OSError("os.write returned 0 bytes")
            written += n

        os.fsync(fd)
    except Exception:
        os.close(fd)
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass
        raise
    else:
        os.close(fd)

    # 3. 原子 commit 替换
    try:
        temp_path.replace(target_path)
    except Exception:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass
        raise

    # 4. 父目录 fsync 与 读回校验 (Post-Replace)
    try:
        dir_fd = os.open(str(history_dir), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

        with open(target_path, "r", encoding="utf-8") as f:
            read_back = json.load(f)
        if not isinstance(read_back, dict):
            raise RuntimeError(f"Read-after-write verification failed for {target_path.name}: not a dict")
        if is_control_event:
            req_in_read = read_back.get("request_id") or (read_back.get("snapshot") or {}).get("last_control_request", {}).get("request_id")
            req_in_snap = snapshot_data.get("request_id") or (snapshot_data.get("snapshot") or {}).get("last_control_request", {}).get("request_id")
            if req_in_read != req_in_snap:
                raise RuntimeError(f"Read-after-write verification failed: request_id mismatch for {target_path.name}")
        else:
            # 对于普通历史快照，也需验证读回数据完整性（只检查顶层 snapshot_version 字段是否匹配）
            if read_back.get("snapshot_version") != snapshot_data.get("snapshot_version"):
                raise RuntimeError(f"Read-after-write verification failed: snapshot_version mismatch for {target_path.name}")
    except Exception as post_err:
        logger.error("Post-replace sync/readback failure for %s: %s", target_path.name, post_err)
        rollback_proven = False
        try:
            if target_path.exists():
                target_path.unlink()
            dir_fd = os.open(str(history_dir), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
            rollback_proven = True
        except Exception as rollback_err:
            logger.critical("Failed to rollback target file after post-replace failure: %s", rollback_err)

        if rollback_proven:
            raise ControlAuditPersistenceError(f"Post-replace fsync/readback failed for {target_path.name} (file unlinked): {post_err}") from post_err
        else:
            raise ControlAuditCommitUncertainError(f"Control audit commit uncertain for {target_path.name}: {post_err}") from post_err


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
    user_message: Optional[str] = None,
    reply: Optional[str] = None,
    control_action: Optional[str] = None,
) -> str:
    """保存 v3 对话快照。全流程由线程 RLock + 跨进程 flock 双锁保护。
    若传入 request_id，生成自描述控制审计文件 control_<id>_<req_id>.json 作为唯一权威提交。
    """
    history_dir = _ensure_dir()

    with _file_lock:
        lock_file_handle = _get_cross_process_lock(history_dir)
        try:
            fcntl.flock(lock_file_handle.fileno(), fcntl.LOCK_EX)
            try:
                safe_session_id = _safe_filename_component(session_id, "session_id")
                task_id = built_json.get("task_id", "unknown") if isinstance(built_json, dict) else "unknown"
                task_type = task_state.get("task_type_key", "unknown") if isinstance(task_state, dict) else "unknown"

                snapshot_core = {
                    "snapshot_version": SNAPSHOT_VERSION,
                    "session_id": session_id,
                    "saved_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
                    "conversation_history": copy.deepcopy(conversation_history),
                    "slot_store": _serialize_slot_store(slot_store),
                    "task_state": copy.deepcopy(task_state),
                    "built_json": copy.deepcopy(built_json),
                    "mode": mode,
                    "phase": phase,
                    "dialogue_mode": dialogue_mode,
                    "control_state": control_state,
                    "last_control_request": copy.deepcopy(last_control_request),
                    "task_id": task_id,
                    "task_type": task_type,
                    "intent_id": intent_id,
                }

                if request_id:
                    safe_req_id = _safe_filename_component(request_id, "request_id")
                    if intent_id:
                        safe_intent_id = _safe_filename_component(intent_id, "intent_id")
                        target_filename = f"control_{safe_intent_id}_{safe_req_id}.json"
                    else:
                        target_filename = f"control_{safe_session_id}_{safe_req_id}.json"

                    eff_action = control_action or (last_control_request.get("action") if isinstance(last_control_request, dict) else None) or ("cancel" if phase == "rejected" else "unknown")
                    request_fp = compute_request_fingerprint(
                        session_id=session_id,
                        request_id=request_id,
                        user_message=user_message or "",
                        action=eff_action,
                        task_id=task_id if task_id != "unknown" else None,
                        intent_id=intent_id,
                    )
                    event_type = "draft_cancel_event" if phase == "rejected" else "control_audit_event"

                    audit_snapshot = {
                        "event_type": event_type,
                        "session_id": session_id,
                        "request_id": request_id,
                        "action": eff_action,
                        "request_fingerprint": request_fp,
                        "user_message": user_message or "",
                        "reply": reply or "",
                        "task_id": task_id if task_id != "unknown" else None,
                        "intent_id": intent_id,
                        "created_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
                        "control_state": control_state,
                        "phase": phase,
                        "mode": mode,
                        "dialogue_mode": dialogue_mode,
                        "last_control_request": copy.deepcopy(last_control_request),
                        "snapshot_version": SNAPSHOT_VERSION,
                        "snapshot": snapshot_core,
                    }

                    _atomic_durable_write(history_dir / target_filename, audit_snapshot, is_control_event=True)
                    return target_filename
                else:
                    if intent_id:
                        safe_intent_id = _safe_filename_component(intent_id, "intent_id")
                        target_filename = f"history_{safe_intent_id}.json"
                    else:
                        target_filename = f"history_{safe_session_id}.json"

                    _atomic_durable_write(history_dir / target_filename, snapshot_core, is_control_event=False)
                    return target_filename
            finally:
                fcntl.flock(lock_file_handle.fileno(), fcntl.LOCK_UN)
        finally:
            lock_file_handle.close()


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
            saved_at = data.get("created_at") or data.get("saved_at") or (data.get("snapshot") or {}).get("saved_at", "")
            task_id = data.get("task_id") or (data.get("snapshot") or {}).get("task_id", "unknown")
            task_type = data.get("task_type") or (data.get("snapshot") or {}).get("task_type", "unknown")
            session_id = data.get("session_id") or (data.get("snapshot") or {}).get("session_id", "")
            records.append(
                {
                    "id": filepath.name,
                    "saved_at": saved_at,
                    "task_id": task_id,
                    "task_type": task_type,
                    "session_id": session_id,
                }
            )
        except (OSError, json.JSONDecodeError, TypeError, AttributeError) as exc:
            logger.warning("Skipping invalid history file %s: %s", filepath.name, exc)

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

    if "snapshot" in data and isinstance(data["snapshot"], dict):
        inner_snap = copy.deepcopy(data["snapshot"])
        for meta_k in ("event_type", "request_id", "action", "request_fingerprint", "reply", "user_message", "created_at"):
            if meta_k in data and meta_k not in inner_snap:
                inner_snap[meta_k] = data[meta_k]
        data = inner_snap

    ver = data.get("snapshot_version")
    if ver != SNAPSHOT_VERSION:
        data = migrate_snapshot_to_v3(data)
    return data
