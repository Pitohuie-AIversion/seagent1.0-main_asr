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
import stat
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
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


def _read_regular_file_no_follow(
    path: Path,
    expected_stat: Optional[os.stat_result] = None,
    error_cls: type[Exception] = ControlAuditPersistenceError,
) -> Tuple[bytes, os.stat_result]:
    """使用文件描述符绑定(FD-bound)、O_CLOEXEC 与 O_NOFOLLOW (及无 NOFOLLOW 平台回退校验) 打开并读取普通文件。

    在打开前、打开后(fstat)及读取后(lstat)全面验证文件类型与 (st_dev, st_ino)，防止 TOCTOU 竞争与符号链接绕过。
    """
    path_str = str(path)
    err_cls = ControlAuditCommitUncertainError if expected_stat is not None else error_cls

    has_nofollow = hasattr(os, "O_NOFOLLOW") and bool(os.O_NOFOLLOW)
    pre_stat: Optional[os.stat_result] = None
    if not has_nofollow:
        try:
            pre_stat = os.lstat(path_str)
            if stat.S_ISLNK(pre_stat.st_mode) or not stat.S_ISREG(pre_stat.st_mode):
                raise err_cls(f"{path.name} is a symlink or non-regular file")
        except OSError as e:
            raise err_cls(f"Cannot stat {path.name} before open: {e}") from e

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC

    try:
        fd = os.open(path_str, flags)
    except OSError as e:
        raise err_cls(f"Cannot open {path.name} without following symlinks: {e}") from e

    try:
        fd_stat = os.fstat(fd)

        if stat.S_ISLNK(fd_stat.st_mode) or not stat.S_ISREG(fd_stat.st_mode):
            raise err_cls(f"{path.name} is not a regular file")

        if pre_stat is not None:
            if (fd_stat.st_dev, fd_stat.st_ino) != (pre_stat.st_dev, pre_stat.st_ino):
                raise err_cls(f"{path.name} inode swapped between pre-lstat and open")

        if expected_stat is not None:
            if (
                fd_stat.st_dev != expected_stat.st_dev
                or fd_stat.st_ino != expected_stat.st_ino
            ):
                raise ControlAuditCommitUncertainError(f"{path.name} inode changed")

        with os.fdopen(os.dup(fd), "rb") as handle:
            content = handle.read()

        path_stat = os.lstat(path_str)
        if (
            path_stat.st_dev != fd_stat.st_dev
            or path_stat.st_ino != fd_stat.st_ino
            or stat.S_ISLNK(path_stat.st_mode)
        ):
            raise err_cls(f"{path.name} changed during read")

        return content, fd_stat
    finally:
        os.close(fd)


def _verify_owned_committed_path(
    target_path: Path,
    expected_payload_bytes: bytes,
    expected_stat: os.stat_result,
) -> bool:
    """验证 target_path 当前文件的 (st_dev, st_ino) 和字节内容与本事务已提交节点完全一致。

    如果 expected_stat 为 None 或非 os.stat_result，坚决拒绝并返回 False (禁止仅凭字节匹配假定所有权)。
    使用 _read_regular_file_no_follow 进行 FD 绑定与 no-follow 校验，消除 TOCTOU 竞争。
    """
    if expected_stat is None or not isinstance(expected_stat, os.stat_result):
        return False
    try:
        content, _ = _read_regular_file_no_follow(
            target_path,
            expected_stat=expected_stat,
            error_cls=ControlAuditCommitUncertainError,
        )
        return content == expected_payload_bytes
    except Exception:
        return False


def _verify_path_ownership(
    target_path: Path,
    expected_payload_bytes: bytes,
    expected_stat: Optional[os.stat_result] = None,
) -> bool:
    """验证 target_path 当前文件的 (st_dev, st_ino) 和字节内容是否与本事务一致。

    绝不允许在 expected_stat 为 None 时认定所有权（拒绝 byte-only 所有权判定）。
    """
    if expected_stat is None or not isinstance(expected_stat, os.stat_result):
        return False
    return _verify_owned_committed_path(target_path, expected_payload_bytes, expected_stat)


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


