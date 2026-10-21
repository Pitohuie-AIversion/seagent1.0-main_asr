"""Historical and questioned time edits cannot be replayed into a saved window."""

import copy
from datetime import datetime

import pytest

from src.dialogue_manager import DialogueManager
from tests.interaction_plan_support import extraction_result, slot_candidate
from tests.test_explicit_time_field_recovery import extract, times
from tests.test_temporal_scope_followup import STATE, manager_factory


@pytest.fixture(autouse=True)
def fixed_now(monkeypatch):
    monkeypatch.setattr(
        'src.temporal.simulated_time.get_current_datetime',
        lambda: datetime(2026, 10, 20, 9),
    )


NON_WRITES = [
    '昨天开始时间推迟半小时。',
    '此前，结束时间延长1小时。',
    '此前，持续4小时。',
    '若有需要，结束时间延长1小时。',
    '开始时间改为2026年10月21日10:00，可以吗？',
    '结束时间延长1小时，可以吗？',
    '持续4小时，是否合适？',
    '持续4小时，这个时长合适吗？',
    '开始时间改为2026年10月21日10:00，这个开始时间可以吗？',
    '开始时间改为2026年10月21日10:00，是否可以开始？',
]


@pytest.mark.parametrize('message', NON_WRITES)
@pytest.mark.parametrize('model_has_value', [False, True])
def test_nonassertive_time_preserves_saved_window(message, model_has_value):
    candidates = []
    relation = None
    if model_has_value:
        raw = ('2026年10月21日10:00' if '10:00' in message else
               '半小时' if '半小时' in message else '1小时' if '1小时' in message else '4小时')
        candidates = [
            slot_candidate('start_time', '2026-10-21T10:00:00', raw_value=raw),
            slot_candidate('end_time', '2026-10-21T14:00:00', raw_value=raw),
        ]
        relation = {'has_duration': True, 'target': 'duration', 'action': 'SET',
                    'raw_text': raw, 'duration_seconds': 14400, 'confidence': 1.0}
    original = copy.deepcopy(STATE)
    result = extract(message, candidates, relation, STATE)
    assert times(result) == {}
    assert result['unresolved'] == []
    assert STATE == original


@pytest.mark.parametrize('message,expected', [
    ('开始时间改为2026年10月21日10:00？结束时间改为2026年10月21日14:00。',
     {'end_time': '2026-10-21T14:00:00'}),
    ('开始时间改为2026年10月21日10:00，可以吗？结束时间改为2026年10月21日14:00。',
     {'end_time': '2026-10-21T14:00:00'}),
    ('是否水深改为300米，开始时间改为2026年10月21日10:00。',
     {'start_time': '2026-10-21T10:00:00', 'end_time': '2026-10-21T13:00:00'}),
    ('开始时间改为2026年10月21日10:00，可以把水深改为300米吗？',
     {'start_time': '2026-10-21T10:00:00', 'end_time': '2026-10-21T13:00:00'}),
    ('开始时间改为2026年10月21日10:00，这个结束时间可以吗？',
     {'start_time': '2026-10-21T10:00:00', 'end_time': '2026-10-21T13:00:00'}),
    ('昨天开始时间不变，现在开始时间改为2026年10月21日10:00。',
     {'start_time': '2026-10-21T10:00:00', 'end_time': '2026-10-21T13:00:00'}),
    ('昨天，结束时间延长1小时，现在开始时间改为2026年10月21日10:00。',
     {'start_time': '2026-10-21T10:00:00', 'end_time': '2026-10-21T13:00:00'}),
    ('昨天，持续4小时。现在开始时间改为2026年10月21日10:00。',
     {'start_time': '2026-10-21T10:00:00', 'end_time': '2026-10-21T13:00:00'}),
    ('昨天结束时间延长1小时，现在结束时间延长1小时。',
     {'end_time': '2026-10-21T13:00:00'}),
    ('结束时间延长1小时？结束时间延长1小时。',
     {'end_time': '2026-10-21T13:00:00'}),
])
def test_independent_present_time_edit_survives_history_and_questions(message, expected):
    result = extract(message, state=STATE)
    assert times(result) == expected
    assert result['unresolved'] == []


@pytest.mark.parametrize('message', NON_WRITES)
@pytest.mark.parametrize('model_has_value', [False, True])
def test_time_scope_protection_through_commit_and_followup(manager_factory, message, model_has_value):
    manager, llm = manager_factory()
    before = {key: copy.deepcopy(manager.slot_store.slots[key].to_dict()) for key in STATE}
    raw = ('2026年10月21日10:00' if '10:00' in message else
           '半小时' if '半小时' in message else '1小时' if '1小时' in message else '4小时')
    candidates = [slot_candidate('end_time', '2026-10-21T14:00:00', raw_value=raw)] if model_has_value else []
    llm.queue_extraction(extraction_result(*candidates))

    manager.process(message + '水深改为320米。')

    assert {key: manager.slot_store.slots[key].to_dict() for key in STATE} == before
    assert manager.task_state['water_depth'] == 320
    llm.queue_extraction(extraction_result())
    manager.process('现在开始时间改为2026年10月21日10:00。')
    assert manager.task_state['start_time'] == '2026-10-21T10:00:00'
    assert manager.task_state['end_time'] == '2026-10-21T13:00:00'
    restored = DialogueManager(llm, manager.kb)
    restored.load_snapshot(manager.export_snapshot())
    assert restored.slot_store.get_task_state() == manager.slot_store.get_task_state()
