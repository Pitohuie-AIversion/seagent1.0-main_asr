"""Field-local restrictions must not become lost punctuation or global vetoes."""

import copy
from datetime import datetime

import pytest

from src.dialogue_manager import DialogueManager
from src.handlers.explicit_value_grounding import ground_explicit_values
from src.knowledge_retriever import KnowledgeBase
from tests.interaction_plan_support import ScriptedLLM, extraction_result, make_plan, slot_candidate


FIELDS = [
    {'key': 'water_depth', 'type': 'number'},
    {'key': 'wellhead_id', 'type': 'raw'},
    {'key': 'cable_type', 'allowed_values': ['海底油气管道', '电力电缆', '光纤通信缆']},
]


@pytest.fixture(scope='module')
def kb():
    return KnowledgeBase()


@pytest.mark.parametrize('model_has_value', [False, True])
@pytest.mark.parametrize('message,key,value,expected', [
    ('井口WH-01？', 'wellhead_id', 'WH-01', None),
    ('井口WH-01，可以吗？', 'wellhead_id', 'WH-01', None),
    ('井口WH-01，是否合适？', 'wellhead_id', 'WH-01', None),
    ('井口WH-01？井口改为WH-02。', 'wellhead_id', 'WH-01', 'WH-02'),
    ('不要选WH-01井口，井口改为WH-02。', 'wellhead_id', 'WH-01', 'WH-02'),
    ('之前井口WH-01，现在井口改为WH-02。', 'wellhead_id', 'WH-01', 'WH-02'),
    ('井口WH-01，水深300米？', 'wellhead_id', 'WH-01', 'WH-01'),
    ('井口WH-01，如果天气允许，支持船用海洋石油286？', 'wellhead_id', 'WH-01', 'WH-01'),
    ('井口WH-01，先不要修改。', 'wellhead_id', 'WH-01', None),
    ('不要修改井口，井口WH-01。', 'wellhead_id', 'WH-01', None),
    ('井口WH-01，井口不能改。', 'wellhead_id', 'WH-01', None),
    ('保持井口不变，井口WH-01。', 'wellhead_id', 'WH-01', None),
    ('井口WH-01。井口WH-01或者WH-02。', 'wellhead_id', 'WH-01', None),
    ('管缆类型为电力，如果天气允许，支持船用海洋石油286。', 'cable_type', '光纤通信缆', '电力电缆'),
    ('管缆类型为电力，如果天气允许，支持船用海洋石油286可以吗？', 'cable_type', '光纤通信缆', '电力电缆'),
    ('之前管缆类型为光纤，管缆类型改为电力。', 'cable_type', '光纤通信缆', '电力电缆'),
    ('之前管缆类型为光纤，现在管缆类型改为电力。', 'cable_type', '光纤通信缆', '电力电缆'),
    ('此前，管缆类型为光纤。', 'cable_type', '光纤通信缆', None),
    ('昨天，管缆类型为电力。', 'cable_type', '电力电缆', None),
    ('此前，管缆类型为光纤，现在管缆类型为电力。', 'cable_type', '光纤通信缆', '电力电缆'),
    ('如果天气允许，管缆类型为电力。', 'cable_type', '电力电缆', None),
    ('管缆类型为电力，如果天气允许。', 'cable_type', '电力电缆', None),
    ('管缆类型为电力，如果天气允许，管缆类型为光纤。', 'cable_type', '电力电缆', None),
    ('管缆类型为电力，管缆类型不能改。', 'cable_type', '电力电缆', None),
    ('不要修改水深，水深300米。', 'water_depth', 300, None),
    ('水深300米，水深不能改。', 'water_depth', 300, None),
    ('保留水深，水深300米。', 'water_depth', 300, None),
    ('请勿修改水深，水深300米。', 'water_depth', 300, None),
    ('不要把水深改为200米，水深300米。', 'water_depth', 200, 300),
])
def test_scalar_assertion_scope(kb, message, key, value, expected, model_has_value):
    candidates = [slot_candidate(key, value)] if model_has_value else []
    result = ground_explicit_values(
        extraction_result(*candidates), message, kb=kb, fields=FIELDS,
        current_state={'wellhead_id': 'WH-00', 'water_depth': 100, 'cable_type': '海底油气管道'},
        task_type='pipeline_inspection',
    )
    actual = {candidate['canonical_key']: candidate['normalized_value'] for candidate in result['slot_candidates']}
    assert actual == ({} if expected is None else {key: expected})


@pytest.mark.parametrize('message,accepted', [
    ('取消摄像机开始时间修改，现在执行', True),
    ('现在执行，取消摄像机开始时间修改', True),
    ('现在执行，取消这个修改', False),
    ('现在开始吧', True),
    ('现在开始执行任务', True),
    ('现在执行？', False),
    ('现在执行，井口WH-01，取消这个修改', True),
])
def test_immediate_fallback_cancellation_is_bound_to_its_own_command(kb, monkeypatch, message, accepted):
    monkeypatch.setattr('src.temporal.simulated_time.get_current_datetime', lambda: datetime(2026, 10, 20, 9, 0, 37))
    result = ground_explicit_values(
        extraction_result(), message, kb=kb, fields=[{'key': 'start_time', 'type': 'datetime'}],
        current_state={}, task_type='pipeline_inspection',
    )
    actual = {candidate['canonical_key']: candidate['normalized_value'] for candidate in result['slot_candidates']}
    assert actual == ({'start_time': '2026-10-20T09:00:37'} if accepted else {})


@pytest.mark.parametrize('patch,norm', [(False, False), (True, False), (True, True)])
def test_wellhead_question_preserves_saved_slot_and_independent_depth(kb, monkeypatch, tmp_path, patch, norm):
    monkeypatch.setenv('SEAGENT_RESULT_DIR', str(tmp_path))
    monkeypatch.setattr('src.dialogue_manager.is_task_patch_v2_enabled', lambda: patch)
    monkeypatch.setattr('src.dialogue_manager.is_normalization_contract_v2_enabled', lambda: norm)
    llm = ScriptedLLM(default_plan=make_plan('WRITE'), default_reply='收到。')
    manager = DialogueManager(llm, kb)
    manager.slot_store.init_task_slots(manager.builder.get_schema('tree_valve_operation', 'normal'))
    slots = manager.slot_store.clone_slots()
    for key, value in {
        'task_type': '采油树控制面板插入', 'task_type_key': 'tree_valve_operation',
        'intent_id': 'TI2026102001', 'wellhead_id': 'WH-00', 'water_depth': 100,
    }.items():
        slots[key].value, slots[key].status = value, 'valid'
    manager.slot_store.commit_transaction(slots, [], request_id='seed-scope')
    manager._rebuild_cache()
    manager.phase = 'collecting'
    before = copy.deepcopy(manager.slot_store.slots['wellhead_id'].to_dict())
    llm.queue_extraction(extraction_result(slot_candidate('wellhead_id', 'WH-01')))

    manager.process('井口WH-01？水深300米。')

    assert manager.slot_store.slots['wellhead_id'].to_dict() == before
    assert manager.task_state['water_depth'] == 300
    llm.queue_extraction(extraction_result())
    manager.process('不要选WH-01井口，井口改为WH-02。')
    assert manager.task_state['wellhead_id'] == 'WH-02'
    restored = DialogueManager(llm, kb)
    restored.load_snapshot(manager.export_snapshot())
    assert restored.slot_store.get_task_state() == manager.slot_store.get_task_state()
