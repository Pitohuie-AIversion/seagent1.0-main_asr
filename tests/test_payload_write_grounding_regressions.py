"""Real journey regressions: explicit list edits and truthful WRITE receipts."""
import copy
from datetime import datetime
from types import SimpleNamespace

import jsonschema
import pytest

from src.dialogue_manager import DialogueManager
from src.handlers.write_reply_grounder import WriteReplyGrounder
from src.knowledge_retriever import KnowledgeBase
from src.llm_client import SLOT_EXTRACTION_JSON_SCHEMA
from src.slots.slot_store import Slot
from src.session.ui_state_builder import build_frontend_ui_state
from tests.interaction_plan_support import ScriptedLLM, make_plan, slot_candidate

TORQUE = '阀门扭矩工具'
HYDRAULIC = '液压飞线插拔工具'
TURBID = '浑水水下成像系统'
STEREO = '双目水下成像系统'
INITIAL = [TORQUE, HYDRAULIC, TURBID]


def mutation(operation='add', items=None, targets=None):
    return {'field':'payload', 'operation':operation, 'items':items or [],
            'target_items':targets or [], 'raw_text':'错误模型抽取',
            'confidence':0.99, 'source':'user_input'}


def extraction(mutations=(), unresolved=(), *, candidates=()):
    result = {'slot_candidates':list(candidates), 'list_mutations':list(mutations),
              'time_relation':None, 'unresolved':list(unresolved)}
    jsonschema.validate(result, SLOT_EXTRACTION_JSON_SCHEMA)
    return result


@pytest.fixture(params=[(False,False),(True,False),(True,True)], ids=['legacy','patch-v2','norm-v2'])
def valve(request, monkeypatch):
    patch,norm=request.param
    monkeypatch.setattr('src.dialogue_manager.is_task_patch_v2_enabled', lambda:patch)
    monkeypatch.setattr('src.dialogue_manager.is_normalization_contract_v2_enabled', lambda:norm)
    monkeypatch.setattr('src.temporal.simulated_time.get_current_datetime', lambda:datetime(2026,9,16,18,40))
    llm=ScriptedLLM(default_reply='已删除阀门扭矩工具，结束时间已更新为13:00。请确认清空。')
    dm=DialogueManager(llm, KnowledgeBase())
    dm.slot_store.init_task_slots(dm.builder.get_schema('tree_valve_operation','normal'))
    values={'task_type_key':'tree_valve_operation','task_type':'采油树控制面板插入',
            'oilfield_name':'流花11-1油田','oilfield_entity_id':'liuhua_11_1',
            'oilfield_coordinates':{'lat':20.815,'lon':115.735},'wellhead_id':'A04',
            'water_depth':305,'equipment_type':'通用工作级深海机器人 250HP',
            'equipment_family':'通用工作级深海机器人','equipment_unit_id':'WROV-250-001',
            'payload':INITIAL.copy(),'support_vessel':'海洋石油681',
            'start_time':'2026-09-17T09:00:00','end_time':'2026-09-17T12:00:00'}
    for key,value in values.items():
        dm.slot_store.slots[key]=Slot(key, value=value, status='valid', value_type='list' if key=='payload' else 'string')
    dm._rebuild_cache()
    dm._refresh_validation(purpose='preview')
    dm.phase='blocked_soft'
    return dm,llm


