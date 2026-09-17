"""Explicit user values must survive stale or incorrectly normalized model output."""
import pytest
from src.dialogue_manager import DialogueManager
from src.knowledge_retriever import KnowledgeBase
from src.slot_store import Slot
from tests.interaction_plan_support import ScriptedLLM, extraction_result, make_plan, slot_candidate

@pytest.fixture(params=[(False, False), (True, False), (True, True)])
def dialogue(request, monkeypatch):
    patch, norm = request.param
    monkeypatch.setattr('src.dialogue_manager.is_task_patch_v2_enabled', lambda: patch)
    monkeypatch.setattr('src.dialogue_manager.is_normalization_contract_v2_enabled', lambda: norm)
    llm = ScriptedLLM(default_plan=make_plan('WRITE'), default_reply='已修改。')
    dm = DialogueManager(llm, KnowledgeBase())
    dm.slot_store.init_task_slots(dm.builder.get_schema('pipeline_inspection', 'normal'))
    unit = dm.kb.resolve_robot_unit('OBSROV-001', 'pipeline_inspection')
    seed = {'task_type_key': 'pipeline_inspection', 'task_type': '管缆巡检', 'water_depth': 300,
            'start_point': {'lat': 20., 'lon':115.}, 'end_point': {'lat':20.05, 'lon':115.05},
            'equipment_family': unit['robot']['family_full_name'],
            'equipment_type': unit['robot']['full_name'], 'equipment_unit_id': unit['unit_id'],
            'equipment_name': unit['display_name']}
    for key, value in seed.items():
        dm.slot_store.slots[key] = Slot(key, value=value, status='valid')
    dm._rebuild_cache()
    dm.phase = 'collecting'
    return dm, llm

@pytest.mark.parametrize('model_value', ['115.2,20.1', {'lat':21., 'lon':114.}])
def test_labelled_coordinates_override_wrong_model_candidate(dialogue, model_value):
    dm,llm=dialogue
    llm.queue_extraction(extraction_result(slot_candidate('start_point', model_value),
        slot_candidate('end_point','115.3,20.2'), slot_candidate('water_depth',350)))
    dm.process('刚才说错了，水深改为350米，起点改为东经115.2度北纬20.1度，终点为东经115.3度北纬20.2度。')
    assert dm.task_state['start_point']=={'lat':20.1,'lon':115.2}
    assert dm.task_state['end_point']=={'lat':20.2,'lon':115.3}
    assert dm.task_state['water_depth']==350
    assert dm.slot_store.slots['start_point'].status=='valid'

@pytest.mark.parametrize('message', [
    '把机器人从观察级一号机改为天鹰座一号机，改用轻型工作级深海机器人150HP。',
    '机器人改用LROV-150-001，其他参数不变。', 'LROV-150-001'])
def test_explicit_robot_change_replaces_stale_family_variant(dialogue, message):
    dm,llm=dialogue
    old=dict(dm.task_state)
    llm.queue_extraction(extraction_result(
        slot_candidate('equipment_type',old['equipment_type'],raw_value='轻型工作级深海机器人150HP'),
        slot_candidate('equipment_family',old['equipment_family']),
        slot_candidate('equipment_unit_id','LROV-150-001')))
    dm.process(message)
    expected=dm.kb.resolve_robot_unit('LROV-150-001','pipeline_inspection')
    assert dm.task_state.get('equipment_unit_id')==expected['unit_id']
    assert dm.task_state.get('equipment_type')==expected['robot']['full_name']
    assert dm.task_state.get('equipment_family')==expected['robot']['family_full_name']
    assert dm.task_state.get('water_depth')==300

@pytest.mark.parametrize('selector', ['天鹰座一号机','LROV-150-001'])
def test_specific_unit_alias_masks_generic_suffix(selector):
    kb=KnowledgeBase()
    assert kb.resolve_robot_unit_from_text(selector, 'pipeline_inspection')['unit_id']=='LROV-150-001'

@pytest.mark.parametrize('selector', ['一号机','001','天鹰座一号机和观察级一号机','LROV-150-0010'])
def test_ambiguous_or_partial_unit_selector_is_not_guessed(selector):
    assert KnowledgeBase().resolve_robot_unit_from_text(selector, 'pipeline_inspection') is None

@pytest.mark.parametrize('message', ['不要改用天鹰座一号机。', '如果使用天鹰座一号机会怎样？', '保持观察级一号机，不要换成天鹰座一号机。'])
def test_mentions_do_not_create_device_candidates(message):
    from src.handlers.explicit_value_grounding import ground_explicit_values
    result=ground_explicit_values({'slot_candidates':[]}, message, kb=KnowledgeBase(),
                                  fields=[], current_state={}, task_type='pipeline_inspection')
    assert not result['slot_candidates']

@pytest.mark.parametrize('payload', [['高清水下摄像机'], ['激光标尺']])
def test_unit_change_invalidates_old_variant_payload(dialogue, payload):
    dm,llm=dialogue
    dm.slot_store.slots['payload']=Slot('payload', value=payload, status='valid', value_type='list')
    dm._rebuild_cache()
    llm.queue_extraction(extraction_result(slot_candidate('equipment_unit_id','LROV-150-001')))
    dm.process('机器人改用LROV-150-001。')
    assert dm.task_state.get('equipment_unit_id')=='LROV-150-001'
    assert dm.slot_store.slots['equipment_name'].status=='valid'
    assert dm.slot_store.slots['payload'].status=='missing'
    assert 'payload' not in dm.task_state

def test_reselecting_same_unit_preserves_payload(dialogue):
    dm,llm=dialogue
    dm.slot_store.slots['payload']=Slot('payload',value=['激光标尺'],status='valid',value_type='list')
    dm._rebuild_cache()
    llm.queue_extraction(extraction_result(slot_candidate('equipment_unit_id','OBSROV-75-001')))
    dm.process('仍然使用观察级一号机。')
    assert dm.task_state.get('payload')==['激光标尺']
