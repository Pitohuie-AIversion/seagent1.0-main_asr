"""Explicit scalar evidence must survive model omission without inventing writes."""

import copy

import pytest

from src.dialogue_manager import DialogueManager
from src.extraction.assertion_scope import task_edits_are_paused
from src.handlers.explicit_value_grounding import ground_explicit_values
from src.knowledge_retriever import KnowledgeBase
from tests.interaction_plan_support import ScriptedLLM, extraction_result, make_plan, slot_candidate


FIELDS = [
    {'key': 'water_depth', 'type': 'number'},
    {'key': 'cable_type', 'type': 'string', 'allowed_values': ['海底油气管道', '电力电缆', '光纤通信缆']},
]


@pytest.fixture(scope='module')
def kb():
    return KnowledgeBase()


def ground(kb, message, candidates=(), fields=FIELDS):
    return ground_explicit_values(
        extraction_result(*copy.deepcopy(candidates)), message, kb=kb, fields=fields,
        current_state={'water_depth': 100, 'cable_type': '海底油气管道'}, task_type='pipeline_inspection',
    )


def values(result):
    return {candidate['canonical_key']: candidate['normalized_value'] for candidate in result['slot_candidates']}


@pytest.mark.parametrize('message,expected', [
    ('水深300米', 300), ('作业水深改为三百二十米。', 320), ('水深改320米', 320),
    ('海水深度为0.3千米', 300), ('请把水深设为0.32km。', 320),
    ('水深（米）：３２０', 320), ('水深约三百米左右', 300),
    ('埋设深度1.5米，水深300米。', 300),
    ('不要修改管缆类型，水深改为320米。', 320),
    ('水深300米？水深320米。', 320),
    ('水深300米？水深300米。', 300),
    ('水深300米，光缆巡检有什么风险？', 300),
    ('之前水深100米，现在水深改为320米。', 320),
])
@pytest.mark.parametrize('model_has_wrong_value', [False, True])
def test_explicit_depth_recovers_omission_and_corrects_model(kb, message, expected, model_has_wrong_value):
    candidates = [slot_candidate('water_depth', 999, raw_value='999米')] if model_has_wrong_value else []
    result = ground(kb, message, candidates)
    assert values(result) == {'water_depth': expected}
    assert result['unresolved'] == []


@pytest.mark.parametrize('message,raw_value,expected', [
    ('水深300米', '300米', 300),
    ('作业水深改为三百二十米。', '三百二十米', 320),
    ('水深约0.32 km左右', '0.32 km', 320),
    ('水深设在300米', '300米', 300),
    ('水深定在320米', '320米', 320),
    ('作业水深在300米左右', '300米', 300),
    ('水深设定为300米', '300米', 300),
])
@pytest.mark.parametrize('model_has_value', [False, True])
def test_depth_grounding_preserves_the_value_literal(kb, message, raw_value, expected, model_has_value):
    candidates = [slot_candidate('water_depth', expected, raw_value=raw_value)] if model_has_value else []
    result = ground(kb, message, candidates)
    assert values(result) == {'water_depth': expected}
    assert result['slot_candidates'][0]['raw_value'] == raw_value


@pytest.mark.parametrize('message', [
    '不要把水深改为300米。', '水深300米？', '水深300米，可以吗？',
    '如果天气允许，水深300米。', '只讨论水深300米。',
    '水深不变，水深300米。', '水深300米，先别改。',
    '不要修改任务，水深300米。', '昨天水深300米。',
    '水深300到400米。', '水深300米。水深400米。',
    '水深300米。撤销水深修改。',
    '水深300米，水深300到400米。', '请勿把水深改为300米。',
])
def test_nonassertive_or_conflicting_depth_cannot_use_model_candidate(kb, message):
    result = ground(kb, message, [slot_candidate('water_depth', 300), slot_candidate('cable_type', '电力电缆')])
    assert 'water_depth' not in values(result)
    assert values(result) == ({} if task_edits_are_paused(message) else {'cable_type': '电力电缆'})


@pytest.mark.parametrize('message', ['埋设深度1.5米。', '设备最大作业水深300米。', '水深传感器量程300米。'])
def test_depth_recovery_requires_task_water_depth(kb, message):
    assert values(ground(kb, message)) == {}