@pytest.mark.parametrize('message,bad_output,expected',[
    ('删除阀门扭矩工具，保留液压飞线插拔工具和浑水水下成像系统。', extraction([mutation(items=[HYDRAULIC,TURBID])]), [HYDRAULIC,TURBID]),
    ('删除阀门扭矩工具。', extraction([mutation(items=[TORQUE])]), [HYDRAULIC,TURBID]),
    ('把浑水水下成像系统替换成双目水下成像系统，其他工具和任务参数不变。', extraction([mutation(items=[STEREO])]), [TORQUE,HYDRAULIC,STEREO]),
    ('移除液压飞线插拔工具，阀门扭矩工具继续保留。', extraction([mutation(items=[TORQUE])]), [TORQUE,TURBID]),
    ('用双目水下成像系统替换浑水水下成像系统。', extraction([mutation(items=[STEREO])]), [TORQUE,HYDRAULIC,STEREO]),
    ('载荷改成双目水下成像系统。', extraction([mutation(items=[STEREO])]), [STEREO]),
    ('清空全部携带工具。', extraction(unresolved=['请确认清空全部携带工具']), []),
    ('清除全部载荷。', extraction([mutation(items=[TORQUE])]), []),
])
def test_explicit_edit_overrules_schema_valid_wrong_model_mutation(valve, message, bad_output, expected):
    dm,llm=valve
    before=copy.deepcopy(dm.task_state)
    llm.queue_plan(make_plan('WRITE'))
    llm.queue_extraction(bad_output)
    reply=dm.process(message)
    assert dm.slot_store.slots['payload'].value == expected
    for key,value in before.items():
        if key!='payload': assert dm.task_state.get(key)==value
    ui=build_frontend_ui_state(dm)
    assert not ui['actions']['can_publish']
    assert '13:00' not in reply
    assert '请确认清空' not in reply
    if not expected:
        assert dm.slot_store.slots['payload'].status=='missing'
        assert any(f['key']=='payload' for f in dm._last_missing)
        assert '载荷' in reply or '工具' in reply
        assert '已清空' in reply


def test_invalid_named_replacement_keeps_confirmed_payload(valve):
    dm,llm=valve
    llm.queue_plan(make_plan('WRITE'))
    llm.queue_extraction(extraction([mutation(items=[STEREO])]))
    reply=dm.process('把浑水水下成像系统替换成不存在的工具，其他不变。')
    assert dm.slot_store.slots['payload'].value==INITIAL
    assert '不存在' in reply
    assert '已删除' not in reply


@pytest.mark.parametrize('message', [
    '不要删除阀门扭矩工具。',
    '如果删除阀门扭矩工具会怎么样？',
    '删除阀门扭矩工具会有什么影响？',
    '不要清空全部携带工具。',
    '请不要替我更换载荷。',
    '不要帮我删除阀门扭矩工具。',
    '别擅自清空全部携带工具。',
])
def test_negated_or_hypothetical_edit_cannot_apply_wrong_model_mutation(valve, message):
    dm,llm=valve
    llm.queue_plan(make_plan('WRITE'))
    llm.queue_extraction(extraction([mutation('remove',[TORQUE])]))
    dm.process(message)
    assert dm.slot_store.slots['payload'].value==INITIAL
    assert not dm.slot_store.slots['payload'].validation_error


@pytest.mark.parametrize('message', [
    '水深改为320米，原有载荷保持不变。',
    '水深改为320米，保留全部携带工具。',
    '水深改为320米，阀门扭矩工具不变。',
    '水深改为320米，携带工具保持不变。',
    '水深改为320米，保持已选工具。',
])
@pytest.mark.parametrize('model_output', ['remove', 'candidate'])
def test_keep_only_payload_clause_blocks_spurious_model_edit(valve, message, model_output):
    dm, llm = valve
    candidates = [slot_candidate('water_depth', 320)]
    if model_output == 'candidate':
        candidates.append(slot_candidate('payload', [STEREO]))
    proposed = extraction(
        [mutation('remove', [TORQUE])] if model_output == 'remove' else [],
        candidates=candidates,
    )
    llm.queue_plan(make_plan('WRITE'))
    llm.queue_extraction(proposed)

    dm.process(message)

    assert dm.task_state['water_depth'] == 320
    assert dm.task_state['payload'] == INITIAL
    assert not dm.slot_store.slots['payload'].validation_error


@pytest.mark.parametrize('message', [
    '保留阀门扭矩工具，添加双目水下成像系统。',
    '添加双目水下成像系统，原有载荷保持不变。',
    '水深保持不变，载荷选择双目水下成像系统。',
])
def test_keep_clause_preserves_independent_payload_addition(valve, message):
    dm, llm = valve
    proposed = extraction(candidates=[slot_candidate('payload', [STEREO])])
    llm.queue_plan(make_plan('WRITE'))
    llm.queue_extraction(proposed)

    dm.process(message)

    assert dm.task_state['payload'] == INITIAL + [STEREO]
    assert dm.task_state['water_depth'] == 305


