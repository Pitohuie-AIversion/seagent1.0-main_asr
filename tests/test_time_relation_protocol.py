"""Schema-constrained regressions from the real burial editing journey."""
import copy
import json
from datetime import datetime
from types import SimpleNamespace

import jsonschema
import pytest

from src.duration_parser import DurationState, parse_duration_spec
from src.extractor import ParameterExtractor
from src.llm_client import LLMClient, SLOT_EXTRACTION_JSON_SCHEMA, TEMPORAL_RELATION_JSON_SCHEMA
from src.relative_time_parser import parse_time_range

STATE = {'start_time': '2026-09-18T09:00:00', 'end_time': '2026-09-18T13:00:00'}
SHIFT = '开始时间推迟半小时，持续时间保持不变，其他参数保持不变。'
ADD = '持续时长再增加半小时，开始时间不变。'


def candidate(key, raw, value):
    return {'canonical_key': key, 'raw_key': key, 'raw_value': raw,
            'normalized_value': value, 'confidence': .95}


def relation(action='SET', target='duration', raw='半小时', seconds=1800, keep=False):
    return {'has_duration': True, 'action': action, 'target': target,
            'duration_seconds': seconds, 'raw_text': raw, 'confidence': .95,
            'keep_existing_duration': keep}


def extraction(candidates=(), rel=None):
    return {'slot_candidates': list(candidates), 'time_relation': rel,
            'list_mutations': [], 'unresolved': []}


def extract(message, candidates=(), rel=None, state=None):
    output = extraction(candidates, rel)
    jsonschema.validate(output, SLOT_EXTRACTION_JSON_SCHEMA)
    llm = SimpleNamespace(extract_json=lambda *a, **kw: copy.deepcopy(output),
        extract_temporal_relation=lambda *a, **kw: {'has_duration': False})
    return ParameterExtractor(llm).extract_updates(message,
        current_state=copy.deepcopy(STATE if state is None else state),
        task_type_key='pipeline_burial', required=[
            {'key': 'start_time', 'type': 'datetime'}, {'key': 'end_time', 'type': 'datetime'}])


def times(result):
    assert not result['unresolved'], result['unresolved']
    return {c['canonical_key']: c['normalized_value'] for c in result['slot_candidates']}


@pytest.mark.parametrize('schema', [TEMPORAL_RELATION_JSON_SCHEMA,
                                  SLOT_EXTRACTION_JSON_SCHEMA['properties']['time_relation']])
@pytest.mark.parametrize('item', [relation('ADD', 'start_time', keep=True),
                                 relation('SUB', 'end_time'),
                                 relation('KEEP', raw='持续时间保持不变', seconds=None, keep=True)])
def test_real_time_schemas_allow_executor_semantics(schema, item):
    jsonschema.validate(item, schema)


def test_real_mutation_schema_allows_existing_set_operation():
    jsonschema.validate({'field': 'payload', 'operation': 'set', 'items': ['激光标尺'],
        'target_items': [], 'raw_text': '只用激光标尺', 'confidence': 1., 'source': 'user_input'},
        SLOT_EXTRACTION_JSON_SCHEMA['properties']['list_mutations']['items'])


@pytest.mark.parametrize('raw,state,seconds', [
    ('持续时间保持不变', DurationState.KEEP, None),
    ('持续时长再增加半小时', DurationState.DELTA, 1800),
    ('持续时长再减少半小时', DurationState.DELTA, -1800),
])
def test_duration_spec_understands_keep_and_filler_words(raw, state, seconds):
    parsed = parse_duration_spec(raw)
    assert parsed.state == state
    if seconds is not None:
        assert parsed.delta_seconds == seconds


@pytest.mark.parametrize('legacy_relation', [None, {'raw_text': '半小时', 'duration_seconds': 1800, 'confidence': .95}])
def test_start_shift_and_keep_override_wrong_model_iso_and_duration(legacy_relation):
    result = extract(SHIFT, [candidate('start_time', '推迟半小时', '2026-09-18T08:30:00')], legacy_relation)
    assert times(result) == {'start_time': '2026-09-18T09:30:00', 'end_time': '2026-09-18T13:30:00'}


