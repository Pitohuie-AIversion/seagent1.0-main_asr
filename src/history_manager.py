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
from .result_paths import get_history_dir, get_task_dir

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


def _get_session_lock(history_dir: Path, session_id: str):
    """获取/创建以 session_id 为粒度的跨进程 Session 锁。"""
    key = hashlib.sha256(str(session_id).encode("utf-8")).hexdigest()[:16]
    lock_path = history_dir / f".session_{key}.lock"
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


def _verify_path_ownership(
    target_path: Path,
    expected_payload_bytes: bytes,
    expected_stat: Optional[os.stat_result] = None,
) -> bool:
    """验证 target_path 当前文件的 (st_dev, st_ino) 和字节内容是否与本事务一致。"""
    if not target_path.exists() or not target_path.is_file():
        return False
    try:
        if expected_stat is not None:
            cur_stat = os.stat(str(target_path))
            if (cur_stat.st_dev, cur_stat.st_ino) != (expected_stat.st_dev, expected_stat.st_ino):
                return False
        actual_bytes = target_path.read_bytes()
        return actual_bytes == expected_payload_bytes
    except Exception:
        return False


_SHA256_HEX_RE = re.compile(r"^[a-f0-9]{64}$")


def get_session_revision_path(history_dir: Path, session_id: str, revision: int) -> Path:
    """计算 session_id 及其 revision 对应的不可变历史文件路径。"""
    safe_sid = _safe_filename_component(session_id, "session_id")
    session_hash = hashlib.sha256(str(session_id).encode("utf-8")).hexdigest()[:16]
    return history_dir / f"session_{session_hash}_rev_{revision}.json"


def get_session_head_path(history_dir: Path, session_id: str) -> Path:
    """计算 session_id 对应的权威 Head 索引文件路径。"""
    safe_sid = _safe_filename_component(session_id, "session_id")
    session_hash = hashlib.sha256(str(session_id).encode("utf-8")).hexdigest()[:16]
    return history_dir / f".session_head_{session_hash}.json"