@pytest.mark.parametrize('prefix', ['我想', '我想要', '请你帮我', '麻烦你', '那我想', '那就请你'])
def test_polite_replacement_uses_only_named_payload_operands(valve, prefix):
    dm, llm = valve
    llm.queue_plan(make_plan('WRITE'))
    llm.queue_extraction(extraction([mutation(items=[STEREO])]))
    dm.process(f'{prefix}把浑水水下成像系统换成双目水下成像系统，其他不变。')
    assert dm.slot_store.slots['payload'].value == [TORQUE, HYDRAULIC, STEREO]


def test_clear_receipt_cannot_repeat_a_model_claim_that_payload_was_installed():
    reply = WriteReplyGrounder.ground_write_reply(
        '载荷配置：已更新为“高清水下摄像机”。',
        accepted_updates={'payload': []}, unresolved_inputs=[], task_state={},
        missing_fields=[{'key': 'payload', 'label': '携带工具'}],
    )
    assert '高清水下摄像机' not in reply
    assert '已清空' in reply


def test_empty_task_creation_cannot_claim_user_parameters_are_recorded():
    reply = WriteReplyGrounder.ground_write_reply(
        '收到您的构想，系统已记录：水深300米，任务类型为管缆巡检。',
        accepted_updates={}, unresolved_inputs=['没有可验证的任务字段候选'], task_state={},
    )
    assert '系统已记录' not in reply
    assert '水深300米' not in reply
    assert '未写入任务状态' in reply


def test_unrelated_depth_edit_cannot_repeat_an_older_payload_from_history():
    reply = WriteReplyGrounder.ground_write_reply(
        f'水深已修改为320米。当前载荷仍为“{TURBID}”。\n携带载荷：{TURBID}',
        accepted_updates={'water_depth': 320}, unresolved_inputs=[],
        task_state={'water_depth': 320, 'payload': [STEREO]},
    )
    assert TURBID not in reply
    assert STEREO in reply
    assert '320' in reply


@pytest.mark.parametrize('query_intent', ['TASK_STATUS', 'DEVICE_STATUS', 'TOOL_QUERY'])
@pytest.mark.parametrize('saved', [True, False])
def test_saved_payload_query_uses_session_facts_without_telemetry(valve, query_intent, saved):
    dm, llm = valve
    if not saved:
        dm.slot_store.slots['payload'].value = []
        dm.slot_store.slots['payload'].status = 'missing'
        dm._rebuild_cache()
    dm.slot_store.slots['payload'].validation_error = '高清水下摄像机为标配机载设备'
    before = copy.deepcopy(dm.slot_store.export_snapshot())
    llm.queue_plan(make_plan('READ', query_intent=query_intent, subject_type='payload',
                            relation='filled_fields', source_policy='session_state'))
    reply = dm.process('你刚才说摄像机已经录入，到底保存了没有？先解释，不要改配置。')
    assert dm.slot_store.export_snapshot() == before
    assert not llm.extract_calls
    assert '实时状态源' not in reply
    assert '标配机载设备' in reply
    assert '未修改任务配置' in reply
    assert TURBID in reply if saved else '尚未保存任何选配载荷' in reply


@pytest.mark.parametrize('message', [
    '把不存在的工具替换成双目水下成像系统。',
    '把浑水水下成像系统替换成假的双目水下成像系统。',
])
def test_unknown_operand_is_not_silently_replaced_by_known_substring(valve, message):
    dm,llm=valve
    llm.queue_plan(make_plan('WRITE'))
    llm.queue_extraction(extraction([mutation(items=[STEREO])]))
    reply=dm.process(message)
    assert dm.slot_store.slots['payload'].value==INITIAL
    assert '失败' in reply or '未写入' in reply


def test_payload_options_for_current_robot_are_not_routed_to_fleet_catalog(valve):
    dm, llm = valve
    before = copy.deepcopy(dm.slot_store.export_snapshot())
    llm.queue_plan(make_plan('READ', query_intent='TOOL_QUERY', subject_type='payload',
                            subject_text='成像载荷', relation='list', source_policy='session_state'))
    llm.default_reply = '该设备可选浑水水下成像系统或双目水下成像系统。'
    reply = dm.process('可能我叫错名字了。这个机器人能选哪些成像载荷？先告诉我，不修改。')
    assert '本系统当前支持以下水下机器人' not in reply
    assert TURBID in reply and STEREO in reply
    assert llm.chat_calls
    evidence_prompt = llm.chat_calls[-1][0]['content']
    assert 'selected_equipment_info' in evidence_prompt
    assert 'supported_payloads' in evidence_prompt
    assert 'all_supported_tools' not in evidence_prompt
    assert 'payload_catalog' not in evidence_prompt
    assert dm.slot_store.export_snapshot() == before
    assert not llm.extract_calls


