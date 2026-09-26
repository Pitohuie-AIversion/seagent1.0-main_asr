"""Future task responses must not inherit cached telemetry through the KB prompt."""
from unittest.mock import Mock

import pytest

from src.dialogue_manager import DialogueManager
from src.knowledge_retriever import KnowledgeBase
from src.slots.slot_store import Slot
from tests.interaction_plan_support import ScriptedLLM, extraction_result, make_plan, slot_candidate


STATE = {
    'task_type_key': 'tree_valve_operation', 'task_type': '采油树控制面板插入',
    'equipment_family': '通用工作级深海机器人',
    'equipment_type': '通用工作级深海机器人 250HP', 'equipment_unit_id': 'WROV-250-001',
    'oilfield_coordinates': {'lat': 20.815, 'lon': 115.735},
    'wellhead_id': 'LH11-A1', 'water_depth': 300,
}


@pytest.mark.parametrize('include_runtime', [True, False])
def test_kb_runtime_state_is_optional_but_static_background_is_preserved(monkeypatch, include_runtime):
    kb = KnowledgeBase()
    telemetry = Mock(return_value={'overall_status': 'unavailable',
                                  'update_timestamp': '2001-01-01T01:02:03',
                                  'water_current_velocity': 8.88})
    monkeypatch.setattr(kb, 'get_robot_state_dict', telemetry)
    context = kb.get_context_for_state(STATE, include_runtime_state=include_runtime)
    assert '3000' in context and '通用工作级' in context
    assert '【作业区域背景资料】' in context and '海底底质' in context
    if include_runtime:
        telemetry.assert_called_once()
        assert '【当前设备实时状态】' in context and '8.88' in context
    else:
        telemetry.assert_not_called()
        assert '【当前设备实时状态】' not in context
        assert '2001-01-01T01:02:03' not in context
        assert '8.88' not in context


@pytest.mark.parametrize('has_candidates', [True, False])
@pytest.mark.parametrize('deferred', [True, False])
def test_write_reply_paths_use_deferred_validation_for_knowledge_context(monkeypatch, has_candidates, deferred):
    extraction = extraction_result(slot_candidate('wellhead_id', 'LH11-A2')) if has_candidates else extraction_result()
    llm = ScriptedLLM(default_plan=make_plan('WRITE'), default_extraction=extraction)
    kb = KnowledgeBase()
    manager = DialogueManager(llm, kb)
    manager.slot_store.init_task_slots(manager.builder.get_schema('tree_valve_operation', 'normal'))
    for key, value in STATE.items():
        manager.slot_store.slots[key] = Slot(key, value=value, status='valid')
    manager._rebuild_cache()
    manager.phase = 'collecting'
    monkeypatch.setattr(manager, '_run_constraint_check', lambda *args, **kwargs: {
        'type': 'none', 'violations': [], 'runtime_validation_deferred': deferred,
    })
    telemetry = Mock(return_value={'update_timestamp': '2001-01-01T01:02:03'})
    monkeypatch.setattr(kb, 'get_robot_state_dict', telemetry)
    context = Mock(wraps=kb.get_context_for_state)
    monkeypatch.setattr(kb, 'get_context_for_state', context)

    manager.process('井口编号改成LH11-A2。' if has_candidates else '请继续安排当前任务。')

    assert context.call_count == 1
    assert context.call_args.kwargs == {'include_runtime_state': not deferred}
    responder_system = llm.chat_calls[-1][0]['content']
    assert ('【当前设备实时状态】\n' in responder_system) is (not deferred)
    assert ('2001-01-01T01:02:03' in responder_system) is (not deferred)
    if has_candidates:
        assert manager.task_state['wellhead_id'] == 'LH11-A2'
    else:
        assert manager.task_state['wellhead_id'] == 'LH11-A1'