def read_session_head(history_dir: Path, session_id: str) -> Optional[Dict[str, Any]]:
    """读取指定 session_id 的权威 Session Head 文件。

    如果 Head 文件存在但解析/校验失败，抛出 ControlAuditCorruptionError (fail closed)。
    """
    head_path = get_session_head_path(history_dir, session_id)
    if not head_path.exists() or not head_path.is_file():
        return None

    try:
        with open(head_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        raise ControlAuditCorruptionError(
            f"Session head file {head_path.name} exists but cannot be read/parsed: {e}"
        ) from e

    if not isinstance(data, dict):
        raise ControlAuditCorruptionError(f"Session head file {head_path.name} is not a dict")

    if data.get("session_id") != session_id:
        raise ControlAuditCorruptionError(f"Session head session_id mismatch in {head_path.name}")

    cur_rev = data.get("current_revision")
    if isinstance(cur_rev, bool) or not isinstance(cur_rev, int) or cur_rev < 1:
        raise ControlAuditCorruptionError(f"Session head has invalid current_revision: {cur_rev!r}")

    snap_file = data.get("snapshot_file")
    if not snap_file or not isinstance(snap_file, str):
        raise ControlAuditCorruptionError(f"Session head missing snapshot_file: {snap_file!r}")

    sha256 = data.get("payload_sha256")
    if sha256:
        computed = _canonical_payload_hash(data)
        if computed != sha256:
            raise ControlAuditCorruptionError(f"Session head payload_sha256 mismatch in {head_path.name}")

    return data


def update_session_head(
    history_dir: Path,
    session_id: str,
    current_revision: int,
    snapshot_file: str,
    snapshot_payload_sha256: Optional[str] = None,
) -> None:
    """原子更新 session_id 的权威 Session Head 文件（带 Post-replace 安全失败语义与旧 Head 恢复）。"""
    head_path = get_session_head_path(history_dir, session_id)
    old_head_bytes: Optional[bytes] = None
    if head_path.exists() and head_path.is_file():
        try:
            old_head_bytes = head_path.read_bytes()
        except OSError:
            pass

    head_data = {
        "session_id": session_id,
        "current_revision": current_revision,
        "snapshot_file": snapshot_file,
        "snapshot_payload_sha256": snapshot_payload_sha256 or "",
        "updated_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
    }
    expected_hash = _canonical_payload_hash(head_data)
    head_data["payload_sha256"] = expected_hash

    payload_bytes = json.dumps(head_data, ensure_ascii=False, indent=2).encode("utf-8")
    temp_path = _write_temp_and_fsync(history_dir, head_path, payload_bytes)

    try:
        temp_path.replace(head_path)
    except Exception as e:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise ControlAuditPersistenceError(f"Failed to update session head for {session_id}: {e}") from e

    try:
        _fsync_directory(history_dir)
        with open(head_path, "r", encoding="utf-8") as f:
            read_back = json.load(f)
        actual_hash = _canonical_payload_hash(read_back)
        if actual_hash != expected_hash:
            raise RuntimeError(
                f"Read-after-write hash mismatch for head {head_path.name}: expected={expected_hash[:16]}... actual={actual_hash[:16]}..."
            )
    except Exception as post_err:
        logger.error("Post-replace failure for session head %s: %s", head_path.name, post_err)
        if _verify_path_ownership(head_path, payload_bytes):
            recovered = False
            try:
                if old_head_bytes is not None:
                    rec_temp = _write_temp_and_fsync(history_dir, head_path, old_head_bytes)
                    rec_temp.replace(head_path)
                    _fsync_directory(history_dir)
                else:
                    head_path.unlink(missing_ok=True)
                    _fsync_directory(history_dir)
                recovered = True
            except Exception as rec_err:
                logger.critical("Cannot restore old session head for %s: %s", session_id, rec_err)

            if recovered:
                raise ControlAuditPersistenceError(
                    f"Session head update failed post-replace (old head restored): {post_err}"
                ) from post_err

        raise ControlAuditCommitUncertainError(
            f"Session head commit uncertain for {session_id}: {post_err}"
        ) from post_err


def validate_control_event(
    data: Any,
    expected_session_id: Optional[str] = None,
    expected_request_id: Optional[str] = None,
) -> Dict[str, Any]:
    """严格校验控制审计/Session Revision 事件的 JSON 结构与语义。

    失败时抛出 ControlAuditCorruptionError (fail closed)。
    """
    if not isinstance(data, dict):
        raise ControlAuditCorruptionError("Control event is not a JSON object")

    req_id = data.get("request_id")
    sess_id = data.get("session_id")
    fp = data.get("request_fingerprint")
    ver = data.get("snapshot_version")

    if ver != SNAPSHOT_VERSION:
        raise ControlAuditCorruptionError(f"Control event invalid snapshot_version: expected {SNAPSHOT_VERSION}, got {ver!r}")

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

    if req_id and not fp:
        raise ControlAuditCorruptionError("Control event with request_id missing 'request_fingerprint'")

    stored_hash = data.get("payload_sha256")
    if not stored_hash or not isinstance(stored_hash, str) or not _SHA256_HEX_RE.match(stored_hash):
        raise ControlAuditCorruptionError("Control event missing or invalid 'payload_sha256' (must be 64 hex chars)")

    computed_hash = _canonical_payload_hash(data)
    if computed_hash != stored_hash:
        raise ControlAuditCorruptionError(
            f"Control event payload_sha256 mismatch: stored={stored_hash[:16]}... computed={computed_hash[:16]}..."
        )

    snapshot = data.get("snapshot")
    if not isinstance(snapshot, dict):
        raise ControlAuditCorruptionError("Control event missing or invalid 'snapshot' dictionary")

    s_rev = snapshot.get("session_revision")
    if isinstance(s_rev, bool) or not isinstance(s_rev, int) or s_rev < 1:
        raise ControlAuditCorruptionError(f"Snapshot missing or invalid 'session_revision': {s_rev!r}")

    p_rev = snapshot.get("parent_revision")
    if isinstance(p_rev, bool) or not isinstance(p_rev, int) or p_rev < 0:
        raise ControlAuditCorruptionError(f"Snapshot missing or invalid 'parent_revision': {p_rev!r}")

    if "conversation_history" not in snapshot or not isinstance(snapshot["conversation_history"], list):
        raise ControlAuditCorruptionError("Snapshot missing or invalid 'conversation_history' list")

    if "slot_store" not in snapshot or not isinstance(snapshot["slot_store"], dict):
        raise ControlAuditCorruptionError("Snapshot missing or invalid 'slot_store' dict")

    if "task_state" not in snapshot or not isinstance(snapshot["task_state"], dict):
        raise ControlAuditCorruptionError("Snapshot missing or invalid 'task_state' dict")

    snap_phase = snapshot.get("phase")
    if snap_phase not in VALID_PHASES:
        raise ControlAuditCorruptionError(f"Control event snapshot has invalid phase: {snap_phase!r}")

    snap_ctrl_state = snapshot.get("control_state")
    if snap_ctrl_state is not None and snap_ctrl_state not in VALID_CONTROL_STATES:
        raise ControlAuditCorruptionError(f"Control event snapshot has invalid control_state: {snap_ctrl_state!r}")

    resp_snap = data.get("response_snapshot")
    if not isinstance(resp_snap, dict):
        raise ControlAuditCorruptionError("Control event missing or invalid 'response_snapshot' dictionary")

    for req_field in ("code", "session_id", "request_id", "reply", "done", "rejected", "dialogue_mode", "control_state", "collected", "missing"):
        if req_field not in resp_snap:
            raise ControlAuditCorruptionError(f"response_snapshot missing required field: {req_field!r}")

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


def _create_control_event_no_overwrite(
    target_path: Path,
    audit_data: Dict[str, Any],
    expected_payload_hash: str,
) -> None:
    """
    控制审计事件写入原语（新建、不可覆盖）。
    """
    history_dir = target_path.parent
    history_dir.mkdir(parents=True, exist_ok=True)

    payload_bytes = json.dumps(audit_data, ensure_ascii=False, indent=2).encode("utf-8")
    temp_path = _write_temp_and_fsync(history_dir, target_path, payload_bytes)

    committed_via_link = False
    try:
        os.link(str(temp_path), str(target_path))
        committed_via_link = True
    except FileExistsError:
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
    """兼容旧主历史快照更新原语。"""
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

    try:
        temp_path.replace(target_path)
    except Exception:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise

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


def get_control_event_path(
    history_dir: Path,
    session_id: str,
    request_id: str,
    intent_id: Optional[str] = None,
) -> Path:
    """计算控制事件文件的精确路径。
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
    """沿 Head 提交链精准检索 (session_id, request_id) 的控制事件。忽略未被 Head 引用的孤儿文件。"""
    if not history_dir.exists():
        return None

    head_data = read_session_head(history_dir, session_id)
    if head_data is not None:
        curr_file: Optional[str] = head_data.get("snapshot_file")
        visited_files = set()

        while curr_file and curr_file not in visited_files:
            visited_files.add(curr_file)
            target_path = history_dir / curr_file
            if not target_path.exists() or not target_path.is_file():
                break
            try:
                with open(target_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception as e:
                raise ControlAuditCorruptionError(
                    f"Revision file {curr_file} in head chain cannot be read/parsed: {e}"
                ) from e

            validate_control_event(data, expected_session_id=session_id)
            if data.get("request_id") == request_id:
                validate_control_event(data, expected_session_id=session_id, expected_request_id=request_id)
                return data

            snap = data.get("snapshot", {})
            parent_rev = snap.get("parent_revision") if isinstance(snap, dict) else None
            if parent_rev is None or not isinstance(parent_rev, int) or parent_rev <= 0:
                break
            curr_file = get_session_revision_path(history_dir, session_id, parent_rev).name

    # 兼容处理：检查是否存在单独写入的旧版 control_*.json
    target = get_control_event_path(history_dir, session_id, request_id, intent_id)
    if target.exists() and target.is_file():
        try:
            with open(target, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            raise ControlAuditCorruptionError(
                f"Control event {target.name} exists but cannot be read/parsed: {e}"
            ) from e
        validate_control_event(data, expected_session_id=session_id, expected_request_id=request_id)
        if head_data is not None:
            return None
        return data

    return None


def load_latest_session_snapshot(session_id: str) -> Optional[Dict[str, Any]]:
    """装载指定 session_id 的权威最新快照。只跟随 Head 指针，忽略未提交文件。"""
    history_dir = get_history_dir(create=False)
    if not history_dir.exists():
        return None

    head_data = read_session_head(history_dir, session_id)
    if head_data is None:
        return None

    target_name = head_data["snapshot_file"]
    target_path = history_dir / target_name
    if not target_path.exists() or not target_path.is_file():
        raise ControlAuditCorruptionError(
            f"Session head for {session_id} points to missing snapshot file: {target_name}"
        )

    try:
        with open(target_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        if not isinstance(data, dict):
            raise ControlAuditCorruptionError(f"Snapshot file {target_name} is not a dict")

        stored_hash = data.get("payload_sha256")
        if stored_hash:
            computed_hash = _canonical_payload_hash(data)
            if computed_hash != stored_hash:
                raise ControlAuditCorruptionError(f"Snapshot file {target_name} payload_sha256 mismatch")

        snap = data.get("snapshot") if isinstance(data.get("snapshot"), dict) else data
        if snap.get("session_id") != session_id:
            raise ControlAuditCorruptionError(f"Snapshot session_id mismatch in {target_name}")

        if snap.get("phase") == "done":
            intent_id = (
                snap.get("intent_id")
                or (snap.get("built_json") or {}).get("intent_id")
                or (snap.get("task_state") or {}).get("intent_id")
            )
            if not intent_id or not isinstance(intent_id, str):
                raise ControlAuditCorruptionError(f"Done snapshot missing required intent_id in {target_name}")
            from .task_intent_builder import validate_task_intent_artifact
            task_dir = get_task_dir(create=False)
            validate_task_intent_artifact(task_dir, intent_id)

        return snap
    except Exception as exc:
        if isinstance(exc, ControlAuditCorruptionError):
            raise exc
        raise ControlAuditCorruptionError(
            f"Head snapshot file {target_name} is corrupted: {exc}"
        ) from exc


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
    parent_revision: Optional[int] = None,
    manager: Optional[Any] = None,
) -> str:
    """保存不可变 session revision 并原子更新 Session Head。
    要求显式 parent_revision: int，禁止 parent_revision=None 的隐式 CAS bypass。
    """
    if parent_revision is None or not isinstance(parent_revision, int) or isinstance(parent_revision, bool):
        raise ValueError("parent_revision is required and must be an explicit int (None CAS bypass prohibited)")

    history_dir = _ensure_dir()

    with _file_lock:
        lock_file_handle = _get_cross_process_lock(history_dir)
        try:
            fcntl.flock(lock_file_handle.fileno(), fcntl.LOCK_EX)
            try:
                task_id = built_json.get("task_id", "unknown") if isinstance(built_json, dict) else "unknown"
                task_type = task_state.get("task_type_key", "unknown") if isinstance(task_state, dict) else "unknown"

                # CAS 校验与 Parent / Session Revision 判定
                current_head = read_session_head(history_dir, session_id)
                if current_head is not None:
                    disk_cur_rev = current_head["current_revision"]
                    if disk_cur_rev != parent_revision:
                        raise ControlAuditConflictError(
                            f"Session revision CAS conflict: disk head has {disk_cur_rev}, expected parent {parent_revision}"
                        )
                else:
                    if parent_revision not in (0, 1):
                        raise ControlAuditConflictError(
                            f"Session revision CAS conflict: no disk head exists, expected parent {parent_revision} in (0, 1)"
                        )

                session_rev = parent_revision + 1
                if manager and hasattr(manager, "session_revision"):
                    manager.session_revision = session_rev

                final_result = getattr(manager, "final_result", None) if manager else None
                last_missing = getattr(manager, "_last_missing", []) if manager else []
                blocking_violations = getattr(manager, "_blocking_violations", []) if manager else []
                soft_wl = list(getattr(manager, "_soft_whitelist", [])) if manager else []
                pending_rov = getattr(manager, "_pending_rov_candidates", []) if manager else []
                hard_counts = getattr(manager, "_hard_refusal_counts", {}) if manager else {}
                awaiting_confirm = getattr(manager, "awaiting_final_confirm", False) if manager else False
                task_start = getattr(manager, "task_start_now", False) if manager else False

                snapshot_core = {
                    "snapshot_version": SNAPSHOT_VERSION,
                    "session_id": session_id,
                    "session_revision": session_rev,
                    "parent_revision": parent_revision,
                    "saved_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
                    "conversation_history": copy.deepcopy(conversation_history),
                    "slot_store": _serialize_slot_store(slot_store),
                    "task_state": copy.deepcopy(task_state),
                    "built_json": copy.deepcopy(built_json),
                    "_last_built_json": copy.deepcopy(built_json),
                    "_last_missing": copy.deepcopy(last_missing),
                    "mode": mode,
                    "phase": phase,
                    "dialogue_mode": dialogue_mode,
                    "control_state": control_state,
                    "last_control_request": copy.deepcopy(last_control_request),
                    "final_result": copy.deepcopy(final_result),
                    "_blocking_violations": copy.deepcopy(blocking_violations),
                    "_soft_whitelist": soft_wl,
                    "_pending_rov_candidates": copy.deepcopy(pending_rov),
                    "_hard_refusal_counts": copy.deepcopy(hard_counts),
                    "awaiting_final_confirm": awaiting_confirm,
                    "task_start_now": task_start,
                    "task_id": task_id,
                    "task_type": task_type,
                    "intent_id": intent_id,
                }

                eff_action = (
                    control_action
                    or (last_control_request.get("action") if isinstance(last_control_request, dict) else None)
                    or ("cancel" if phase == "rejected" else None)
                )
                request_fp = (
                    compute_request_fingerprint(
                        session_id=session_id,
                        request_id=request_id,
                        user_message=user_message or "",
                        action=eff_action or "unknown",
                        task_id=task_id if task_id != "unknown" else None,
                        intent_id=intent_id,
                    )
                    if request_id
                    else None
                )
                event_type = "draft_cancel_event" if (phase == "rejected" and request_id) else ("control_audit_event" if request_id else "session_revision_event")

                resp_snap = {
                    "code": 200,
                    "session_id": session_id,
                    "request_id": request_id or "",
                    "reply": reply or "",
                    "done": phase == "done",
                    "rejected": phase == "rejected",
                    "dialogue_mode": dialogue_mode,
                    "control_state": control_state,
                    "last_control_request": copy.deepcopy(last_control_request),
                    "collected": copy.deepcopy(built_json),
                    "missing": [miss["key"] if isinstance(miss, dict) else str(miss) for miss in last_missing],
                    "task_type": task_type,
                    "emergency": mode == "emergency",
                    "final_json": copy.deepcopy(built_json) if phase == "done" else None,
                    "is_retry": False,
                }

                revision_data = {
                    "session_id": session_id,
                    "session_revision": session_rev,
                    "parent_revision": parent_revision,
                    "request_id": request_id,
                    "event_type": event_type,
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
                    "response_snapshot": resp_snap,
                }

                expected_hash = _canonical_payload_hash(revision_data)
                revision_data["payload_sha256"] = expected_hash

                target_path = get_session_revision_path(history_dir, session_id, session_rev)
                _create_control_event_no_overwrite(target_path, revision_data, expected_hash)

                update_session_head(
                    history_dir=history_dir,
                    session_id=session_id,
                    current_revision=session_rev,
                    snapshot_file=target_path.name,
                    snapshot_payload_sha256=expected_hash,
                )
                return target_path.name
            finally:
                fcntl.flock(lock_file_handle.fileno(), fcntl.LOCK_UN)
        finally:
            lock_file_handle.close()


def maintenance_append_revision(
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
    manager: Optional[Any] = None,
) -> str:
    """维护/迁移专用的无条件追加 revision 接口。显式从 Head 提取 current_revision 作为 parent_revision。"""
    history_dir = _ensure_dir()
    with _file_lock:
        lock_file_handle = _get_cross_process_lock(history_dir)
        try:
            fcntl.flock(lock_file_handle.fileno(), fcntl.LOCK_EX)
            try:
                cur_head = read_session_head(history_dir, session_id)
                parent_rev = cur_head["current_revision"] if cur_head is not None else 0
            finally:
                fcntl.flock(lock_file_handle.fileno(), fcntl.LOCK_UN)
        finally:
            lock_file_handle.close()

    return save_conversation(
        session_id=session_id,
        conversation_history=conversation_history,
        task_state=task_state,
        built_json=built_json,
        mode=mode,
        phase=phase,
        intent_id=intent_id,
        slot_store=slot_store,
        dialogue_mode=dialogue_mode,
        control_state=control_state,
        last_control_request=last_control_request,
        request_id=request_id,
        user_message=user_message,
        reply=reply,
        control_action=control_action,
        parent_revision=parent_rev,
        manager=manager,
    )



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