def test_two_list_edits_preserve_other_tools(valve):
    dm,llm=valve
    llm.queue_plan(make_plan('WRITE'))
    llm.queue_extraction(extraction([mutation(items=[STEREO])]))
    dm.process('删除阀门扭矩工具，添加双目水下成像系统。')
    assert dm.slot_store.slots['payload'].value==[HYDRAULIC,TURBID,STEREO]


@pytest.mark.parametrize('message,expected', [
    ('删除阀门扭矩工具并保留其他工具。', [HYDRAULIC,TURBID]),
    ('把浑水水下成像系统换成双目水下成像系统并保留其他工具。', [TORQUE,HYDRAULIC,STEREO]),
])
def test_edit_and_keep_in_same_clause(valve,message,expected):
    dm,llm=valve
    llm.queue_plan(make_plan('WRITE'))
    llm.queue_extraction(extraction([mutation(items=[STEREO])]))
    dm.process(message)
    assert dm.slot_store.slots['payload'].value==expected


@pytest.mark.parametrize('bad_output', [
    extraction([mutation('remove', [TORQUE])]),
    extraction([mutation('set', [STEREO])]),
    extraction(candidates=[slot_candidate('payload', [STEREO])]),
], ids=['wrong-remove', 'wrong-set', 'wrong-candidate'])
@pytest.mark.parametrize('message,expected', [
    ('保留阀门扭矩工具并删除液压飞线插拔工具。', [TORQUE, TURBID]),
    ('删除液压飞线插拔工具并保留阀门扭矩工具。', [TORQUE, TURBID]),
    ('保留阀门扭矩工具，删除液压飞线插拔工具。', [TORQUE, TURBID]),
    ('保留阀门扭矩工具并且删除液压飞线插拔工具。', [TORQUE, TURBID]),
    ('保留阀门扭矩工具同时添加双目水下成像系统。', INITIAL + [STEREO]),
    ('保留阀门扭矩工具并把液压飞线插拔工具替换成双目水下成像系统。', [TORQUE, TURBID, STEREO]),
    ('保留阀门扭矩工具并用双目水下成像系统替换液压飞线插拔工具。', [TORQUE, TURBID, STEREO]),
    ('阀门扭矩工具保持不变并删除液压飞线插拔工具。', [TORQUE, TURBID]),
    ('删除液压飞线插拔工具并保留阀门扭矩工具并添加双目水下成像系统。', [TORQUE, TURBID, STEREO]),
    ('保留阀门扭矩工具并删除液压飞线插拔工具，浑水水下成像系统。', [TORQUE]),
    ('水深保持不变，载荷改成双目水下成像系统。', [STEREO]),
])
def test_keep_and_explicit_edit_are_order_independent(valve, bad_output, message, expected):
    dm, llm = valve
    before = copy.deepcopy(dm.task_state)
    version = dm.slot_store.slots['payload'].version
    llm.queue_plan(make_plan('WRITE'))
    llm.queue_extraction(bad_output)

    reply = dm.process(message)

    payload = dm.slot_store.slots['payload']
    assert payload.value == expected
    assert payload.version == version + 1
    assert payload.status == 'valid'
    assert not payload.validation_error
    assert dm.task_state['payload'] == expected
    for key, value in before.items():
        if key != 'payload':
            assert dm.task_state.get(key) == value
    receipt = next(line for line in reply.splitlines() if line.startswith('✅ 已记录：'))
    assert '携带工具' in receipt
    for item in expected:
        assert item in receipt
    for item in set(INITIAL) - set(expected):
        assert item not in receipt


@pytest.mark.parametrize('message', [
    '保留阀门扭矩工具并不要删除液压飞线插拔工具。',
    '如果保留阀门扭矩工具并删除液压飞线插拔工具会怎样？',
    '保留阀门扭矩工具并删除液压飞线插拔工具吗？',
])
def test_keep_with_nonasserted_edit_does_not_mutate_payload(valve, message):
    dm, llm = valve
    before = copy.deepcopy(dm.slot_store.slots['payload'].to_dict())
    llm.queue_plan(make_plan('WRITE'))
    llm.queue_extraction(extraction([mutation('remove', [HYDRAULIC])]))

    reply = dm.process(message)

    assert dm.slot_store.slots['payload'].to_dict() == before
    retained = next(line for line in reply.splitlines() if line.startswith('当前已保存：'))
    assert all(item in retained for item in INITIAL)
    assert not any('携带工具' in line for line in reply.splitlines() if line.startswith('✅ 已记录：'))


