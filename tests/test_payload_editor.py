"""Payload editor lifecycle regressions from the real user journey."""
import copy

import pytest

from src.dialogue_manager import DialogueManager
from src.knowledge_retriever import KnowledgeBase
from src.slot_store import Slot
from src.ui_state_builder import build_frontend_ui_state
from tests.interaction_plan_support import ScriptedLLM, make_plan, extraction_result, slot_candidate


@pytest.fixture
def editor_dm():
    llm = ScriptedLLM(default_reply="参数已接收。")
    dm = DialogueManager(llm=llm, kb=KnowledgeBase())
    dm.slot_store.init_task_slots(dm.builder.get_schema("pipeline_inspection", "normal"))
    for key, value in {"task_type_key": "pipeline_inspection", "task_type": "管缆巡检", "equipment_type": "观察级深海机器人 75HP", "water_depth": 400, "payload": ["多波束声呐系统"]}.items():
        dm.slot_store.slots[key] = Slot(key, value=value, status="valid", value_type="list" if key == "payload" else "number" if key == "water_depth" else "string")
    dm._rebuild_cache()
    return dm, llm


def test_open_cancel_and_refresh_keep_committed_payload(editor_dm):
    dm, _ = editor_dm
    before = copy.deepcopy(dm.slot_store.export_snapshot())
    dm.phase = "confirming"
    dm.process("修改载荷")
    first = build_frontend_ui_state(dm)
    assert first["editing_slot"] == "payload"
    assert [s["key"] for s in first["slots"] if s["status"] != "valid"][:3] != ["payload"]
    assert dm.slot_store.export_snapshot() == before
    assert build_frontend_ui_state(dm)["editing_slot"] == "payload", "refresh should reopen the editor from committed values"
    reply = dm.process("取消载荷修改")
    assert "取消" in reply
    assert dm.slot_store.export_snapshot() == before
    assert dm.phase == "confirming"
    assert build_frontend_ui_state(dm)["editing_slot"] is None


def test_open_editor_keeps_constraint_blockers(editor_dm):
    dm, _ = editor_dm
    dm.phase = "blocked_hard"
    dm._blocking_violations = [object()]
    blockers = list(dm._blocking_violations)
    dm.process("修改载荷")
    assert dm.phase == "blocked_hard"
    assert dm._blocking_violations == blockers
    assert dm.slot_store.slots["payload"].value == ["多波束声呐系统"]


def queue_payload(llm, value):
    llm.queue_plan(make_plan("WRITE"))
    llm.queue_extraction(extraction_result(slot_candidate("payload", value)))


@pytest.mark.parametrize("patch_v2,norm_v2", [(False, False), (True, False), (True, True)])
def test_confirm_editor_replaces_list_and_closes_only_after_commit(editor_dm, monkeypatch, patch_v2, norm_v2):
    monkeypatch.setattr("src.dialogue_manager.is_task_patch_v2_enabled", lambda: patch_v2)
    monkeypatch.setattr("src.dialogue_manager.is_normalization_contract_v2_enabled", lambda: norm_v2)
    dm, llm = editor_dm
    dm.process("修改载荷")
    queue_payload(llm, ["机械扫描声呐"])
    dm.process("确认选择携带工具：机械扫描声呐")
    assert dm.slot_store.slots["payload"].value == ["机械扫描声呐"]
    assert dm.slot_store.slots["payload"].status == "valid"
    assert build_frontend_ui_state(dm)["editing_slot"] is None


def test_failed_selection_keeps_original_value_and_editor(editor_dm):
    dm, llm = editor_dm
    dm.process("修改载荷")
    queue_payload(llm, ["不存在的水下设备"])
    dm.process("确认选择携带工具：不存在的水下设备")
    assert dm.slot_store.slots["payload"].value == ["多波束声呐系统"]
    assert dm.slot_store.slots["payload"].status == "valid"
    assert build_frontend_ui_state(dm)["editing_slot"] == "payload"


def test_reset_and_history_restore_close_editor(editor_dm):
    dm, _ = editor_dm
    snapshot = dm.export_snapshot()
    dm.process("修改载荷")
    dm.load_snapshot(snapshot)
    assert build_frontend_ui_state(dm)["editing_slot"] is None
    dm.process("修改载荷")
    dm.reset()
    assert build_frontend_ui_state(dm)["editing_slot"] is None


def test_task_change_closes_editor(editor_dm):
    dm, llm = editor_dm
    dm.process("修改载荷")
    llm.queue_plan(make_plan("WRITE"))
    for _ in range(2):
        llm.queue_extraction(extraction_result(slot_candidate("task_type_key", "pipeline_burial")))
    dm.process("任务调整为管缆埋设")
    assert dm.task_state["task_type_key"] == "pipeline_burial"
    assert build_frontend_ui_state(dm)["editing_slot"] is None


def test_transaction_rollback_restores_open_editor(editor_dm, monkeypatch):
    from src.exceptions import TaskPersistenceError
    dm, _ = editor_dm
    dm.process("修改载荷")
    before = copy.deepcopy(dm.slot_store.slots["payload"].value)
    def fail_after_editing(*args, **kwargs):
        dm.editing_slot = None
        raise TaskPersistenceError("simulated request failure")
    monkeypatch.setattr(dm, "_process_internal", fail_after_editing)
    with pytest.raises(TaskPersistenceError):
        dm.process("确认选择携带工具：机械扫描声呐")
    assert dm.slot_store.slots["payload"].value == before
    assert build_frontend_ui_state(dm)["editing_slot"] == "payload"
