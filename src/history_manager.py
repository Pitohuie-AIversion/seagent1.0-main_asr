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
    ControlAuditCorruptionError,
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


VALID_PHASES = {"collecting", "blocked_hard", "blocked_soft", "confirming", "done", "rejected"}
VALID_CONTROL_STATES = {
    "idle", "stop_requested", "pause_requested", "abort_requested", "cancel_requested",
    "stopped", "paused", "aborted", "cancelled"
}


def compute_request_fingerprint(
    session_id: str,
    request_id: str,
    user_message: str,
    action: Optional[str] = None,
    task_id: Optional[str] = None,
    intent_id: Optional[str] = None,
) -> str:
    """计算控制请求的标准唯一指纹（SHA256）。只依赖不可变请求参数。

    注意：task_id 和 intent_id 为保持兼容保留在签名中，但不参与 hash 计算，
    确保服务重启、Manager 为空白状态时计算出的指纹绝对一致。
    """
    norm_msg = str(user_message or "").strip()
    raw = f"{session_id or ''}|{request_id or ''}|{norm_msg}|{action or ''}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _canonical_payload_hash(data: Dict[str, Any]) -> str:
    """计算 JSON 数据的 canonical SHA256 hash（sort_keys, ensure_ascii=False）。

    若 data 字典包含 payload_sha256 自指字段，在计算前自动过滤该字段，确保 Hash 一致性。
    """
    if isinstance(data, dict) and "payload_sha256" in data:
        data = {k: v for k, v in data.items() if k != "payload_sha256"}
    canonical = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _verify_path_ownership(target_path: Path, expected_payload_bytes: bytes) -> bool:
    """验证 target_path 当前文件内容字节是否与本事务写入的 expected_payload_bytes 完全一致。"""
    if not target_path.exists() or not target_path.is_file():
        return False
    try:
        actual_bytes = target_path.read_bytes()
        return actual_bytes == expected_payload_bytes
    except Exception:
        return False


