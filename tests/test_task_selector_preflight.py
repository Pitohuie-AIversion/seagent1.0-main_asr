"""Ambiguous task selectors must not reach slots or auto-selection."""
from copy import deepcopy
from unittest.mock import MagicMock

import pytest

from src.dialogue_manager import DialogueManager
from src.session.intent_router import IntentRouteResult
from src.slots.slot_store import Slot
from tests.interaction_plan_support import extraction_result, slot_candidate


@pytest.mark.parametrize("patch_v2,norm_v2", [(False, False), (True, False), (True, True)])
@pytest.mark.parametrize("existing_task", [False, True])
@pytest.mark.parametrize("extraction", [
    extraction_result(
        slot_candidate("task_type", "采油树控制面板拔出"),
        unresolved=["采油树阀门操作存在歧义，请明确说明‘插入’或‘拔出’。"],
    ),
    extraction_result(
        slot_candidate("task_type", "采油树控制面板插入"),
        slot_candidate("task_type", "采油树控制面板拔出"),
    ),
    extraction_result(
        slot_candidate("task_type", "管缆巡检"),
        slot_candidate("task_type_key", "tree_valve_operation"),
    ),
])
def test_selector_rejection_preserves_committed_values(
    monkeypatch, patch_v2, norm_v2, existing_task, extraction,
):
    monkeypatch.setattr("src.dialogue_manager.is_task_patch_v2_enabled", lambda: patch_v2)
    monkeypatch.setattr("src.dialogue_manager.is_normalization_contract_v2_enabled", lambda: norm_v2)
    manager = DialogueManager(llm=MagicMock())
    manager.intent_router.route = MagicMock(return_value=IntentRouteResult(
        interaction_type="WRITE", confidence=1.0, reason="test",
    ))
    if existing_task:
        manager.slot_store.init_task_slots(manager.builder.get_schema("pipeline_inspection"))
        for key, value in {"task_type": "管缆巡检", "task_type_key": "pipeline_inspection", "water_depth": 100}.items():
            manager.slot_store.slots[key] = Slot(key, value=value, status="valid")
        manager._rebuild_cache()
    before = manager.slot_store.get_task_state()
    proposed = deepcopy(extraction)
    proposed["slot_candidates"].append(slot_candidate("water_depth", 50))
    manager.extractor.extract_updates = MagicMock(return_value=proposed)

    reply = manager.process("进行采油树阀门操作，水深50米")

    assert manager.slot_store.get_task_state() == before
    assert manager.task_state == before
    assert manager.phase == "collecting"
    assert manager.slot_store.slots["task_type_key"].validation_error
    assert "歧义" in reply or "冲突" in reply
    manager.extractor.extract_updates.assert_called_once()
