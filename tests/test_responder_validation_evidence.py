"""Real dialogue regressions: deferred telemetry and warning explanations."""

from unittest.mock import MagicMock

import pytest

from src.dialogue_manager import DialogueManager
from src.extraction.prompts import build_responder_messages, _format_state_snapshot_summary
from src.knowledge_retriever import KnowledgeBase
from src.validation.validator import ValidationResult, Violation
from tests.interaction_plan_support import ScriptedLLM, make_plan


def future_notice():
    return Violation('C032', '未来任务环境与遥测延后校验提示',
                     '任务执行窗口期前再核验环境与遥测。', 'soft', ['start_time'],
                     check_type='future_task_runtime_notice')


@pytest.mark.parametrize('acknowledged', [False, True])
def test_future_task_prompt_does_not_present_old_telemetry_as_live_check(acknowledged):
    context = {
        'type': 'none' if acknowledged else 'soft',
        'violations': [] if acknowledged else [future_notice()],
        'runtime_validation_deferred': acknowledged,
        'state_snapshot': {'unit_id': 'LROV-150-001', 'state': {
            'overall_status': 'unavailable', 'thruster_status': 'normal',
            'updated_at': '2026-08-21T18:03:20+08:00',
        }},
    }
    messages = build_responder_messages(
        task_state={}, built_json={}, missing_fields=[], mode='normal',
        phase='confirming' if acknowledged else 'blocked_soft',
        knowledge_context='', constraint_context=context,
        conversation_history=[], latest_user_message='继续', ROV2type={},
        support_task=['管缆巡检'], accepted_updates={},
    )
    system = messages[0]['content']
    assert '运行时核验尚未进行' in system
    assert '该提示本身不表示机器人故障' in system
    assert '【📡 所选机器人及作业环境 State 动态状态校核摘要】' not in system
    assert '2026-08-21T18:03:20' not in system


def test_missing_subsystems_are_unknown_not_implicitly_normal():
    summary = _format_state_snapshot_summary({'unit_id': 'LROV-150-001', 'state': {'overall_status': 'unknown'}})
    assert '推进器 未知' in summary
    assert '声呐系统 未知' in summary
    assert '正常' not in summary


def test_task_status_model_receives_warning_reason_without_health_assumptions(monkeypatch):
    llm = ScriptedLLM(plans=[make_plan('READ', query_intent='TASK_STATUS', source_policy='session_state')])
    dm = DialogueManager(llm, KnowledgeBase())
    dm.phase = 'blocked_soft'
    dm.slot_store.validation_result = ValidationResult(
        'pending_runtime_validation', '2026-09-26T09:00:00+08:00', 1, 1, 'test',
        state_snapshot=None, violations=[future_notice()],
    )
    dm._blocking_violations = [future_notice()]
    capture = MagicMock(return_value='该提示表示将在执行前校验，不能据此判断故障。')
    monkeypatch.setattr(llm, 'chat', capture)

    reply = dm.process('这条提示是什么意思，是不是机器人坏了？先解释，不要忽略也不要发布。')

    messages = capture.call_args.args[0]
    prompt = '\n'.join(message['content'] for message in messages)
    assert 'future_task_runtime_notice' in prompt
    assert '未来任务环境与遥测延后校验提示' in prompt
    assert '任务执行窗口期前再核验环境与遥测' in prompt
    assert dm.phase == 'blocked_soft'
    assert not dm.slot_store.validation_acknowledgements
    assert reply
