"""
history_manager.py — 对话历史与控制审计快照的原子持久化与加载 (v3)

持久化语义分为两类，使用独立的写入原语：

1. _create_control_event_no_overwrite():
   控制审计事件 / 草稿取消事件。
   新建文件，不可覆盖，幂等。
   Post-Replace 失败后 unlink 安全（之前该路径不存在）。

2. _replace_main_snapshot_with_recovery():
   主历史快照（history_*.json）更新。
   替换前备份旧内容到内存，失败时原子恢复旧文件。
   绝不 unlink 已替换的主历史文件。
"""

import copy
import fcntl
import hashlib
import json
import logging
import os
import re
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

# request_id 严格白名单：只允许字母数字、下划线、点、连字符，1–128 字符
_REQUEST_ID_RE = re.compile(r'^[A-Za-z0-9._-]{1,128}$')


def _ensure_dir() -> Path:
    """确保 history 目录存在并返回。"""
    return get_history_dir(create=True)


def _get_cross_process_lock(history_dir: Path):
    """获取/创建跨进程全局历史锁文件句柄。"""
    lock_path = history_dir / ".history.lock"
    return open(lock_path, "a+")


def _get_request_lock(history_dir: Path, session_id: str, request_id: str):
    """获取/创建以 (session_id, request_id) 为粒度的跨进程请求锁。"""
    key = hashlib.sha256(f"{session_id}|{request_id}".encode("utf-8")).hexdigest()[:16]
    lock_path = history_dir / f".req_{key}.lock"
    return open(lock_path, "a+")


def _safe_filename_component(value: str, field_name: str) -> str:
    """校验用于文件名的外部标识，禁止绝对路径、目录穿越和 glob 元字符。"""
    text = str(value or "").strip()
    if not text or text in {".", ".."} or Path(text).name != text:
        raise ValueError(f"Invalid {field_name} for history filename")
    # 禁止 glob 元字符（*, ?, [, ]）
    if any(c in text for c in ('*', '?', '[', ']')):
        raise ValueError(f"{field_name} contains glob metacharacters")
    return text


def _safe_request_id(request_id: str) -> str:
    """校验 request_id 严格白名单，防止 glob 注入和路径穿越。"""
    text = str(request_id or "").strip()
    if not _REQUEST_ID_RE.match(text):
        raise ValueError(f"Invalid request_id: must match [A-Za-z0-9._-]{{1,128}}, got: {text!r}")
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
    """计算控制请求的标准唯一指纹（SHA256）。"""
    norm_msg = str(user_message or "").strip()
    raw = f"{session_id or ''}|{request_id or ''}|{norm_msg}|{action or ''}|{task_id or ''}|{intent_id or ''}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _canonical_payload_hash(data: Dict[str, Any]) -> str:
    """计算 JSON 数据的 canonical SHA256 hash（sort_keys, ensure_ascii=False）。"""
    canonical = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


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


def _write_temp_and_fsync(history_dir: Path, target_path: Path, payload_bytes: bytes) -> Path:
    """在 history_dir 中写入临时文件并 fsync，返回 temp_path。写入失败时自动清理。"""
    temp_filename = f".tmp_{target_path.name}_{uuid.uuid4().hex[:8]}"
    temp_path = history_dir / temp_filename

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
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    else:
        os.close(fd)

    return temp_path


