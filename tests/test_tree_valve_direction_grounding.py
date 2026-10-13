"""End-to-end guards for tree-valve direction grounding."""

from unittest.mock import MagicMock

from src.dialogue_manager import DialogueManager
from src.extraction.extractor import ParameterExtractor
from src.llm_client import LLMClient
from src.session.intent_router import IntentRouteResult
from src.slots.slot_store import Slot


def test_model_direction_conflict_is_rejected_before_task_type_commit():
    """A model echoing INSERT cannot override the user's explicit WITHDRAW."""
    llm = MagicMock(spec=LLMClient)
    llm.extract_json.return_value = {
        "slot_candidates": [
            {
                "canonical_key": "task_type",
                "normalized_value": "采油树控制面板插入",
                "raw_value": "采油树控制面板插入",
            },
        ],
        "unresolved": [],
    }
    llm.chat.return_value = "已更新"
    llm.filter_reply.side_effect = lambda text, *args, **kwargs: text

    manager = DialogueManager(llm=llm)
    manager.slot_store.init_task_slots(
        manager.builder.get_schema("tree_valve_operation")
    )
    for key, value in {
        "task_type": "采油树控制面板拔出",
        "task_type_key": "tree_valve_operation",
    }.items():
        manager.slot_store.slots[key] = Slot(key, value=value, status="valid")
    manager._rebuild_cache()
    manager.phase = "collecting"
    manager.intent_router.route = MagicMock(return_value=IntentRouteResult(
        interaction_type="WRITE",
        confidence=1.0,
        reason="test",
    ))

    assert isinstance(manager.extractor, ParameterExtractor)
    before = manager.slot_store.get_task_state()

    reply = manager.process("执行采油树控制面板拔出作业")

    assert manager.slot_store.get_task_state() == before
    assert manager.task_state == before
    assert "候选与用户明确方向不一致" in reply
    assert llm.extract_json.call_count == 1