@pytest.mark.parametrize('message', [
    '水深从300米改到350米。', '把水深调到350米。', '水深改至350米。',
])
def test_depth_update_connector_does_not_reject_model_candidate(kb, message):
    assert values(ground(kb, message, [slot_candidate('water_depth', 350)])) == {'water_depth': 350}


@pytest.mark.parametrize('message,expected', [
    ('电力', '电力电缆'), ('管缆类型为电力', '电力电缆'),
    ('电缆类型改为电力电缆。', '电力电缆'),
    ('光纤', '光纤通信缆'), ('缆线类型：光纤', '光纤通信缆'),
    ('电缆类型为电力，水深需要多少？', '电力电缆'),
    ('不要修改水深，管缆类型为电力。', '电力电缆'),
    ('不要选电力，换成光纤。', '光纤通信缆'),
    ('任务1小时后开始持续4小时  电力', '电力电缆'),
    ('作业半小时后开始持续一个半小时  光纤', '光纤通信缆'),
])
def test_cable_short_answers_and_labels_are_recovered(kb, message, expected):
    assert values(ground(kb, message)) == {'cable_type': expected}


@pytest.mark.parametrize('message', [
    '不要改为电力。', '管缆类型是电力吗？', '如果管缆类型为电力会怎样？',
    '只讨论管缆类型为电力。', '管缆类型不变。',
    '管缆类型为电力，先别改。', '管缆类型为电力。管缆类型为光纤。',
    '管缆类型不变，管缆类型改为电力。',
    '管缆类型为电力。撤销管缆类型修改。',
    '请勿把管缆类型改为电力。',
    '如果任务1小时后开始持续4小时  电力',
    '任务1小时后开始持续4小时  电力吗',
    '任务1小时后开始持续4小时  电力 光纤',
])
def test_nonassertive_or_conflicting_cable_blocks_model_guess(kb, message):
    result = ground(kb, message, [slot_candidate('cable_type', '电力电缆'), slot_candidate('water_depth', 320)])
    assert 'cable_type' not in values(result)
    assert values(result) == ({} if task_edits_are_paused(message) else {'water_depth': 320})


def test_recovery_obeys_schema_and_does_not_invent_bare_depth(kb):
    assert values(ground(kb, '水深300米，电力', fields=[])) == {}
    assert values(ground(kb, '300米')) == {}
    restricted = [{'key': 'cable_type', 'allowed_values': ['海底油气管道']}]
    assert values(ground(kb, '电力', fields=restricted)) == {}


@pytest.mark.parametrize('message,expected', [
    ('水深改为320米，如果天气允许，支持船用海洋石油286。', {'water_depth': 320}),
    ('水深改为320米，如果天气允许，支持船用海洋石油286可以吗？', {'water_depth': 320}),
    ('如果天气允许，水深改为320米，支持船用海洋石油286。', {}),
    ('水深改为320米，如果天气允许，水深改为400米。', {}),
    ('水深改为320米，如果天气允许。', {}),
])
@pytest.mark.parametrize('model_has_value', [False, True])
def test_depth_scope_is_independent_of_a_later_conditional_field(kb, message, expected, model_has_value):
    candidates = [slot_candidate('water_depth', 320)] if model_has_value else []
    assert values(ground(kb, message, candidates)) == expected


@pytest.mark.parametrize('patch,norm', [(False, False), (True, False), (True, True)])
def test_scalar_recovery_and_refused_edit_preserve_committed_state(kb, monkeypatch, tmp_path, patch, norm):
    monkeypatch.setenv('SEAGENT_RESULT_DIR', str(tmp_path))
    monkeypatch.setattr('src.dialogue_manager.is_task_patch_v2_enabled', lambda: patch)
    monkeypatch.setattr('src.dialogue_manager.is_normalization_contract_v2_enabled', lambda: norm)
    llm = ScriptedLLM(default_plan=make_plan('WRITE'), default_reply='收到。')
    dm = DialogueManager(llm, kb)
    dm.slot_store.init_task_slots(dm.builder.get_schema('pipeline_inspection', 'normal'))
    slots = dm.slot_store.clone_slots()
    for key, value in {'task_type': '管缆巡检', 'task_type_key': 'pipeline_inspection', 'intent_id': 'TI2026102001'}.items():
        slots[key].value, slots[key].status = value, 'valid'
    dm.slot_store.commit_transaction(slots, [], request_id='seed-scalar-recovery')
    dm._rebuild_cache()
    dm.phase = 'collecting'
    llm.queue_extraction(extraction_result())

    dm.process('水深三百米，电缆类型为电力。')

    assert dm.task_state['water_depth'] == 300
    assert dm.task_state['cable_type'] == '电力电缆'
    before = {key: dm.slot_store.slots[key].to_dict() for key in ('water_depth', 'cable_type')}
    llm.queue_extraction(extraction_result(slot_candidate('water_depth', 500), slot_candidate('cable_type', '光纤通信缆')))
    dm.process('不要把水深改为500米，管缆类型改为光纤可以吗？')
    assert {key: dm.slot_store.slots[key].to_dict() for key in before} == before
    restored = DialogueManager(llm, kb)
    restored.load_snapshot(dm.export_snapshot())
    assert restored.slot_store.get_task_state() == dm.slot_store.get_task_state()