def validate_control_event(
    data: Any,
    expected_session_id: Optional[str] = None,
    expected_request_id: Optional[str] = None,
) -> Dict[str, Any]:
    """严格校验控制审计事件的 JSON 结构与语义。

    失败时抛出 ControlAuditCorruptionError (fail closed)。
    """
    if not isinstance(data, dict):
        raise ControlAuditCorruptionError("Control event is not a JSON object")

    req_id = data.get("request_id")
    sess_id = data.get("session_id")
    fp = data.get("request_fingerprint")

    if not req_id or not isinstance(req_id, str):
        raise ControlAuditCorruptionError("Control event missing or invalid 'request_id'")

    if not sess_id or not isinstance(sess_id, str):
        raise ControlAuditCorruptionError("Control event missing or invalid 'session_id'")

    if expected_request_id and req_id != expected_request_id:
        raise ControlAuditCorruptionError(
            f"Control event request_id mismatch: expected {expected_request_id!r}, got {req_id!r}"
        )

    if expected_session_id and sess_id != expected_session_id:
        raise ControlAuditCorruptionError(
            f"Control event session_id mismatch: expected {expected_session_id!r}, got {sess_id!r}"
        )

    if not fp or not isinstance(fp, str):
        raise ControlAuditCorruptionError("Control event missing or invalid 'request_fingerprint'")

    snapshot = data.get("snapshot")
    if snapshot is not None and isinstance(snapshot, dict):
        snap_phase = snapshot.get("phase")
        if snap_phase is not None and snap_phase not in VALID_PHASES:
            raise ControlAuditCorruptionError(f"Control event snapshot has invalid phase: {snap_phase!r}")

        snap_ctrl_state = snapshot.get("control_state")
        if snap_ctrl_state is not None and snap_ctrl_state not in VALID_CONTROL_STATES:
            raise ControlAuditCorruptionError(f"Control event snapshot has invalid control_state: {snap_ctrl_state!r}")

    stored_hash = data.get("payload_sha256")
    if stored_hash:
        computed_hash = _canonical_payload_hash(data)
        if computed_hash != stored_hash:
            raise ControlAuditCorruptionError(
                f"Control event payload_sha256 mismatch: stored={stored_hash[:16]}... computed={computed_hash[:16]}..."
            )

    return data


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

    使用 os.link 实现真正的内核级别 no-clobber 原子提交：
    - 若 target_path 已存在，os.link 抛出 FileExistsError (EEXIST)，绝对不覆盖已有文件。
    - 验证已有文件：指纹+内容相同时幂等成功，否则抛出 Conflict Error。
    - Post-commit 异常清理时，首先验证 target_path 的 ownership，确认属于本事务才允许 unlink。
    """
    history_dir = target_path.parent
    history_dir.mkdir(parents=True, exist_ok=True)

    payload_bytes = json.dumps(audit_data, ensure_ascii=False, indent=2).encode("utf-8")
    temp_path = _write_temp_and_fsync(history_dir, target_path, payload_bytes)

    committed_via_link = False
    try:
        # 使用 os.link 真正的 no-clobber 原子提交
        os.link(str(temp_path), str(target_path))
        committed_via_link = True
    except FileExistsError:
        # 目标文件已被创建：删除 temp，检查已有文件是否冲突
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass

        try:
            with open(target_path, "r", encoding="utf-8") as fh:
                existing = json.load(fh)
            validate_control_event(existing, audit_data.get("session_id"), audit_data.get("request_id"))
        except ControlAuditCorruptionError:
            raise
        except Exception as e:
            raise ControlAuditCorruptionError(
                f"Control event {target_path.name} exists but cannot be read/parsed: {e}"
            ) from e

        existing_fp = existing.get("request_fingerprint")
        incoming_fp = audit_data.get("request_fingerprint")
        if existing_fp and incoming_fp and existing_fp == incoming_fp:
            existing_hash = _canonical_payload_hash(existing)
            if existing_hash == expected_payload_hash:
                logger.info("Control event %s already exists with identical payload. Idempotent.", target_path.name)
                return
        raise ControlAuditConflictError(
            f"Control audit event {target_path.name} already exists with conflicting content"
        )
    except Exception:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    finally:
        if committed_via_link:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass

    # Post-commit：目录 fsync + 完整 payload 读回校验
    try:
        _fsync_directory(history_dir)
        with open(target_path, "r", encoding="utf-8") as f:
            read_back = json.load(f)
        validate_control_event(read_back, audit_data.get("session_id"), audit_data.get("request_id"))
        actual_hash = _canonical_payload_hash(read_back)
        if actual_hash != expected_payload_hash:
            raise RuntimeError(
                f"Read-after-write hash mismatch for {target_path.name}: "
                f"expected={expected_payload_hash[:16]}… actual={actual_hash[:16]}…"
            )
    except Exception as post_err:
        logger.error("Post-commit failure for control event %s: %s", target_path.name, post_err)
        # 清理前进行 Ownership 验证：验证文件字节与本事务写入的 payload_bytes 一致
        if _verify_path_ownership(target_path, payload_bytes):
            rollback_proven = False
            try:
                target_path.unlink(missing_ok=True)
                _fsync_directory(history_dir)
                rollback_proven = True
            except Exception as rb_err:
                logger.critical("Cannot unlink control event after post-commit failure %s: %s", target_path.name, rb_err)

            if rollback_proven:
                raise ControlAuditPersistenceError(
                    f"Post-commit failed for {target_path.name} (file unlinked): {post_err}"
                ) from post_err

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
    - os.replace 成功后 Post-Replace 失败：验证 target_path ownership 确认仍为本事务写入，
      再将旧内容写回并原子替换。
    - 旧内容恢复失败或 ownership 不匹配 → ControlAuditCommitUncertainError（500/503）。
    - 绝不 unlink 目标路径（该文件是已有主历史文件，不属于本次事务新建）。
    """
    history_dir = target_path.parent
    history_dir.mkdir(parents=True, exist_ok=True)

    old_content_bytes: Optional[bytes] = None
    if target_path.exists():
        try:
            old_content_bytes = target_path.read_bytes()
        except OSError as e:
            logger.warning("Cannot read old main snapshot %s for backup: %s", target_path.name, e)

    payload_bytes = json.dumps(snapshot_data, ensure_ascii=False, indent=2).encode("utf-8")
    temp_path = _write_temp_and_fsync(history_dir, target_path, payload_bytes)

    # 原子 replace（temp → target）
    try:
        temp_path.replace(target_path)
    except Exception:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise

    # Post-Replace：目录 fsync + 完整 payload 读回校验
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
        # 恢复前验证 Ownership：确认当前 target_path 字节与本事务 payload_bytes 一致，或尝试恢复
        if _verify_path_ownership(target_path, payload_bytes) and old_content_bytes is not None:
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

        raise ControlAuditCommitUncertainError(
            f"Main snapshot commit uncertain for {target_path.name}: {post_err}"
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
    snapshot_data["payload_sha256"] = expected_hash
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
    """计算控制事件文件的精确路径。

    路径固定为 control_<session_hash16>_<request_id>.json。
    唯一键 = (session_id, request_id)。
    """
    safe_req_id = _safe_request_id(request_id)
    session_hash = hashlib.sha256(str(session_id).encode("utf-8")).hexdigest()[:16]
    return history_dir / f"control_{session_hash}_{safe_req_id}.json"


def load_control_event(
    history_dir: Path,
    session_id: str,
    request_id: str,
    intent_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    精确加载 (session_id, request_id) 的控制事件文件，不使用 glob。

    返回语义：
    - None：文件不存在（可以执行新请求）
    - dict：文件存在且合法（用于幂等重试或冲突检测）
    - ControlAuditCorruptionError：文件存在但损坏/不可读/schema 非法 → fail closed
    """
    target = get_control_event_path(history_dir, session_id, request_id, intent_id)
    if not target.exists() or not target.is_file():
        return None
    try:
        with open(target, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        raise ControlAuditCorruptionError(
            f"Control event {target.name} exists but cannot be read/parsed: {e}"
        ) from e

    # 严格模式校验
    validate_control_event(data, expected_session_id=session_id, expected_request_id=request_id)
    return data


def load_latest_session_snapshot(session_id: str) -> Optional[Dict[str, Any]]:
    """装载指定 session_id 的最新有效快照（扫描 main history 及 control audit events）。

    按 saved_at / created_at 时间倒序，返回最新的有效 snapshot 字典。
    用于服务重启或初始化时恢复 Manager 的最新持久化状态。
    """
    history_dir = get_history_dir(create=False)
    if not history_dir.exists():
        return None

    safe_sid = _safe_filename_component(session_id, "session_id")
    session_hash = hashlib.sha256(str(session_id).encode("utf-8")).hexdigest()[:16]

    candidates = []

    # 1. 搜寻主历史文件
    for pattern in (f"history_{safe_sid}.json", "history_*.json"):
        for path in history_dir.glob(pattern):
            if path.name.startswith(".tmp_"):
                continue
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if not isinstance(data, dict):
                    continue
                snap = data.get("snapshot") if isinstance(data.get("snapshot"), dict) else data
                if snap.get("session_id") == session_id:
                    saved_at = snap.get("saved_at") or snap.get("created_at") or ""
                    candidates.append((saved_at, snap))
            except Exception:
                continue

    # 2. 搜寻控制事件文件
    control_pattern = f"control_{session_hash}_*.json"
    for path in history_dir.glob(control_pattern):
        if path.name.startswith(".tmp_"):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            validate_control_event(data, expected_session_id=session_id)
            snap = data.get("snapshot")
            if isinstance(snap, dict) and snap.get("session_id") == session_id:
                created_at = data.get("created_at") or snap.get("saved_at") or ""
                candidates.append((created_at, snap))
        except Exception:
            continue

    if not candidates:
        return None

    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]



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
                    # 控制审计事件：精确路径
                    # 注意：per-request 跨进程锁已由调用方 process_with_audit() 在业务层持有，
                    # 此处不再重复加锁，避免 fcntl.flock 的自锁语义问题。
                    safe_req_id = _safe_request_id(request_id)
                    target_path = get_control_event_path(history_dir, session_id, request_id)

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
                    audit_snapshot["payload_sha256"] = expected_hash
                    _create_control_event_no_overwrite(target_path, audit_snapshot, expected_hash)
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
                    snapshot_core["payload_sha256"] = expected_hash
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