def test_distinct_named_payloads_are_not_conflicting_via_shared_broad_alias(valve):
    dm, llm = valve
    for info in dm.kb.assets['payload_catalog'].values():
        if info['name'] in {TORQUE, HYDRAULIC}:
            info['aliases'] = [*info.get('aliases', []), '操作工具']
    llm.queue_plan(make_plan('WRITE'))
    llm.queue_extraction(extraction([mutation('remove', [TORQUE])]))

    reply = dm.process('保留阀门扭矩工具并删除液压飞线插拔工具。')

    assert dm.task_state['payload'] == [TORQUE, TURBID]
    assert '冲突' not in reply


@pytest.mark.parametrize('message', [
    '保留阀门扭矩工具并删除阀门扭矩工具。',
    '删除阀门扭矩工具并保留阀门扭矩工具。',
    '保留阀门扭矩工具，删除阀门扭矩工具。',
    '保留阀门操作工具并删除阀门扭矩工具。',
    '保留阀门扭矩工具并把阀门扭矩工具换成双目水下成像系统。',
    '保留阀门扭矩工具并清空全部携带工具。',
    '保留全部携带工具并删除液压飞线插拔工具。',
])
def test_keep_and_destructive_edit_of_same_item_requires_clarification(valve, message):
    dm, llm = valve
    before = copy.deepcopy(dm.slot_store.slots['payload'].to_dict())
    llm.queue_plan(make_plan('WRITE'))
    llm.queue_extraction(extraction([mutation('remove', [TORQUE])]))

    reply = dm.process(message)

    assert dm.slot_store.slots['payload'].to_dict() == before
    assert '冲突' in reply and '保留' in reply
    assert '未写入' in reply
    assert any('冲突' in item for item in dm.slot_store.unresolved)


def test_only_keep_named_tool_requires_explicit_full_selection(valve):
    dm, llm = valve
    before = copy.deepcopy(dm.slot_store.slots['payload'].to_dict())
    llm.queue_plan(make_plan('WRITE'))
    llm.queue_extraction(extraction([mutation('set', [TORQUE])]))

    reply = dm.process('只保留阀门扭矩工具。')

    assert dm.slot_store.slots['payload'].to_dict() == before
    assert '仅保留' in reply or '只保留' in reply
    assert '确认' in reply


@pytest.mark.parametrize('message', [
    '保留阀门扭矩工具并删除不存在的工具。',
    '保留阀门扭矩工具并把液压飞线插拔工具替换成不存在的工具。',
    '保留阀门扭矩工具并删除液压飞线插拔工具，添加不存在的工具。',
])
def test_keep_with_unknown_operand_still_uses_atomic_validation(valve, message):
    dm, llm = valve
    version = dm.slot_store.slots['payload'].version
    llm.queue_plan(make_plan('WRITE'))
    llm.queue_extraction(extraction([mutation('remove', [HYDRAULIC])]))

    reply = dm.process(message)

    payload = dm.slot_store.slots['payload']
    assert payload.value == INITIAL
    assert payload.version == version + 1  # Failure metadata is committed, not a partial edit.
    assert payload.validation_error and '不存在' in payload.validation_error
    assert '载荷操作失败' in reply


@pytest.fixture
def observation_payload(valve):
    previous, llm = valve
    dm = DialogueManager(llm, previous.kb)
    dm.slot_store.init_task_slots(dm.builder.get_schema('pipeline_inspection', 'normal'))
    values = {
        'task_type_key': 'pipeline_inspection', 'task_type': '管缆巡检',
        'cable_type': '海底油气管道', 'start_point': {'lat': 20.1, 'lon': 115.2},
        'end_point': {'lat': 20.3, 'lon': 115.5}, 'water_depth': 320,
        'equipment_type': '观察级深海机器人 75HP', 'equipment_family': '观察级深海机器人',
        'equipment_unit_id': 'OBSROV-75-001', 'support_vessel': '海洋石油681',
        'payload': ['高清水下摄像机'],
        'start_time': '2026-10-17T09:00:00', 'end_time': '2026-10-17T23:00:00',
    }
    for key, value in values.items():
        dm.slot_store.slots[key] = Slot(key, value=value, status='valid')
    dm._rebuild_cache()
    dm._refresh_validation(purpose='preview')
    dm.phase = 'blocked_soft'
    return dm, llm