@pytest.mark.parametrize('action,word,start,end', [
    ('ADD', '推迟', '2026-09-18T09:30:00', '2026-09-18T13:30:00'),
    ('SUB', '提前', '2026-09-18T08:30:00', '2026-09-18T12:30:00'),
])
def test_grounded_start_shift_creates_missing_candidate(action, word, start, end):
    result = extract(f'开始时间{word}半小时，持续时间保持不变。', rel=relation(action, 'start_time', keep=True))
    assert times(result) == {'start_time': start, 'end_time': end}


@pytest.mark.parametrize('word,expected', [('增加', '2026-09-18T13:30:00'), ('减少', '2026-09-18T12:30:00')])
def test_duration_delta_overrides_model_set_and_preserves_start(word, expected):
    result = extract(f'持续时长再{word}半小时，开始时间不变。',
        [candidate('start_time', '开始时间不变', '2026-09-18T08:00:00')],
        {'raw_text': '半小时', 'duration_seconds': 1800, 'confidence': .95})
    assert times(result).get('start_time', STATE['start_time']) == STATE['start_time']
    assert times(result)['end_time'] == expected


def test_start_shift_with_fixed_end_does_not_shift_end():
    result = extract('开始时间推迟半小时，结束时间不变。', rel=relation('ADD', 'start_time'))
    assert times(result) == {'start_time': '2026-09-18T09:30:00', 'end_time': '2026-09-18T13:00:00'}


def test_start_shift_and_duration_increment_are_applied_once():
    result = extract('开始时间推迟半小时，持续时长再增加半小时。', rel=relation('ADD', 'duration'))
    assert times(result) == {'start_time': '2026-09-18T09:30:00', 'end_time': '2026-09-18T14:00:00'}


def test_end_shift_uses_existing_end_and_preserves_start():
    result = extract('结束时间提前半小时，开始时间不变。', rel=relation('SUB', 'end_time'))
    assert times(result).get('start_time', STATE['start_time']) == STATE['start_time']
    assert times(result)['end_time'] == '2026-09-18T12:30:00'


def test_explicit_duration_target_overrides_model_wrong_start_target():
    result = extract(ADD, rel=relation('SUB', 'start_time'))
    assert times(result).get('start_time', STATE['start_time']) == STATE['start_time']
    assert times(result)['end_time'] == '2026-09-18T13:30:00'


def test_explicit_start_target_overrides_model_wrong_end_target():
    result = extract(SHIFT, rel=relation('SUB', 'end_time'))
    assert times(result) == {'start_time': '2026-09-18T09:30:00', 'end_time': '2026-09-18T13:30:00'}


def test_explicit_end_is_not_discarded_when_start_moves():
    result = extract('开始时间推迟半小时，结束时间改为2026年9月18日下午2点。',
        [candidate('end_time', '2026年9月18日下午2点', '2026-09-18T14:00:00')])
    assert times(result) == {'start_time': '2026-09-18T09:30:00', 'end_time': '2026-09-18T14:00:00'}


def test_shift_without_history_cannot_accept_hallucinated_iso():
    result = extract(SHIFT, [candidate('start_time', '推迟半小时', '2026-09-18T08:30:00')],
                     state={})
    assert result['unresolved']
    assert not result['slot_candidates']


def test_shortening_beyond_existing_duration_rejects_all_time_updates():
    result = extract('持续时长再减少5小时，开始时间不变。',
        [candidate('end_time', '减少5小时', '2026-09-18T08:00:00')], relation('SUB', seconds=18000, raw='5小时'))
    assert result['unresolved']
    assert not result['slot_candidates']