def _validate_session_head_data(
    history_dir: Path,
    expected_session_id: str,
    data: Any,
) -> Dict[str, Any]:
    """严格校验 Session Head 的字段、格式、时间戳、路径与关联 revision 完整链。

    失败时抛出 ControlAuditCorruptionError (fail closed)。
    """
    if not isinstance(data, dict):
        raise ControlAuditCorruptionError("Session head data is not a dict")

    # 1. 强制校验所有 mandatory 字段
    for field in (
        "schema_version",
        "session_id",
        "current_revision",
        "snapshot_file",
        "snapshot_payload_sha256",
        "payload_sha256",
        "updated_at",
    ):
        if field not in data:
            raise ControlAuditCorruptionError(f"Session head missing mandatory field '{field}'")

    if data.get("schema_version") != SNAPSHOT_VERSION:
        raise ControlAuditCorruptionError(
            f"Session head invalid schema_version: expected {SNAPSHOT_VERSION}, got {data.get('schema_version')!r}"
        )

    if data.get("session_id") != expected_session_id:
        raise ControlAuditCorruptionError(
            f"Session head session_id mismatch: expected {expected_session_id!r}, got {data.get('session_id')!r}"
        )

    cur_rev = data.get("current_revision")
    if isinstance(cur_rev, bool) or not isinstance(cur_rev, int) or cur_rev < 1:
        raise ControlAuditCorruptionError(f"Session head has invalid current_revision: {cur_rev!r}")

    snap_file = data.get("snapshot_file")
    if not snap_file or not isinstance(snap_file, str):
        raise ControlAuditCorruptionError(f"Session head missing or invalid snapshot_file: {snap_file!r}")

    # 2. 路径安全与派生文件名精确匹配校验 (防止 path traversal 和不一致文件名)
    if "/" in snap_file or "\\" in snap_file or ".." in snap_file or Path(snap_file).name != snap_file:
        raise ControlAuditCorruptionError(f"Session head snapshot_file contains path traversal or dir separator: {snap_file!r}")

    expected_snap_path = get_session_revision_path(history_dir, expected_session_id, cur_rev)
    if snap_file != expected_snap_path.name:
        raise ControlAuditCorruptionError(
            f"Session head snapshot_file {snap_file!r} does not match derived revision filename {expected_snap_path.name!r}"
        )

    snap_hash = data.get("snapshot_payload_sha256")
    if not snap_hash or not isinstance(snap_hash, str) or not _SHA256_HEX_RE.match(snap_hash):
        raise ControlAuditCorruptionError(f"Session head missing or invalid snapshot_payload_sha256 (must be 64 hex chars): {snap_hash!r}")

    payload_hash = data.get("payload_sha256")
    if not payload_hash or not isinstance(payload_hash, str) or not _SHA256_HEX_RE.match(payload_hash):
        raise ControlAuditCorruptionError(f"Session head missing or invalid payload_sha256 (must be 64 hex chars): {payload_hash!r}")

    updated_at = data.get("updated_at")
    if not updated_at or not isinstance(updated_at, str):
        raise ControlAuditCorruptionError(f"Session head missing or invalid updated_at: {updated_at!r}")

    # P2-2 updated_at 必须为合法的带时区 ISO 8601 时间戳
    try:
        dt = datetime.fromisoformat(updated_at.strip())
        if dt.tzinfo is None:
            raise ControlAuditCorruptionError(f"Session head updated_at must be timezone-aware ISO 8601: {updated_at!r}")
    except Exception as e:
        if isinstance(e, ControlAuditCorruptionError):
            raise e
        raise ControlAuditCorruptionError(f"Session head updated_at is not a valid ISO 8601 timestamp: {updated_at!r}") from e

    computed = _canonical_payload_hash(data)
    if computed != payload_hash:
        raise ControlAuditCorruptionError(f"Session head payload_sha256 mismatch")

    # 3. 校验 Head 关联的 snapshot 文件存在且 session_id/revision/hash 精确匹配 (FD-bound no-follow 读取)
    try:
        snap_content, _ = _read_regular_file_no_follow(
            expected_snap_path,
            error_cls=ControlAuditCorruptionError,
        )
    except Exception as e:
        raise ControlAuditCorruptionError(
            f"Head referenced snapshot file {snap_file} cannot be read without following symlinks: {e}"
        ) from e

    try:
        snap_json = json.loads(snap_content.decode("utf-8"))
    except Exception as e:
        raise ControlAuditCorruptionError(f"Head referenced snapshot file {snap_file} cannot be parsed: {e}") from e

    if not isinstance(snap_json, dict):
        raise ControlAuditCorruptionError(f"Head referenced snapshot file {snap_file} is not a JSON object")

    snap_sid = snap_json.get("session_id")
    if snap_sid != expected_session_id:
        raise ControlAuditCorruptionError(
            f"Snapshot file {snap_file} session_id {snap_sid!r} does not match Head session_id {expected_session_id!r}"
        )

    snap_rev = snap_json.get("session_revision")
    if snap_rev != cur_rev:
        raise ControlAuditCorruptionError(
            f"Snapshot file {snap_file} revision {snap_rev!r} does not match Head current_revision {cur_rev}"
        )

    actual_snap_hash = _canonical_payload_hash(snap_json)
    if actual_snap_hash != snap_hash:
        raise ControlAuditCorruptionError(
            f"Snapshot file {snap_file} payload hash {actual_snap_hash[:16]}... mismatch with Head snapshot_payload_sha256 {snap_hash[:16]}..."
        )

    return data