@pytest.mark.parametrize('message', [
    '新增机械扫描声呐，保留现有高清水下摄像机。',
    '保留现有高清水下摄像机并新增机械扫描声呐。',
    '新增机械扫描声呐并保留现有高清水下摄像机。',
])
@pytest.mark.parametrize('bad_output', [
    extraction(unresolved=['未抽取到工具名称']),
    extraction([mutation('remove', ['高清水下摄像机'])]),
    extraction(candidates=[slot_candidate('payload', ['机械扫描声呐'])]),
], ids=['missing', 'wrong-remove', 'wrong-candidate'])
def test_new_add_word_preserves_existing_camera(observation_payload, message, bad_output):
    dm, llm = observation_payload
    before = copy.deepcopy(dm.task_state)
    version = dm.slot_store.slots['payload'].version
    llm.queue_plan(make_plan('WRITE'))
    llm.queue_extraction(bad_output)

    reply = dm.process(message)

    assert dm.task_state['payload'] == ['高清水下摄像机', '机械扫描声呐']
    assert dm.slot_store.slots['payload'].version == version + 1
    assert not dm.slot_store.slots['payload'].validation_error
    assert {key: dm.task_state[key] for key in before if key != 'payload'} == {
        key: value for key, value in before.items() if key != 'payload'
    }
    receipt = next(line for line in reply.splitlines() if line.startswith('✅ 已记录：'))
    assert '高清水下摄像机' in receipt and '机械扫描声呐' in receipt


@pytest.mark.parametrize('message', [
    '不要新增机械扫描声呐。',
    '保留现有高清水下摄像机并不要新增机械扫描声呐。',
    '新增机械扫描声呐吗？',
    '保留现有高清水下摄像机并新增机械扫描声呐吗？',
])
def test_nonasserted_new_add_word_does_not_mutate_payload(observation_payload, message):
    dm, llm = observation_payload
    before = copy.deepcopy(dm.slot_store.slots['payload'].to_dict())
    llm.queue_plan(make_plan('WRITE'))
    llm.queue_extraction(extraction([mutation('add', ['机械扫描声呐'])]))

    reply = dm.process(message)

    assert dm.slot_store.slots['payload'].to_dict() == before
    assert not any('携带工具' in line for line in reply.splitlines() if line.startswith('✅ 已记录：'))


@pytest.mark.parametrize('message', [
    '保留现有高清水下摄像机并删除高清水下摄像机。',
    '删除高清水下摄像机，保留现有的高清水下摄像机。',
])
def test_existing_keep_prefix_does_not_hide_same_item_conflict(observation_payload, message):
    dm, llm = observation_payload
    before = copy.deepcopy(dm.slot_store.slots['payload'].to_dict())
    llm.queue_plan(make_plan('WRITE'))
    llm.queue_extraction(extraction([mutation('remove', ['高清水下摄像机'])]))

    reply = dm.process(message)

    assert dm.slot_store.slots['payload'].to_dict() == before
    assert '冲突' in reply
    assert any('冲突' in item for item in dm.slot_store.unresolved)


def test_read_device_question_cannot_apply_extractor_mutation(valve):
    dm,llm=valve
    before=copy.deepcopy(dm.slot_store.export_snapshot())
    llm.queue_plan(make_plan('READ',query_intent='DEVICE_QUERY',subject_type='device_model',
                             subject_text='观察级深海机器人 75HP',relation='capability',source_policy='local_kb'))
    reply=dm.process('观察级机器人能做采油树阀门操作吗？先解释不要改任务。')
    assert dm.slot_store.export_snapshot()==before
    assert '不覆盖' in reply or '不能' in reply
    assert not llm.extract_calls