@pytest.mark.parametrize('profile_v2', [False, True])
@pytest.mark.parametrize('dedicated', [False, True])
def test_schema_constrained_generation_reaches_real_extractor(monkeypatch, profile_v2, dedicated):
    """Inspect the exact schema handed to the mocked vLLM boundary, not a bypass."""
    monkeypatch.setattr('src.llm_client.is_model_profiles_v2_enabled', lambda: profile_v2)
    monkeypatch.setattr('src.llm_client.SamplingParams', lambda **kw: SimpleNamespace(**kw))
    monkeypatch.setattr('src.llm_client.StructuredOutputsParams', lambda **kw: SimpleNamespace(**kw))
    rel = relation('ADD', 'start_time', keep=True)
    outputs = [extraction([candidate('start_time', '推迟半小时', '2026-09-18T08:30:00')],
                          None if dedicated else rel)]
    if dedicated:
        outputs.append(rel)
    schemas = []
    def generate(prompts, sampling):
        output = outputs.pop(0)
        schema = sampling.structured_outputs.json
        jsonschema.validate(output, schema)
        schemas.append(schema)
        return [SimpleNamespace(outputs=[SimpleNamespace(text=json.dumps(output, ensure_ascii=False))])]
    llm = LLMClient(SimpleNamespace(generate=generate),
                    SimpleNamespace(apply_chat_template=lambda *a, **kw: 'offline prompt'))
    result = ParameterExtractor(llm).extract_updates(SHIFT, current_state=STATE,
        task_type_key='pipeline_burial', required=[{'key': 'start_time', 'type': 'datetime'},
                                                  {'key': 'end_time', 'type': 'datetime'}])
    assert times(result) == {'start_time': '2026-09-18T09:30:00', 'end_time': '2026-09-18T13:30:00'}
    assert len(schemas) == (2 if dedicated else 1)
    assert not outputs


@pytest.mark.parametrize('patch_v2,norm_v2', [(False, False), (True, False), (True, True)])
def test_burial_two_turn_edit_commits_correct_slots_in_each_pipeline(monkeypatch, tmp_path, patch_v2, norm_v2):
    from src.dialogue_manager import DialogueManager
    from src.knowledge_retriever import KnowledgeBase
    from tests.interaction_plan_support import ScriptedLLM, make_plan
    monkeypatch.setenv('SEAGENT_RESULT_DIR', str(tmp_path))
    monkeypatch.setattr('src.dialogue_manager.is_task_patch_v2_enabled', lambda: patch_v2)
    monkeypatch.setattr('src.dialogue_manager.is_normalization_contract_v2_enabled', lambda: norm_v2)
    llm = ScriptedLLM(default_plan=make_plan('WRITE'), default_reply='收到。')
    dm = DialogueManager(llm=llm, kb=KnowledgeBase())
    dm.slot_store.init_task_slots(dm.builder.get_schema('pipeline_burial', 'normal'))
    slots = dm.slot_store.clone_slots()
    for key, value in {**STATE, 'task_type_key': 'pipeline_burial', 'task_type': '管缆埋设', 'water_depth': 300}.items():
        slots[key].value, slots[key].status = value, 'valid'
    dm.slot_store.commit_transaction(slots, [], request_id='seed')
    dm._rebuild_cache()
    dm.phase = 'collecting'
    for message, output, expected in [
        (SHIFT, extraction([candidate('start_time', '推迟半小时', '2026-09-18T08:30:00')],
                           relation('ADD', 'start_time', keep=True)),
         ('2026-09-18T09:30:00', '2026-09-18T13:30:00')),
        (ADD, extraction(rel=relation('ADD')), ('2026-09-18T09:30:00', '2026-09-18T14:00:00')),
    ]:
        jsonschema.validate(output, SLOT_EXTRACTION_JSON_SCHEMA)
        llm.queue_extraction(output)
        dm.process(message)
        assert tuple(dm.slot_store.get_task_state()[key] for key in ('start_time','end_time')) == expected
        assert all(dm.slot_store.slots[key].status == 'valid' for key in ('start_time','end_time'))