def read_session_head(history_dir: Path, session_id: str) -> Optional[Dict[str, Any]]:
    """读取指定 session_id 的权威 Session Head 文件。

    如果 Head 文件不存在，返回 None。
    如果 Head 文件为 symlink (包含 broken symlink)、非普通文件、解析/校验失败，抛出 ControlAuditCorruptionError (fail closed)。
    """
    head_path = get_session_head_path(history_dir, session_id)
    try:
        head_stat = os.lstat(str(head_path))
    except FileNotFoundError:
        return None
    except OSError as e:
        raise ControlAuditCorruptionError(f"Session head stat failed for {head_path.name}: {e}") from e

    if stat.S_ISLNK(head_stat.st_mode) or not stat.S_ISREG(head_stat.st_mode):
        raise ControlAuditCorruptionError(
            f"Session head file {head_path.name} is a symlink or non-regular file"
        )

    try:
        content, _ = _read_regular_file_no_follow(
            head_path,
            expected_stat=head_stat,
            error_cls=ControlAuditCorruptionError,
        )
    except Exception as e:
        raise ControlAuditCorruptionError(
            f"Session head file {head_path.name} exists but cannot be read without following symlinks: {e}"
        ) from e

    try:
        data = json.loads(content.decode("utf-8"))
    except Exception as e:
        raise ControlAuditCorruptionError(
            f"Session head file {head_path.name} exists but cannot be read/parsed: {e}"
        ) from e

    return _validate_session_head_data(history_dir, session_id, data)


