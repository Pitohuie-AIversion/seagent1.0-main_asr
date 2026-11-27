"""Comprehensive tests for BUG-4a (.claimed_* cleanup) and BUG-4b (quarantine)."""

import copy
import errno
import json
import os
import stat
from pathlib import Path
from unittest.mock import patch

import pytest

from src.dispatch import atomic_task_storage as compat_atomic_storage
from src.dispatch import result_paths as compat_result_paths
from src.dispatch.storage import atomic_task_storage as storage_atomic_storage
from src.dispatch.storage import result_paths as storage_result_paths
from src.dispatch.storage.atomic_task_storage import (
    TaskCommitUncertainError,
    quarantine_artifact,
    quarantine_file,
)
from src.dispatch.storage.result_paths import get_quarantine_dir
from src.dispatch.task_intent_builder import TaskIntentBuilder
from src.exceptions import IntentIdConflict, TaskPersistenceError
from src.knowledge_retriever import KnowledgeBase


def _make_valid_intent(intent_id: str = "TI2026072101") -> dict:
    return {
        "schema_version": 2,
        "internal_id": "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11",
        "task_id": "PI-20260721-001",
        "intent_id": intent_id,
        "task_type": "pipeline_inspection",
        "priority": 7,
        "time": {"start": None, "end": None},
        "location": {"oilfield": None, "water_depth_m": 300.0},
        "task": {"type": "pipeline_inspection", "details": {}},
        "equipment": {
            "robot_type": "observation_rov",
            "payload": [],
            "support_vessel": {"name": None},
        },
        "conditions": {},
    }


def test_get_quarantine_dir_respects_result_dir_and_env_override(tmp_path, monkeypatch):
    assert compat_result_paths.get_quarantine_dir is storage_result_paths.get_quarantine_dir
    assert compat_atomic_storage.quarantine_artifact is storage_atomic_storage.quarantine_artifact
    assert quarantine_file is quarantine_artifact

    result_dir = tmp_path / "res_root"
    monkeypatch.setenv("SEAGENT_RESULT_DIR", str(result_dir))
    monkeypatch.delenv("SEAGENT_QUARANTINE_DIR", raising=False)

    q_nocreate = get_quarantine_dir(create=False)
    assert q_nocreate == (result_dir / "quarantine").resolve()
    assert not q_nocreate.exists()

    q_create = get_quarantine_dir(create=True)
    assert q_create == (result_dir / "quarantine").resolve()
    assert q_create.is_dir()

    custom_q = tmp_path / "custom_quarantine"
    monkeypatch.setenv("SEAGENT_QUARANTINE_DIR", str(custom_q))
    q_env = get_quarantine_dir(create=True)
    assert q_env == custom_q.resolve()
    assert q_env.is_dir()


def test_clean_publish_unlinks_owned_claim_and_leaves_quarantine_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("SEAGENT_RESULT_DIR", str(tmp_path))
    monkeypatch.delenv("SEAGENT_QUARANTINE_DIR", raising=False)

    builder = TaskIntentBuilder(KnowledgeBase())
    intent = _make_valid_intent("TI2026072101")
    task_dir = tmp_path / "task"
    q_dir = get_quarantine_dir(create=False)

    opened_task_dir_flags = []
    real_open = os.open

    def spy_open(path, flags, *args, **kwargs):
        if Path(path) == task_dir:
            opened_task_dir_flags.append(flags)
        return real_open(path, flags, *args, **kwargs)

    with patch("os.open", side_effect=spy_open):
        staging = builder.create_staging(intent)
        published_name = builder.publish_staging(staging, intent)

    assert (task_dir / published_name).is_file()
    assert list(task_dir.glob(".claimed_*")) == []
    assert list(task_dir.glob(".tmp_publish_*")) == []
    if q_dir.exists():
        assert list(q_dir.iterdir()) == []
    if hasattr(os, "O_DIRECTORY"):
        assert any(flags & os.O_DIRECTORY for flags in opened_task_dir_flags)


