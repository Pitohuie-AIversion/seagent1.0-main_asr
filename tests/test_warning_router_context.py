"""The planner must see the warnings users refer to by title or pronoun."""

import json
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
