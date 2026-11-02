"""Scripted-model dialogue regressions for omitted relative start times.

These tests exercise parsing, slot validation and snapshot recovery without
loading a real model or evaluating GPU inference.
"""

from datetime import datetime

import pytest

from src.dialogue_manager import DialogueManager
from src.knowledge_retriever import KnowledgeBase
from tests.interaction_plan_support import (
    ScriptedLLM,
    extraction_result,
    make_plan,
    slot_candidate,
)


@pytest.mark.parametrize(
    "patch_v2,norm_v2",
    [(False, False), (True, False), (True, True)],
    ids=["legacy", "patch-v2", "normalization-v2"],
)
@pytest.mark.parametrize("model_has_duration,model_has_cable", [(True, True), (False, True), (False, False)])
@pytest.mark.parametrize("offset,start,end", [
    ("1小时后", "2026-10-21T00:00:00", "2026-10-21T04:00:00"),
    ("半小时后", "2026-10-20T23:30:00", "2026-10-21T03:30:00"),
    ("1.5小时后", "2026-10-21T00:30:00", "2026-10-21T04:30:00"),
    ("1小时30分钟后", "2026-10-21T00:30:00", "2026-10-21T04:30:00"),
])
def test_relative_start_and_duration_are_saved_when_model_omits_times(
    monkeypatch, tmp_path, patch_v2, norm_v2, model_has_duration, model_has_cable, offset, start, end,
):
    monkeypatch.setenv("SEAGENT_RESULT_DIR", str(tmp_path))
    monkeypatch.setattr(
        "src.temporal.simulated_time.get_current_datetime",
        lambda: datetime(2026, 10, 20, 23, 0),
    )
    monkeypatch.setattr("src.dialogue_manager.is_task_patch_v2_enabled", lambda: patch_v2)
    monkeypatch.setattr("src.dialogue_manager.is_normalization_contract_v2_enabled", lambda: norm_v2)
    llm = ScriptedLLM(default_plan=make_plan("WRITE"), default_reply="收到。")
    manager = DialogueManager(llm=llm, kb=KnowledgeBase())
    manager.slot_store.init_task_slots(manager.builder.get_schema("pipeline_inspection", "normal"))
    slots = manager.slot_store.clone_slots()
    for key, value in {
        "intent_id": "TI2026102001",
        "task_type_key": "pipeline_inspection",
        "task_type": "管缆巡检",
    }.items():
        slots[key].value = value
        slots[key].status = "valid"
    manager.slot_store.commit_transaction(slots, [], request_id="seed-relative-start")
    manager._rebuild_cache()
    manager.phase = "collecting"

    # Include complete model omission of the combined time/cable statement.
    extracted = extraction_result(*(
        [slot_candidate("cable_type", "电力电缆", raw_value="电力")] if model_has_cable else []
    ))
    extracted["time_relation"] = {
        "has_duration": True,
        "action": "SET",
        "target": "duration",
        "duration_seconds": 14400,
        "raw_text": "持续4小时",
        "confidence": 0.95,
        "keep_existing_duration": False,
    } if model_has_duration else None
    llm.queue_extraction(extracted)

    reply = manager.process(f"任务{offset}开始持续4小时  电力")

    expected = {
        "start_time": start,
        "end_time": end,
        "cable_type": "电力电缆",
    }
    for key, value in expected.items():
        assert manager.task_state.get(key) == value
        assert manager.slot_store.slots[key].status == "valid"
        assert manager.slot_store.slots[key].validation_error is None
    assert manager.slot_store.unresolved == []
    assert "缺少开始时间" not in reply
    missing = {field["key"] for field in manager.get_status()["missing"]}
    assert not missing.intersection(expected)
    assert missing  # The task still needs location and other required fields.

    restored = DialogueManager(llm=llm, kb=manager.kb)
    restored.load_snapshot(manager.export_snapshot())
    assert restored.phase == "collecting"
    assert restored.slot_store.get_task_state() == manager.slot_store.get_task_state()
    assert restored.slot_store.unresolved == []
    for key in expected:
        assert restored.slot_store.slots[key].status == "valid"
    restored_missing = {field["key"] for field in restored.get_status()["missing"]}
    assert restored_missing == missing