def test_claim_cleanup_preserves_in_place_same_size_replacement(tmp_path, monkeypatch):
    monkeypatch.setenv("SEAGENT_RESULT_DIR", str(tmp_path))
    builder = TaskIntentBuilder(KnowledgeBase())
    intent = _make_valid_intent("TI2026072102")
    task_dir = tmp_path / "task"

    staging = builder.create_staging(intent)
    original_bytes = staging.read_bytes()
    # Build exact-same-length replacement bytes to verify byte comparison beyond st_size.
    forged_bytes = (b"X" * len(original_bytes))
    assert len(forged_bytes) == len(original_bytes)
    assert forged_bytes != original_bytes

    real_rename = os.rename
    claimed_paths = []

    def overwrite_claim_same_size(src, dst):
        real_rename(src, dst)
        if ".claimed_" in str(dst):
            claimed_paths.append(Path(dst))
            with open(dst, "wb") as handle:
                handle.write(forged_bytes)

    with patch("os.rename", side_effect=overwrite_claim_same_size):
        pub_name = builder.publish_staging(staging, intent)

    assert (task_dir / pub_name).is_file()
    assert len(claimed_paths) == 1
    assert claimed_paths[0].exists()
    assert claimed_paths[0].read_bytes() == forged_bytes


def test_claim_cleanup_preserves_new_inode_and_symlink_replacements(tmp_path, monkeypatch):
    monkeypatch.setenv("SEAGENT_RESULT_DIR", str(tmp_path))
    builder = TaskIntentBuilder(KnowledgeBase())

    # Case 1: new inode replacement
    intent1 = _make_valid_intent("TI2026072103")
    staging1 = builder.create_staging(intent1)
    real_rename = os.rename
    new_inode_claims = []

    def swap_new_inode(src, dst):
        real_rename(src, dst)
        if ".claimed_" in str(dst):
            dst_path = Path(dst)
            new_inode_claims.append(dst_path)
            replacement = dst_path.with_name("external_claim_tmp")
            replacement.write_text('{"external": true}', encoding="utf-8")
            os.replace(replacement, dst_path)

    with patch("os.rename", side_effect=swap_new_inode):
        builder.publish_staging(staging1, intent1)

    assert len(new_inode_claims) == 1
    assert new_inode_claims[0].read_text(encoding="utf-8") == '{"external": true}'

    # Case 2: symlink replacement
    intent2 = _make_valid_intent("TI2026072104")
    staging2 = builder.create_staging(intent2)
    target_file = tmp_path / "do_not_delete.txt"
    target_file.write_text("keep me", encoding="utf-8")
    symlink_claims = []

    def swap_symlink(src, dst):
        real_rename(src, dst)
        if ".claimed_" in str(dst):
            dst_path = Path(dst)
            symlink_claims.append(dst_path)
            dst_path.unlink()
            os.symlink(target_file, dst_path)

    with patch("os.rename", side_effect=swap_symlink):
        builder.publish_staging(staging2, intent2)

    assert len(symlink_claims) == 1
    assert symlink_claims[0].is_symlink()
    assert target_file.read_text(encoding="utf-8") == "keep me"