@pytest.mark.parametrize('patch,norm', [(False, False), (True, False), (True, True)])
def test_recovered_invalid_depth_still_runs_normal_validation(kb, monkeypatch, tmp_path, patch, norm):
    monkeypatch.setenv('SEAGENT_RESULT_DIR', str(tmp_path))
    monkeypatch.setattr('src.dialogue_manager.is_task_patch_v2_enabled', lambda: patch)
    monkeypatch.setattr('src.dialogue_manager.is_normalization_contract_v2_enabled', lambda: norm)
    llm = ScriptedLLM(default_plan=make_plan('WRITE'), default_reply='收到。')
    dm = DialogueManager(llm, kb)
    dm.slot_store.init_task_slots(dm.builder.get_schema('pipeline_inspection', 'normal'))
    slots = dm.slot_store.clone_slots()
    for key, value in {'task_type': '管缆巡检', 'task_type_key': 'pipeline_inspection', 'intent_id': 'TI2026102001'}.items():
        slots[key].value, slots[key].status = value, 'valid'
    dm.slot_store.commit_transaction(slots, [], request_id='seed-invalid-depth')
    dm._rebuild_cache()
    dm.phase = 'collecting'
    llm.queue_extraction(extraction_result())
    dm.process('水深-3米。')
    assert dm.slot_store.slots['water_depth'].status == 'invalid'
    assert 'water_depth' not in dm.task_state


@pytest.mark.parametrize('message,expected', [
    ('井口编号WH-01', 'WH-01'),
    ('井口编号为WH-01', 'WH-01'),
    ('把井口设为WH-02。', 'WH-02'),
    ('作业井口改为WH-01', 'WH-01'),
    ('井口：1号井', '1号井'),
    ('井口编号：WH-01', 'WH-01'),
    ('选用WH-01号井口', 'WH-01'),
    ('选用WH-01井口', 'WH-01'),
    ('井口定在WH-02', 'WH-02'),
    ('井口设在WH-03', 'WH-03'),
    ('目标井口WH-01', 'WH-01'),
    ('WH-01井口', 'WH-01'),
])
def test_explicit_wellhead_grounding(kb, message, expected):
    fields = [{'key': 'wellhead_id', 'type': 'raw'}]
    result = ground_explicit_values(
        extraction_result(), message, kb=kb, fields=fields,
        current_state={'wellhead_id': 'WH-00'}, task_type='tree_valve_operation',
    )
    assert values(result) == {'wellhead_id': expected}


@pytest.mark.parametrize('message', [
    '不要把井口改为WH-01。',
    '不要选用WH-01号井口。',
    '选用WH-01号井口可以吗？',
    '井口是WH-01吗？',
    '井口WH-01合适吗？',
    '井口不变，井口WH-01。',
    '撤销井口修改。',
    '井口WH-01或者WH-02。',
    '只讨论井口WH-01。',
])
def test_nonassertive_wellhead_cannot_write(kb, message):
    fields = [{'key': 'wellhead_id', 'type': 'raw'}]
    candidates = [slot_candidate('wellhead_id', 'WH-01')]
    result = ground_explicit_values(
        extraction_result(*candidates), message, kb=kb, fields=fields,
        current_state={'wellhead_id': 'WH-00'}, task_type='tree_valve_operation',
    )
    assert 'wellhead_id' not in values(result)
