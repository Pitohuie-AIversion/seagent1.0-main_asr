"""The planner must see the warnings users refer to by title or pronoun."""

import json
import pytest
from unittest.mock import MagicMock

from src.dialogue_manager import DialogueManager
from src.knowledge_retriever import KnowledgeBase
from src.session.intent_router import IntentRouter
from src.validation.validator import Violation
from src.llm_client import INTERACTION_PLAN_JSON_SCHEMA
from tests.interaction_plan_support import ScriptedLLM, make_plan


def test_named_warning_reaches_planner_as_context_not_task_fields():
    llm = ScriptedLLM(plans=[make_plan('WRITE', warning_action='acknowledge')])
    dm = DialogueManager(llm, KnowledgeBase())
    dm.phase = 'blocked_soft'
    dm._blocking_violations = [Violation(
        constraint_id='C032',
        constraint_name='未来任务环境与遥测延后校验提示',
        severity='soft',
        message='执行窗口期前再核验环境与遥测。',
        related_fields=['start_time'],
    )]
    dm._handle_soft_warning_confirmation = MagicMock(return_value='已记录确认，任务尚未发布。')
    original = llm.classify_interaction
    llm.classify_interaction = MagicMock(side_effect=original)
    before = dm.slot_store.export_snapshot()

    reply = dm.process('确认忽略未来任务环境与遥测延后校验提示，继续。')

    context_text = llm.classify_interaction.call_args.args[0][-1]['content']
    context = json.loads(context_text.split('【当前上下文状态】', 1)[1].split('\n【最新用户输入】', 1)[0])
    assert context['active_warnings'] == [{
        'code': 'C032', 'name': '未来任务环境与遥测延后校验提示',
        'message': '执行窗口期前再核验环境与遥测。', 'severity': 'soft',
    }]
    assert 'active_warnings' not in context['filled_slots']
    dm._handle_soft_warning_confirmation.assert_called_once()
    assert len(llm.extract_calls) == 1
    assert dm.slot_store.export_snapshot() == before
    assert reply == '已记录确认，任务尚未发布。'


def test_warning_context_does_not_override_model_read_decision():
    llm = ScriptedLLM(plans=[make_plan('READ', query_intent='KNOWLEDGE_QA')])
    plan = IntentRouter(llm).route(
        '这条提示是什么意思，能忽略吗？', [], {}, phase='blocked_soft',
        active_warnings=[{'code': 'C032', 'severity': 'soft', 'message': '执行前核验'}],
    )
    assert plan.interaction_plan.operation == 'READ'
    assert plan.interaction_plan.warning_action is None


def test_native_planner_must_explicitly_decide_warning_side_effect():
    assert 'warning_action' in INTERACTION_PLAN_JSON_SCHEMA['required']
    assert INTERACTION_PLAN_JSON_SCHEMA['properties']['warning_action']['enum'] == ['acknowledge', None]


@pytest.mark.parametrize('unresolved,acknowledged', [
    (['C032'], True),
    (['未来任务环境与遥测延后校验提示'], True),
    (['C032', '水深不确定'], False),
])
def test_warning_identity_is_not_an_unresolved_task_field(unresolved, acknowledged):
    from src.slots.slot_store import Slot
    from tests.interaction_plan_support import empty_extraction
    llm = ScriptedLLM(default_plan=make_plan('WRITE', warning_action='acknowledge'),
                      default_extraction=empty_extraction(unresolved=unresolved))
    dm = DialogueManager(llm, KnowledgeBase())
    dm.slot_store.init_task_slots(dm.builder.get_schema('pipeline_inspection', 'normal'))
    for key, value in {'task_type_key': 'pipeline_inspection', 'task_type': '管缆巡检'}.items():
        dm.slot_store.slots[key] = Slot(key, value=value, status='valid')
    dm._rebuild_cache()
    dm.phase = 'blocked_soft'
    dm._blocking_violations = [Violation(constraint_id='C032',
        constraint_name='未来任务环境与遥测延后校验提示', severity='soft',
        message='执行前复核环境。', related_fields=['start_time'])]
    dm._handle_soft_warning_confirmation = MagicMock(return_value='已记录确认。')
    dm.process('确认忽略未来任务环境与遥测延后校验提示，继续。')
    assert dm._handle_soft_warning_confirmation.called is acknowledged



@pytest.mark.parametrize('replay', [False, True])
def test_oilfield_defaults_cannot_swallow_warning_acknowledgement(replay):
    from src.slots.slot_store import Slot
    from tests.interaction_plan_support import empty_extraction
    llm = ScriptedLLM(default_plan=make_plan('WRITE', warning_action='acknowledge'),
                      default_extraction=empty_extraction())
    dm = DialogueManager(llm, KnowledgeBase())
    dm.slot_store.init_task_slots(dm.builder.get_schema('tree_valve_operation', 'normal'))
    for key, value in {'task_type_key': 'tree_valve_operation', 'task_type': '采油树控制面板插入',
                       'oilfield_name': '流花11-1油田', 'oilfield_entity_id': 'liuhua_11_1',
                       'oilfield_coordinates': {'lat': 20.815, 'lon': 115.735}}.items():
        dm.slot_store.slots[key] = Slot(key, value=value, status='valid')
    dm._rebuild_cache()
    dm.phase = 'blocked_soft'
    dm._blocking_violations = [Violation(constraint_id='C032',
        constraint_name='未来任务环境与遥测延后校验提示', severity='soft',
        message='执行前复核环境。', related_fields=['start_time'])]
    dm._handle_soft_warning_confirmation = MagicMock(return_value='已记录确认。')
    if replay:
        from tests.interaction_plan_support import extraction_result, slot_candidate
        llm.default_extraction = extraction_result(slot_candidate('oilfield_name', '流花11-1油田'))
    before = dm.slot_store.export_snapshot()
    dm.process('我接受刚才这项未来作业提醒，请继续。')
    dm._handle_soft_warning_confirmation.assert_called_once()
    assert dm.slot_store.export_snapshot() == before


@pytest.mark.parametrize('proposed_depth,acknowledged', [(300, True), (320, False), ('有点深', False)])
def test_validated_replayed_values_do_not_consume_acknowledgement(proposed_depth, acknowledged):
    from src.slots.slot_store import Slot
    from tests.interaction_plan_support import extraction_result, slot_candidate
    llm = ScriptedLLM(default_plan=make_plan('WRITE', warning_action='acknowledge'),
        default_extraction=extraction_result(slot_candidate('water_depth', proposed_depth)))
    dm = DialogueManager(llm, KnowledgeBase())
    dm.slot_store.init_task_slots(dm.builder.get_schema('pipeline_inspection', 'normal'))
    for key, value in {'task_type_key': 'pipeline_inspection', 'task_type': '管缆巡检', 'water_depth': 300}.items():
        dm.slot_store.slots[key] = Slot(key, value=value, status='valid')
    dm._rebuild_cache()
    dm.phase = 'blocked_soft'
    dm._blocking_violations = [Violation(constraint_id='C032',
        constraint_name='未来任务环境与遥测延后校验提示', severity='soft',
        message='执行前复核环境。', related_fields=['start_time'])]
    dm._handle_soft_warning_confirmation = MagicMock(return_value='已记录确认。')
    before = dm.slot_store.export_snapshot()
    dm.process('我接受刚才这项未来作业提醒，请继续。')
    assert dm._handle_soft_warning_confirmation.called is acknowledged
    if acknowledged:
        assert dm.slot_store.export_snapshot() == before