def test_partial_commit_drops_false_claim_and_preserves_real_failure():
    reply=WriteReplyGrounder.ground_write_reply(
        '支持船和水深均已设置，已成功发布任务。',
        accepted_updates={'support_vessel':'海洋石油681'},
        unresolved_inputs=['水深超出允许范围'],missing_fields=[{'key':'water_depth','label':'水深'}])
    assert '均已设置' not in reply
    assert '已成功发布' not in reply
    assert '海洋石油681' in reply and '水深超出允许范围' in reply
    assert '仍需补充' in reply


def test_actual_state_and_constraint_explanations_replace_stale_history_claims():
    reply=WriteReplyGrounder.ground_write_reply(
        '阀门扭矩工具已移除，结束时间已写入13:00，所有设备正常。',
        accepted_updates={'start_time':'2026-09-17T09:00:00'},
        unresolved_inputs=['开始时间、持续时间和结束时间不一致'],missing_fields=[],
        task_state={'start_time':'2026-09-17T09:00:00','end_time':'2026-09-17T12:00:00','payload':INITIAL},
        constraint_context={'type':'soft','violations':[SimpleNamespace(severity='soft',constraint_id='C032',message='未来排期需在执行前校验环境与遥测。')]})
    assert '已移除' not in reply and '13:00' not in reply and '所有设备正常' not in reply
    assert '12:00' in reply and TORQUE in reply
    assert '不一致' in reply and '右侧' in reply
    assert 'C032' not in reply and '执行前校验环境与遥测' not in reply
    assert '尚未发布' in reply


def test_hard_details_are_visible_but_soft_details_remain_in_sidebar():
    reply=WriteReplyGrounder.ground_write_reply(
        '所有设备正常，可以立即发布。',accepted_updates={},unresolved_inputs=[],
        constraint_context={'type':'hard','violations':[
            {'severity':'soft','constraint_id':'C022','message':'推进器状态异常。'},
            {'severity':'hard','constraint_id':'DEPTH_LIMIT','message':'任务水深超过设备最大工作水深。'},
        ]})
    assert '所有设备正常' not in reply and '立即发布' not in reply
    assert 'C022' not in reply and '推进器状态' not in reply
    assert 'DEPTH_LIMIT' in reply and '超过设备最大工作水深' in reply
    assert '先修正' in reply and '尚未发布' in reply


@pytest.mark.parametrize('candidate_kind', ['slot_candidate', 'list_mutation'])
@pytest.mark.parametrize('verb', ['携带', '带上'])
def test_named_onboard_tool_cannot_be_substituted_by_model(valve, candidate_kind, verb):
    dm, llm = valve
    dm.slot_store.slots['payload'].value = []
    dm.slot_store.slots['payload'].status = 'missing'
    dm._rebuild_cache()
    bad = extraction([mutation(items=['电液机械臂', HYDRAULIC])])
    if candidate_kind == 'slot_candidate':
        bad['list_mutations'] = []
        bad['slot_candidates'] = [slot_candidate('payload', ['电液机械臂', HYDRAULIC],
            raw_value=['多功能液压机械臂', HYDRAULIC])]
    llm.queue_plan(make_plan('WRITE'))
    llm.queue_extraction(bad)

    dm.process(f'就用通用工作级001，支持船选择海洋石油681，{verb}多功能液压机械臂和液压飞线插拔工具，明天上午11点结束。')

    assert dm.slot_store.slots['payload'].value == [HYDRAULIC]
    assert '电液机械臂' not in dm.task_state['payload']


@pytest.mark.parametrize('message', [
    '如果携带多功能液压机械臂和液压飞线插拔工具会怎么样？',
    '请不要帮我携带电液机械臂。',
    '不要添加电液机械臂。',
    '带上电液机械臂可以吗？',
])
def test_nonasserted_carry_request_does_not_accept_model_payload(valve, message):
    dm, llm = valve
    before = copy.deepcopy(dm.slot_store.slots['payload'].value)
    llm.queue_plan(make_plan('WRITE'))
    llm.queue_extraction(extraction([mutation(items=['电液机械臂'])]))
    dm.process(message)
    assert dm.slot_store.slots['payload'].value == before


def test_explicit_optional_arm_is_still_selectable(valve):
    dm, llm = valve
    llm.queue_plan(make_plan('WRITE'))
    llm.queue_extraction(extraction([mutation(items=['多功能液压机械臂'])]))
    dm.process('携带电液机械臂。')
    assert dm.slot_store.slots['payload'].value == INITIAL + ['电液机械臂']
