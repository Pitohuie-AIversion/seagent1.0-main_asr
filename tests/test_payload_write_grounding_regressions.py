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
from src.slot_store import Slot
from src.ui_state_builder import build_frontend_ui_state
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


def extraction(mutations=(), unresolved=()):
    result = {'slot_candidates':[], 'list_mutations':list(mutations),
              'time_relation':None, 'unresolved':list(unresolved)}
    jsonschema.validate(result, SLOT_EXTRACTION_JSON_SCHEMA)
    return result


@pytest.fixture(params=[(False,False),(True,False),(True,True)], ids=['legacy','patch-v2','norm-v2'])
def valve(request, monkeypatch):
    patch,norm=request.param
    monkeypatch.setattr('src.dialogue_manager.is_task_patch_v2_enabled', lambda:patch)
    monkeypatch.setattr('src.dialogue_manager.is_normalization_contract_v2_enabled', lambda:norm)
    monkeypatch.setattr('src.simulated_time.get_current_datetime', lambda:datetime(2026,9,16,18,40))
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
])
def test_negated_or_hypothetical_edit_cannot_apply_wrong_model_mutation(valve, message):
    dm,llm=valve
    llm.queue_plan(make_plan('WRITE'))
    llm.queue_extraction(extraction([mutation('remove',[TORQUE])]))
    dm.process(message)
    assert dm.slot_store.slots['payload'].value==INITIAL


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
