"""A confirmation must not downgrade the check that produced a hard block."""
import pytest
from src.dialogue_manager import DialogueManager
from src.handlers.base import DialogueContext
from src.knowledge_retriever import KnowledgeBase
from src.simulated_time import get_current_datetime
from src.validator import ValidationResult
from tests.interaction_plan_support import ScriptedLLM
from tests.test_slot_consistency import seed_complete_valid_pipeline_task


@pytest.fixture
def offline_task(tmp_path):
    kb = KnowledgeBase()
    kb.state_info.state_file = tmp_path / 'state.yaml'
    dm = DialogueManager(ScriptedLLM(), kb)
    seed_complete_valid_pipeline_task(dm, kb)
    unit = dm.task_state['equipment_unit_id']
    kb.state_info.set_status(unit, {'overall_status': 'offline', 'is_online': False,
        'update_timestamp': get_current_datetime().isoformat()})
    return dm


@pytest.mark.parametrize('message', ['确认发布', '确认', '忽略警告'])
@pytest.mark.parametrize('purpose', ['preview', 'publish', 'runtime_execution'])
def test_offline_block_survives_confirmation(offline_task, message, purpose):
    dm = offline_task
    result = dm._refresh_validation(purpose=purpose)
    assert any(v.constraint_id == 'C020' for v in result.violations)
    dm.phase = 'blocked_hard'
    dm._blocking_violations = list(result.violations)
    version = dm.slot_store.version
    response = dm.constraint_handler.handle(DialogueContext(manager=dm, user_message=message, request_id='blocked-recheck'))
    assert response.handled
    assert dm.phase == 'blocked_hard'
    assert any(v.constraint_id == 'C020' for v in dm.slot_store.validation_result.violations)
    assert dm.slot_store.version == version
    if purpose == 'runtime_execution':
        assert dm.slot_store.validation_result.purpose == purpose


def test_recovered_external_state_can_clear_block(offline_task):
    dm = offline_task
    result = dm._refresh_validation(purpose='publish')
    dm.phase = 'blocked_hard'
    dm._blocking_violations = result.violations
    dm.kb.state_info.set_status(dm.task_state['equipment_unit_id'], {
        'overall_status': 'available', 'is_online': True,
        'update_timestamp': get_current_datetime().isoformat()})
    response = dm.constraint_handler.handle(DialogueContext(manager=dm, user_message='确认发布', request_id='recovered'))
    assert not any(v.constraint_id == 'C020' for v in dm.slot_store.validation_result.violations)
    assert dm.phase != 'blocked_hard'
    assert not response.handled


def test_validation_purpose_survives_serialization(offline_task):
    result = offline_task._refresh_validation(purpose='runtime_execution')
    restored = ValidationResult.from_dict(result.to_dict())
    assert restored.purpose == 'runtime_execution'


def test_external_refresh_keeps_runtime_execution_gate(offline_task, monkeypatch):
    dm=offline_task
    # A runtime execution check must stay a runtime check even for a future schedule.
    monkeypatch.setattr(dm.validator, '_is_task_start_now', lambda _: False)
    result=dm._refresh_validation(purpose='runtime_execution')
    assert any(v.constraint_id=='C020' for v in result.violations)
    dm.phase='blocked_hard';dm._blocking_violations=result.violations
    dm.refresh_external_state_constraints(dm.task_state['equipment_unit_id'])
    assert dm.phase=='blocked_hard'
    assert dm.slot_store.validation_result.purpose=='runtime_execution'
    assert any(v.constraint_id=='C020' for v in dm.slot_store.validation_result.violations)