def test_pre_commit_failure_quarantines_owned_claim_and_cleans_owned_temp(tmp_path, monkeypatch):
    monkeypatch.setenv("SEAGENT_RESULT_DIR", str(tmp_path))
    builder = TaskIntentBuilder(KnowledgeBase())
    task_dir = tmp_path / "task"
    q_dir = get_quarantine_dir(create=False)

    # 1. Generic commit OSError failure
    intent1 = _make_valid_intent("TI2026072105")
    staging1 = builder.create_staging(intent1)
    staging1_bytes = staging1.read_bytes()

    with patch("src.dispatch.task_intent_builder._atomic_commit_noreplace", side_effect=OSError("disk error")):
        with pytest.raises(TaskPersistenceError):
            builder.publish_staging(staging1, intent1)

    assert list(task_dir.glob(".claimed_*")) == []
    assert list(task_dir.glob(".tmp_publish_*")) == []
    quarantined1 = list(q_dir.glob(".claimed_TI2026072105_*.pre_commit_failure*"))
    assert len(quarantined1) == 1
    assert quarantined1[0].read_bytes() == staging1_bytes

    # 2. FileExistsError -> IntentIdConflict race after claim
    intent2 = _make_valid_intent("TI2026072106")
    staging2 = builder.create_staging(intent2)
    staging2_bytes = staging2.read_bytes()
    final2 = task_dir / "task_intent_TI2026072106.json"
    existing_payload = copy.deepcopy(intent2)
    existing_payload["priority"] = 1

    def create_final_race(temp_file, final_file):
        final_file.write_text(json.dumps(existing_payload), encoding="utf-8")
        raise FileExistsError("target created concurrently")

    with patch("src.dispatch.task_intent_builder._atomic_commit_noreplace", side_effect=create_final_race):
        with pytest.raises(IntentIdConflict):
            builder.publish_staging(staging2, intent2)

    assert json.loads(final2.read_text(encoding="utf-8"))["priority"] == 1
    assert list(task_dir.glob(".claimed_*")) == []
    assert list(task_dir.glob(".tmp_publish_*")) == []
    quarantined2 = list(q_dir.glob(".claimed_TI2026072106_*.pre_commit_failure*"))
    assert len(quarantined2) == 1
    assert quarantined2[0].read_bytes() == staging2_bytes


def test_pre_commit_failure_preserves_externally_replaced_claim(tmp_path, monkeypatch):
    monkeypatch.setenv("SEAGENT_RESULT_DIR", str(tmp_path))
    builder = TaskIntentBuilder(KnowledgeBase())
    intent = _make_valid_intent("TI2026072107")
    task_dir = tmp_path / "task"
    q_dir = get_quarantine_dir(create=False)

    staging = builder.create_staging(intent)
    real_rename = os.rename

    def replace_claim_and_fail(src, dst):
        real_rename(src, dst)
        if ".claimed_" in str(dst):
            Path(dst).write_text('{"forged": "keep_in_task_dir"}', encoding="utf-8")

    with patch("os.rename", side_effect=replace_claim_and_fail), \
         patch("src.dispatch.task_intent_builder._atomic_commit_noreplace", side_effect=OSError("commit err")):
        with pytest.raises(TaskPersistenceError):
            builder.publish_staging(staging, intent)

    claims = list(task_dir.glob(".claimed_TI2026072107_*"))
    assert len(claims) == 1
    assert json.loads(claims[0].read_text(encoding="utf-8")) == {"forged": "keep_in_task_dir"}
    if q_dir.exists():
        assert list(q_dir.glob(".claimed_TI2026072107_*")) == []


def test_quarantine_artifact_deterministic_naming_and_exdev_fallback(tmp_path):
    q_dir = tmp_path / "quarantine"
    src_dir = tmp_path / "src"
    src_dir.mkdir()

    # Deterministic naming: .reason, .reason.1, .reason.2 without overwriting
    results = []
    for idx in range(3):
        src = src_dir / "artifact.json"
        payload = f'{{"seq": {idx}}}'.encode("utf-8")
        src.write_bytes(payload)
        dst = quarantine_artifact(src, quarantine_dir=q_dir, reason="bad / reason!")
        assert dst is not None
        assert not src.exists()
        results.append(dst)

    assert [p.name for p in results] == [
        "artifact.json.bad_reason",
        "artifact.json.bad_reason.1",
        "artifact.json.bad_reason.2",
    ]
    for idx, dst in enumerate(results):
        assert json.loads(dst.read_text(encoding="utf-8")) == {"seq": idx}

    # EXDEV fallback: cross-device link fails once with EXDEV, copies via 0o600 temp dotfile + link
    exdev_src = src_dir / "exdev_artifact.json"
    exdev_src.write_bytes(b'{"exdev": true}')
    real_link = os.link
    first_call = {"done": False}

    def link_with_exdev(s, d):
        if not first_call["done"] and Path(s) == exdev_src:
            first_call["done"] = True
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return real_link(s, d)

    with patch("os.link", side_effect=link_with_exdev):
        dst_exdev = quarantine_artifact(exdev_src, quarantine_dir=q_dir, reason="cross_dev")

    assert dst_exdev == q_dir / "exdev_artifact.json.cross_dev"
    assert dst_exdev.read_bytes() == b'{"exdev": true}'
    assert stat.S_IMODE(dst_exdev.stat().st_mode) == 0o600
    assert not exdev_src.exists()
    assert list(q_dir.glob(".quarantine_tmp_*")) == []