def _fsync_directory(history_dir: Path) -> None:
    """对目录执行 fsync（持久化目录项变更）。"""
    dir_fd = os.open(str(history_dir), os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


# ---------------------------------------------------------------------------
# 两套独立写入原语
# ---------------------------------------------------------------------------

def _create_control_event_no_overwrite(
    target_path: Path,
    audit_data: Dict[str, Any],
    expected_payload_hash: str,
) -> None:
    """
    控制审计事件写入原语（新建、不可覆盖）。

    前提：target_path 之前不存在（由调用方通过 request lock 保证）。

    - 写入失败前：unlink temp（temp 是唯一新建文件，安全）。
    - os.replace 成功后 Post-Replace 失败：unlink target_path 是安全的，
      因为该路径在本次事务前不存在。
    - unlink 也失败 → ControlAuditCommitUncertainError（500/503）。
    """
    history_dir = target_path.parent
    history_dir.mkdir(parents=True, exist_ok=True)

    # 1. 再次确认目标不存在（防止 TOCTOU：request lock 已提供外层保护，此处二次校验）
    if target_path.exists():
        try:
            existing = json.load(open(target_path, encoding="utf-8"))
            existing_fp = existing.get("request_fingerprint")
            incoming_fp = audit_data.get("request_fingerprint")
            if existing_fp and incoming_fp and existing_fp == incoming_fp:
                logger.info("Control event %s already exists with same fingerprint. Idempotent.", target_path.name)
                return
        except Exception:
            pass
        raise ControlAuditConflictError(
            f"Control audit event {target_path.name} already exists with conflicting content"
        )

    payload_bytes = json.dumps(audit_data, ensure_ascii=False, indent=2).encode("utf-8")
    temp_path = _write_temp_and_fsync(history_dir, target_path, payload_bytes)

    # 2. 原子 replace（temp → target）
    try:
        temp_path.replace(target_path)
    except Exception:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise

    # 3. Post-Replace：目录 fsync + 完整 payload 读回校验
    try:
        _fsync_directory(history_dir)
        with open(target_path, "r", encoding="utf-8") as f:
            read_back = json.load(f)
        actual_hash = _canonical_payload_hash(read_back)
        if actual_hash != expected_payload_hash:
            raise RuntimeError(
                f"Read-after-write hash mismatch for {target_path.name}: "
                f"expected={expected_payload_hash[:16]}… actual={actual_hash[:16]}…"
            )
    except Exception as post_err:
        logger.error("Post-replace failure for control event %s: %s", target_path.name, post_err)
        rollback_proven = False
        try:
            target_path.unlink(missing_ok=True)
            _fsync_directory(history_dir)
            rollback_proven = True
        except Exception as rb_err:
            logger.critical("Cannot unlink control event after post-replace failure %s: %s", target_path.name, rb_err)

        if rollback_proven:
            raise ControlAuditPersistenceError(
                f"Post-replace failed for {target_path.name} (file unlinked): {post_err}"
            ) from post_err
        else:
            raise ControlAuditCommitUncertainError(
                f"Control audit commit uncertain for {target_path.name}: {post_err}"
            ) from post_err


def _replace_main_snapshot_with_recovery(
    target_path: Path,
    snapshot_data: Dict[str, Any],
    expected_payload_hash: str,
) -> None:
    """
    主历史快照更新原语（替换已有文件，失败时恢复旧内容）。

    - 操作前：读取并保存旧文件内容到内存。
    - os.replace 成功后 Post-Replace 失败：将旧内容写回并原子替换。
    - 旧内容恢复失败 → ControlAuditCommitUncertainError（500/503）。
    - 绝不 unlink 目标路径（该文件是已有主历史文件，不属于本次事务新建）。
    """
    history_dir = target_path.parent
    history_dir.mkdir(parents=True, exist_ok=True)

    # 1. 备份旧内容（如果存在）
    old_content_bytes: Optional[bytes] = None
    if target_path.exists():
        try:
            old_content_bytes = target_path.read_bytes()
        except OSError as e:
            logger.warning("Cannot read old main snapshot %s for backup: %s", target_path.name, e)

    payload_bytes = json.dumps(snapshot_data, ensure_ascii=False, indent=2).encode("utf-8")
    temp_path = _write_temp_and_fsync(history_dir, target_path, payload_bytes)

    # 2. 原子 replace（temp → target）
    try:
        temp_path.replace(target_path)
    except Exception:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise

    # 3. Post-Replace：目录 fsync + 完整 payload 读回校验
    try:
        _fsync_directory(history_dir)
        with open(target_path, "r", encoding="utf-8") as f:
            read_back = json.load(f)
        actual_hash = _canonical_payload_hash(read_back)
        if actual_hash != expected_payload_hash:
            raise RuntimeError(
                f"Read-after-write hash mismatch for {target_path.name}: "
                f"expected={expected_payload_hash[:16]}… actual={actual_hash[:16]}…"
            )
    except Exception as post_err:
        logger.error("Post-replace failure for main snapshot %s: %s", target_path.name, post_err)
        # 尝试恢复旧内容（绝不 unlink！）
        if old_content_bytes is not None:
            recovery_proven = False
            try:
                recovery_temp = _write_temp_and_fsync(
                    history_dir, target_path, old_content_bytes
                )
                recovery_temp.replace(target_path)
                _fsync_directory(history_dir)
                recovery_proven = True
                logger.info("Recovered old main snapshot %s successfully.", target_path.name)
            except Exception as re_err:
                logger.critical(
                    "CRITICAL: Cannot recover main snapshot %s: %s", target_path.name, re_err
                )

            if recovery_proven:
                raise ControlAuditPersistenceError(
                    f"Post-replace failed for {target_path.name} (old content recovered): {post_err}"
                ) from post_err
            else:
                raise ControlAuditCommitUncertainError(
                    f"Main snapshot commit uncertain for {target_path.name}: {post_err}"
                ) from post_err
        else:
            # 没有旧内容备份（新建场景），也不 unlink
            raise ControlAuditPersistenceError(
                f"Post-replace failed for new main snapshot {target_path.name}: {post_err}"
            ) from post_err


# ---------------------------------------------------------------------------
# 兼容旧代码的公开接口（测试层保留调用）
# ---------------------------------------------------------------------------

def _atomic_durable_write(
    target_path: Path,
    snapshot_data: Dict[str, Any],
    is_control_event: bool = False,
) -> None:
    """向后兼容接口，路由到两套写入原语之一。"""
    expected_hash = _canonical_payload_hash(snapshot_data)
    if is_control_event:
        _create_control_event_no_overwrite(target_path, snapshot_data, expected_hash)
    else:
        _replace_main_snapshot_with_recovery(target_path, snapshot_data, expected_hash)


# ---------------------------------------------------------------------------
# 精确 request_id 路径查找（不使用 glob）
# ---------------------------------------------------------------------------

def get_control_event_path(
    history_dir: Path,
    session_id: str,
    request_id: str,
    intent_id: Optional[str] = None,
) -> Path:
    """计算控制事件文件的精确路径（(session_id, request_id) 作用域）。"""
    safe_req_id = _safe_request_id(request_id)
    safe_session_id = _safe_filename_component(session_id, "session_id")
    if intent_id:
        safe_intent_id = _safe_filename_component(intent_id, "intent_id")
        return history_dir / f"control_{safe_intent_id}_{safe_req_id}.json"
    return history_dir / f"control_{safe_session_id}_{safe_req_id}.json"


def load_control_event(
    history_dir: Path,
    session_id: str,
    request_id: str,
    intent_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    精确加载 (session_id, request_id) 的控制事件文件，不使用 glob。
    返回事件 dict 或 None（文件不存在或无法解析时）。
    """
    target = get_control_event_path(history_dir, session_id, request_id, intent_id)
    if not target.exists() or not target.is_file():
        return None
    try:
        with open(target, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning("Failed to load control event %s: %s", target.name, e)
        return None


# ---------------------------------------------------------------------------
# save_conversation（主入口）
# ---------------------------------------------------------------------------

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

    - 传入 request_id：写自描述控制审计文件（不可覆盖，以 (session_id, request_id) 为精确路径）。
    - 不传 request_id：更新主历史快照（保护旧文件，失败时恢复）。
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
                    # 控制审计事件：精确路径，跨进程 request lock
                    safe_req_id = _safe_request_id(request_id)
                    target_path = get_control_event_path(history_dir, session_id, request_id, intent_id)

                    eff_action = (
                        control_action
                        or (last_control_request.get("action") if isinstance(last_control_request, dict) else None)
                        or ("cancel" if phase == "rejected" else "unknown")
                    )
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

                    expected_hash = _canonical_payload_hash(audit_snapshot)

                    # 跨进程 request lock（保护"检查—执行—提交"原子性）
                    req_lock = _get_request_lock(history_dir, session_id, safe_req_id)
                    try:
                        fcntl.flock(req_lock.fileno(), fcntl.LOCK_EX)
                        _create_control_event_no_overwrite(target_path, audit_snapshot, expected_hash)
                    finally:
                        fcntl.flock(req_lock.fileno(), fcntl.LOCK_UN)
                        req_lock.close()

                    return target_path.name
                else:
                    # 主历史快照更新
                    if intent_id:
                        safe_intent_id = _safe_filename_component(intent_id, "intent_id")
                        target_filename = f"history_{safe_intent_id}.json"
                    else:
                        target_filename = f"history_{safe_session_id}.json"

                    target_path = history_dir / target_filename
                    expected_hash = _canonical_payload_hash(snapshot_core)
                    _replace_main_snapshot_with_recovery(target_path, snapshot_core, expected_hash)
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