def _validate_revision_before_head_commit(
    history_dir: Path,
    session_id: str,
    current_revision: int,
    snapshot_file: str,
    expected_payload_sha256: Optional[str] = None,
) -> Dict[str, Any]:
    """在更新 Session Head 之前强制验证 revision 文件的路径安全、物理存储、身份标量与 canonical hash。

    使用 _read_regular_file_no_follow 进行 FD 绑定的 no-follow 读取，消除 TOCTOU 竞争。
    必须在 Head replace 之前完成所有校验。校验失败抛出 ControlAuditPersistenceError。
    """
    if not snapshot_file or not isinstance(snapshot_file, str):
        raise ControlAuditPersistenceError(f"snapshot_file must be a non-empty string, got {snapshot_file!r}")

    if "/" in snapshot_file or "\\" in snapshot_file or ".." in snapshot_file or Path(snapshot_file).name != snapshot_file:
        raise ControlAuditPersistenceError(f"snapshot_file contains path traversal or dir separator: {snapshot_file!r}")

    expected_snap_path = get_session_revision_path(history_dir, session_id, current_revision)
    if snapshot_file != expected_snap_path.name:
        raise ControlAuditPersistenceError(
            f"snapshot_file {snapshot_file!r} does not match derived revision filename {expected_snap_path.name!r}"
        )

    try:
        content, _ = _read_regular_file_no_follow(
            expected_snap_path,
            error_cls=ControlAuditPersistenceError,
        )
    except (ControlAuditPersistenceError, ControlAuditCommitUncertainError) as e:
        raise ControlAuditPersistenceError(f"Revision file {snapshot_file} no-follow validation failed: {e}") from e
    except OSError as e:
        raise ControlAuditPersistenceError(f"Revision file {snapshot_file} does not exist or stat failed: {e}") from e

    try:
        snap_json = json.loads(content.decode("utf-8"))
    except Exception as e:
        raise ControlAuditPersistenceError(f"Revision file {snapshot_file} cannot be read/parsed: {e}") from e

    if not isinstance(snap_json, dict):
        raise ControlAuditPersistenceError(f"Revision file {snapshot_file} is not a JSON object")

    snap_sid = snap_json.get("session_id")
    if snap_sid != session_id:
        raise ControlAuditPersistenceError(
            f"Revision file {snapshot_file} session_id mismatch: expected {session_id!r}, got {snap_sid!r}"
        )

    snap_rev = snap_json.get("session_revision")
    if snap_rev != current_revision:
        raise ControlAuditPersistenceError(
            f"Revision file {snapshot_file} session_revision mismatch: expected {current_revision!r}, got {snap_rev!r}"
        )

    real_hash = _canonical_payload_hash(snap_json)
    if not _SHA256_HEX_RE.match(real_hash):
        raise ControlAuditPersistenceError(f"Derived payload hash for {snapshot_file} is invalid: {real_hash!r}")

    if expected_payload_sha256 is not None:
        if not isinstance(expected_payload_sha256, str) or not _SHA256_HEX_RE.match(expected_payload_sha256):
            raise ControlAuditPersistenceError("snapshot_payload_sha256 is required and must be a 64-hex SHA-256 string")
        if real_hash != expected_payload_sha256:
            raise ControlAuditPersistenceError(
                f"Revision payload hash mismatch for {snapshot_file}: expected {expected_payload_sha256!r}, got {real_hash!r}"
            )

    return snap_json


def _read_revision_payload_hash(history_dir: Path, snapshot_file: str) -> str:
    """尝试读取并计算给定 snapshot_file 的可信 canonical payload hash。
    若文件不存在、无法解析或格式非 dict，抛出 ControlAuditPersistenceError（禁止零/占位 Hash）。
    """
    snap_path = history_dir / snapshot_file
    try:
        content, _ = _read_regular_file_no_follow(
            snap_path,
            error_cls=ControlAuditPersistenceError,
        )
    except Exception as e:
        raise ControlAuditPersistenceError(
            f"Cannot derive snapshot payload hash: file {snapshot_file} does not exist or is not a regular file: {e}"
        ) from e

    try:
        data = json.loads(content.decode("utf-8"))
    except Exception as e:
        raise ControlAuditPersistenceError(
            f"Cannot derive snapshot payload hash: file {snapshot_file} cannot be read/parsed: {e}"
        ) from e

    if not isinstance(data, dict):
        raise ControlAuditPersistenceError(
            f"Cannot derive snapshot payload hash: file {snapshot_file} is not a JSON object"
        )

    hash_val = _canonical_payload_hash(data)
    if not _SHA256_HEX_RE.match(hash_val):
        raise ControlAuditPersistenceError(
            f"Derived payload hash for {snapshot_file} is invalid: {hash_val!r}"
        )
    return hash_val


