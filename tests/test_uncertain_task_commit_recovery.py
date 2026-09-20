"""Recover a visible commit after its final durability check fails."""
import copy
import json
import os
import stat
from unittest.mock import patch

import pytest

from src.dispatch.task_intent_builder import TaskIntentBuilder, IntentIdConflict


@pytest.fixture
def task(tmp_path, monkeypatch):
    from tests.test_failure_recovery_benchmark import FailureRecoveryBenchmarkTest
    monkeypatch.setenv("SEAGENT_RESULT_DIR", str(tmp_path))
    case = FailureRecoveryBenchmarkTest()
    case.setUp()
    case._setup_full_confirming_task()
    return case.dm, tmp_path / "task" / "task_intent_TI2026063001.json"


def _publish_with_late_failure(manager, fault="directory"):
    original_publish, original_fsync = TaskIntentBuilder.publish_staging, os.fsync
    def fail_directory(fd):
        path = os.readlink(f"/proc/self/fd/{fd}")
        if ((fault == "directory" and stat.S_ISDIR(os.fstat(fd).st_mode))
                or (fault == "final_file" and path.endswith(".json"))):
            raise OSError(f"late {fault} fsync failure")
        return original_fsync(fd)
    def publish(builder, staging, intent):
        with patch("os.fsync", side_effect=fail_directory):
            return original_publish(builder, staging, intent)
    with patch.object(TaskIntentBuilder, "publish_staging", publish):
        return manager.process("确认发布", request_id="uncertain_commit")


@pytest.mark.parametrize("fault", ["directory", "final_file"])
def test_real_confirmation_recovers_same_file_and_official_id_without_reserving_again(task, fault):
    manager, path = task
    reply = _publish_with_late_failure(manager, fault)
    assert "持久化结果尚待核对" in reply
    assert manager.phase == "confirming" and manager.final_result is None
    artifact = json.loads(path.read_text())
    assert manager.task_state["task_id"] == artifact["task_id"]
    assert manager._pending_published_intent == artifact
    inode = path.stat().st_ino
    with patch.object(manager.builder, "reserve_task_id", side_effect=AssertionError("must reuse committed ID")):
        manager.process("确认发布", request_id="recover_uncertain_commit")
    assert manager.phase == "done" and manager.final_result == artifact
    assert manager._pending_published_intent is None
    assert path.stat().st_ino == inode
    assert json.loads(path.read_text()) == artifact
    assert "已生成并下发" not in manager.conversation_history[-1]["content"]


def test_pending_commit_survives_real_history_save_and_new_manager_restore(task):
    from src.dialogue_manager import DialogueManager
    from src.session.history_manager import load_history
    from src.dispatch.task_dispatch import save_dispatch_history
    manager, path = task
    _publish_with_late_failure(manager)
    artifact = json.loads(path.read_text())
    filename = save_dispatch_history(manager)
    restored = DialogueManager(manager.llm, manager.kb)
    restored.load_snapshot(load_history(filename))
    assert restored._pending_published_intent == artifact
    with patch.object(restored.builder, "reserve_task_id", side_effect=AssertionError("must reuse committed ID")):
        restored.process("确认发布", request_id="recover_after_history_load")
    assert restored.phase == "done" and restored.final_result == artifact


def test_recovery_never_overwrites_a_mismatched_official_file(task):
    manager, path = task
    _publish_with_late_failure(manager)
    changed = json.loads(path.read_text())
    changed["priority"] = 1
    path.write_text(json.dumps(changed))
    content = path.read_bytes()
    reply = manager.process("确认发布", request_id="mismatch_recovery")
    assert "仍待核对" in reply
    assert manager.phase == "confirming" and manager.final_result is None
    assert path.read_bytes() == content


def test_uncertain_commit_cannot_be_modified_before_recovery(task):
    manager, _ = task
    _publish_with_late_failure(manager)
    before = manager.slot_store.export_snapshot()
    reply = manager.process("把水深改成 500 米", request_id="edit_uncertain")
    assert "暂不能修改" in reply
    assert manager.slot_store.export_snapshot() == before


def test_normal_duplicate_publish_still_rejects_existing_file(task):
    manager, path = task
    manager.process("确认发布", request_id="normal_publish")
    artifact = copy.deepcopy(manager.final_result)
    builder = TaskIntentBuilder(manager.kb)
    staging = builder.create_staging(artifact)
    with pytest.raises(IntentIdConflict):
        builder.publish_staging(staging, artifact)
    assert json.loads(path.read_text()) == artifact