def test_quarantine_artifact_refuses_symlinks_directories_missing_and_unowned(tmp_path):
    q_dir = tmp_path / "quarantine"
    target = tmp_path / "real.json"
    target.write_text('{"ok": 1}', encoding="utf-8")

    # Symlink
    symlink_src = tmp_path / "sym.json"
    os.symlink(target, symlink_src)
    assert quarantine_artifact(symlink_src, quarantine_dir=q_dir) is None
    assert symlink_src.is_symlink()
    assert target.exists()

    # Directory
    dir_src = tmp_path / "subdir"
    dir_src.mkdir()
    assert quarantine_artifact(dir_src, quarantine_dir=q_dir) is None
    assert dir_src.is_dir()

    # Missing file
    assert quarantine_artifact(tmp_path / "does_not_exist.json", quarantine_dir=q_dir) is None

    # Unowned (bytes mismatch)
    owned = tmp_path / "owned.json"
    owned.write_bytes(b"actual-bytes")
    assert quarantine_artifact(owned, quarantine_dir=q_dir, expected_bytes=b"other-bytes!") is None
    assert owned.read_bytes() == b"actual-bytes"


def test_late_fsync_failure_preserves_final_and_raises_uncertain_for_recovery(tmp_path, monkeypatch):
    monkeypatch.setenv("SEAGENT_RESULT_DIR", str(tmp_path))
    builder = TaskIntentBuilder(KnowledgeBase())
    intent = _make_valid_intent("TI2026072108")
    task_dir = tmp_path / "task"
    q_dir = get_quarantine_dir(create=False)

    staging = builder.create_staging(intent)
    staging_bytes = staging.read_bytes()

    real_fsync = os.fsync

    def fail_dir_fsync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.EIO, "directory fsync failed")
        return real_fsync(fd)

    with patch("os.fsync", side_effect=fail_dir_fsync):
        with pytest.raises(TaskCommitUncertainError):
            builder.publish_staging(staging, intent)

    final_file = task_dir / "task_intent_TI2026072108.json"
    assert final_file.is_file()
    assert json.loads(final_file.read_text(encoding="utf-8")) == intent

    # Even though quarantine's directory fsync also hit fail_dir_fsync and could not finish,
    # TaskCommitUncertainError was NOT masked and final_file was preserved.
    recovered_name = builder.recover_committed(intent)
    assert recovered_name == final_file.name

    # Now test when only the first directory fsync (step 7) fails, quarantine succeeds cleanly.
    intent2 = _make_valid_intent("TI2026072109")
    staging2 = builder.create_staging(intent2)
    staging2_bytes = staging2.read_bytes()
    dir_fsync_calls = {"count": 0}

    def fail_first_dir_fsync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            dir_fsync_calls["count"] += 1
            if dir_fsync_calls["count"] == 1:
                raise OSError(errno.EIO, "first dir fsync failed")
        return real_fsync(fd)

    with patch("os.fsync", side_effect=fail_first_dir_fsync):
        with pytest.raises(TaskCommitUncertainError):
            builder.publish_staging(staging2, intent2)

    assert list(task_dir.glob(".claimed_TI2026072109_*")) == []
    q_uncertain = list(q_dir.glob(".claimed_TI2026072109_*.commit_uncertain*"))
    assert len(q_uncertain) == 1
    assert q_uncertain[0].read_bytes() == staging2_bytes
    assert builder.recover_committed(intent2) == "task_intent_TI2026072109.json"