def update_session_head(
    history_dir: Path,
    session_id: str,
    current_revision: int,
    snapshot_file: str,
    snapshot_payload_sha256: Optional[str] = None,
) -> None:
    """原子更新 session_id 的权威 Session Head 文件（带 Head replace 前严格 revision 验证、Post-replace 安全失败语义与 inode 验证）。"""
    validated_snap = _validate_revision_before_head_commit(
        history_dir=history_dir,
        session_id=session_id,
        current_revision=current_revision,
        snapshot_file=snapshot_file,
        expected_payload_sha256=snapshot_payload_sha256,
    )
    snapshot_payload_sha256 = _canonical_payload_hash(validated_snap)

    head_path = get_session_head_path(history_dir, session_id)
    old_head_bytes: Optional[bytes] = None
    if head_path.exists():
        try:
            old_head_bytes, _ = _read_regular_file_no_follow(head_path)
        except Exception:
            pass

    head_data = {
        "schema_version": SNAPSHOT_VERSION,
        "session_id": session_id,
        "current_revision": current_revision,
        "snapshot_file": snapshot_file,
        "snapshot_payload_sha256": snapshot_payload_sha256,
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

    # 替换成功后，立即获取已提交 Head 节点的 stat (st_dev, st_ino) 并通过 FD 绑定 no-follow 读回
    try:
        committed_stat = os.lstat(str(head_path))
        os.stat(str(head_path))
        if stat.S_ISLNK(committed_stat.st_mode) or not stat.S_ISREG(committed_stat.st_mode):
            raise ControlAuditCommitUncertainError(
                f"Head replace succeeded but committed path is not a regular file for {session_id}"
            )
        read_back_bytes, _ = _read_regular_file_no_follow(
            head_path,
            expected_stat=committed_stat,
            error_cls=ControlAuditCommitUncertainError,
        )
    except Exception as exc:
        if isinstance(exc, ControlAuditCommitUncertainError):
            raise exc
        raise ControlAuditCommitUncertainError(
            f"Head replace succeeded but committed inode cannot be proven for {session_id}: {exc}"
        ) from exc

    try:
        _fsync_directory(history_dir)
        read_back = json.loads(read_back_bytes.decode("utf-8"))
        actual_hash = _canonical_payload_hash(read_back)
        if actual_hash != expected_hash:
            raise RuntimeError(
                f"Read-after-write hash mismatch for head {head_path.name}: expected={expected_hash[:16]}... actual={actual_hash[:16]}..."
            )
    except Exception as post_err:
        logger.error("Post-replace failure for session head %s: %s", head_path.name, post_err)
        # Inode 强所有权校验：必须传入有效的 committed_stat
        if _verify_owned_committed_path(head_path, payload_bytes, committed_stat):
            recovered = False
            try:
                if old_head_bytes is not None:
                    rec_temp = _write_temp_and_fsync(history_dir, head_path, old_head_bytes)
                    rec_temp.replace(head_path)
                    _fsync_directory(history_dir)
                    # 恢复旧 Head 后的完整 Head -> Revision 引用链校验
                    restored_readback_bytes, _ = _read_regular_file_no_follow(
                        head_path,
                        error_cls=ControlAuditCorruptionError,
                    )
                    restored_readback = json.loads(restored_readback_bytes.decode("utf-8"))
                    _validate_session_head_data(history_dir, session_id, restored_readback)
                    recovered = True
                else:
                    head_path.unlink(missing_ok=True)
                    _fsync_directory(history_dir)
                    if not head_path.exists():
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




def validate_response_snapshot(
    resp_snap: Any,
    expected_session_id: Optional[str] = None,
    expected_request_id: Optional[str] = None,
) -> Dict[str, Any]:
    """独立严格校验不可变 HTTP response snapshot 的字段类型与身份标量。"""
    if not isinstance(resp_snap, dict):
        raise ControlAuditCorruptionError("response_snapshot is not a JSON object")

    code = resp_snap.get("code")
    if code != 200 or isinstance(code, bool) or not isinstance(code, int):
        raise ControlAuditCorruptionError(f"response_snapshot code must be integer 200, got {code!r}")

    sess_id = resp_snap.get("session_id")
    if not isinstance(sess_id, str) or not sess_id.strip():
        raise ControlAuditCorruptionError("response_snapshot missing or invalid session_id")
    if expected_session_id and sess_id != expected_session_id:
        raise ControlAuditCorruptionError(f"response_snapshot session_id mismatch: expected {expected_session_id!r}, got {sess_id!r}")

    req_id = resp_snap.get("request_id")
    if req_id is not None and not isinstance(req_id, str):
        raise ControlAuditCorruptionError(f"response_snapshot request_id must be str or None, got {req_id!r}")
    if expected_request_id and req_id != expected_request_id:
        raise ControlAuditCorruptionError(f"response_snapshot request_id mismatch: expected {expected_request_id!r}, got {req_id!r}")

    reply = resp_snap.get("reply")
    if not isinstance(reply, str):
        raise ControlAuditCorruptionError(f"response_snapshot reply must be str, got {type(reply).__name__}")

    done = resp_snap.get("done")
    if not isinstance(done, bool):
        raise ControlAuditCorruptionError(f"response_snapshot done must be bool, got {type(done).__name__}")

    rejected = resp_snap.get("rejected")
    if not isinstance(rejected, bool):
        raise ControlAuditCorruptionError(f"response_snapshot rejected must be bool, got {type(rejected).__name__}")

    dlg_mode = resp_snap.get("dialogue_mode")
    if dlg_mode not in {"task_collection", "knowledge_qa", "emergency_intervention", "uncertain"}:
        raise ControlAuditCorruptionError(f"response_snapshot invalid dialogue_mode: {dlg_mode!r}")

    ctrl_state = resp_snap.get("control_state")
    if ctrl_state not in VALID_CONTROL_STATES:
        raise ControlAuditCorruptionError(f"response_snapshot invalid control_state: {ctrl_state!r}")

    collected = resp_snap.get("collected")
    if not isinstance(collected, dict):
        raise ControlAuditCorruptionError(f"response_snapshot collected must be dict, got {type(collected).__name__}")

    missing = resp_snap.get("missing")
    if not isinstance(missing, list):
        raise ControlAuditCorruptionError(f"response_snapshot missing must be list, got {type(missing).__name__}")

    is_retry = resp_snap.get("is_retry")
    if not isinstance(is_retry, bool):
        raise ControlAuditCorruptionError(f"response_snapshot is_retry must be bool, got {type(is_retry).__name__}")

    return resp_snap


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

    lcr = snapshot.get("last_control_request")
    if lcr is not None:
        if not isinstance(lcr, dict):
            raise ControlAuditCorruptionError(f"last_control_request must be dict or None, got {type(lcr).__name__}")
        act = lcr.get("action")
        if act not in ("stop", "pause", "abort", "cancel"):
            raise ControlAuditCorruptionError(f"last_control_request invalid action: {act!r}")
        st = lcr.get("status")
        if st != "requested":
            raise ControlAuditCorruptionError(f"last_control_request status must be 'requested', got {st!r}")
        conf = lcr.get("confidence")
        if isinstance(conf, bool) or not isinstance(conf, (int, float)) or not (0.0 <= float(conf) <= 1.0):
            raise ControlAuditCorruptionError(f"last_control_request invalid confidence: {conf!r}")
        if snap_ctrl_state != f"{act}_requested":
            raise ControlAuditCorruptionError(f"last_control_request action {act!r} mismatch with control_state {snap_ctrl_state!r}")
    else:
        if snap_ctrl_state in ("stop_requested", "pause_requested", "abort_requested", "cancel_requested"):
            raise ControlAuditCorruptionError(f"control_state is {snap_ctrl_state!r} but last_control_request is None")

    resp_snap = data.get("response_snapshot")
    validate_response_snapshot(resp_snap, sess_id, req_id)

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
    committed_stat: Optional[os.stat_result] = None
    try:
        os.link(str(temp_path), str(target_path))
        committed_via_link = True
        committed_stat = os.lstat(str(target_path))
    except FileExistsError:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass

        try:
            content, _ = _read_regular_file_no_follow(target_path, error_cls=ControlAuditCorruptionError)
            existing = json.loads(content.decode("utf-8"))
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
        if committed_stat is None:
            committed_stat = os.lstat(str(target_path))
        content, _ = _read_regular_file_no_follow(
            target_path,
            expected_stat=committed_stat,
            error_cls=ControlAuditCommitUncertainError,
        )
        read_back = json.loads(content.decode("utf-8"))
        validate_control_event(read_back, audit_data.get("session_id"), audit_data.get("request_id"))
        actual_hash = _canonical_payload_hash(read_back)
        if actual_hash != expected_payload_hash:
            raise RuntimeError(
                f"Read-after-write hash mismatch for {target_path.name}: "
                f"expected={expected_payload_hash[:16]}… actual={actual_hash[:16]}…"
            )
    except Exception as post_err:
        logger.error("Post-commit failure for control event %s: %s", target_path.name, post_err)
        if committed_stat is not None and _verify_owned_committed_path(target_path, payload_bytes, committed_stat):
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
            old_content_bytes, _ = _read_regular_file_no_follow(target_path)
        except OSError as e:
            logger.warning("Cannot read old main snapshot %s for backup: %s", target_path.name, e)

    payload_bytes = json.dumps(snapshot_data, ensure_ascii=False, indent=2).encode("utf-8")
    temp_path = _write_temp_and_fsync(history_dir, target_path, payload_bytes)

    committed_stat: Optional[os.stat_result] = None
    try:
        temp_path.replace(target_path)
        committed_stat = os.lstat(str(target_path))
    except Exception:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise

    try:
        _fsync_directory(history_dir)
        content, _ = _read_regular_file_no_follow(
            target_path,
            expected_stat=committed_stat,
            error_cls=ControlAuditCommitUncertainError,
        )
        read_back = json.loads(content.decode("utf-8"))
        actual_hash = _canonical_payload_hash(read_back)
        if actual_hash != expected_payload_hash:
            raise RuntimeError(
                f"Read-after-write hash mismatch for {target_path.name}: "
                f"expected={expected_payload_hash[:16]}… actual={actual_hash[:16]}…"
            )
    except Exception as post_err:
        logger.error("Post-replace failure for main snapshot %s: %s", target_path.name, post_err)
        if (
            committed_stat is not None
            and _verify_owned_committed_path(target_path, payload_bytes, committed_stat)
            and old_content_bytes is not None
        ):
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
                content, _ = _read_regular_file_no_follow(target_path, error_cls=ControlAuditCorruptionError)
                data = json.loads(content.decode("utf-8"))
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
            content, _ = _read_regular_file_no_follow(target, error_cls=ControlAuditCorruptionError)
            data = json.loads(content.decode("utf-8"))
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
        content, _ = _read_regular_file_no_follow(target_path, error_cls=ControlAuditCorruptionError)
        data = json.loads(content.decode("utf-8"))

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
    lock_handle: Optional[Any] = None,
) -> str:
    """保存不可变 session revision 并原子更新 Session Head。
    要求显式 parent_revision: int，禁止 parent_revision=None 的隐式 CAS bypass。
    无 Head 时仅允许 parent_revision == 0。
    """
    if parent_revision is None or not isinstance(parent_revision, int) or isinstance(parent_revision, bool):
        raise ValueError("parent_revision is required and must be an explicit int (None CAS bypass prohibited)")

    history_dir = _ensure_dir()

    with _file_lock:
        should_close_lock = False
        if lock_handle is None:
            lock_file_handle = _get_cross_process_lock(history_dir)
            should_close_lock = True
        else:
            lock_file_handle = lock_handle

        try:
            if should_close_lock:
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
                    if parent_revision != 0:
                        raise ControlAuditConflictError(
                            f"Session revision CAS conflict: no disk head exists, expected parent_revision == 0, got {parent_revision}"
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
                if should_close_lock:
                    fcntl.flock(lock_file_handle.fileno(), fcntl.LOCK_UN)
        finally:
            if should_close_lock:
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
    """维护/迁移专用的无条件追加 revision 接口。显式从 Head 提取 current_revision 作为 parent_revision，全程在跨进程锁下运行。"""
    history_dir = _ensure_dir()
    with _file_lock:
        lock_file_handle = _get_cross_process_lock(history_dir)
        try:
            fcntl.flock(lock_file_handle.fileno(), fcntl.LOCK_EX)
            cur_head = read_session_head(history_dir, session_id)
            parent_rev = cur_head["current_revision"] if cur_head is not None else 0
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
                lock_handle=lock_file_handle,
            )
        finally:
            fcntl.flock(lock_file_handle.fileno(), fcntl.LOCK_UN)
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
