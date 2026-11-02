"""Vessel evidence must protect saved state even when the model proposes a value."""

import copy

import pytest

from src.dialogue_manager import DialogueManager
from src.handlers.explicit_value_grounding import ground_explicit_values
from src.knowledge_retriever import KnowledgeBase
from src.slots.slot_store import Slot
from tests.interaction_plan_support import extraction_result, slot_candidate
from tests.test_explicit_coordinate_device_corrections import dialogue


FIELDS = [{'key': 'support_vessel', 'allowed_values': [
    '海洋石油681', '海洋石油286', 'DSV-Oceanic',
]}]


@pytest.fixture(scope='module')
def kb():
    return KnowledgeBase()


@pytest.mark.parametrize('model_has_value', [False, True])
@pytest.mark.parametrize('message,expected', [
    ('如果天气允许，支持船用海洋石油286。', None),
    ('母船不要用海洋石油286。', None),
    ('昨天支持船用海洋石油286。', None),
    ('此前，支持船用海洋石油286。', None),
    ('昨天，支持船改用海洋石油286。', None),
    ('不要用286。', None),
    ('不要用286，水深300米。', None),
    ('支持船用286，或者681。', None),
    ('支持船用海洋石油286？', None),
    ('支持船用海洋石油286，可以吗？', None),
    ('支持船用海洋石油286，如果天气允许。', None),
    ('支持船用海洋石油286，撤销支持船修改。', None),
    ('取消母船修改，支持船用海洋石油286。', None),
    ('支持船用海洋石油286，等等，不要执行这个支持船变更。', None),
    ('支持船用海洋石油286，等等，取消这个修改。', None),
    ('支持船用海洋石油2860。', None),
    ('支持船用未知船。', None),
    ('支持船用海洋石油286或者海洋石油681。', None),
    ('支持船用海洋石油286，支持船用海洋石油681。', None),
    ('支持船用海洋石油286，水深300米可以吗？', '海洋石油286'),
    ('支持船用海洋石油286，可以把水深改为300米吗？', '海洋石油286'),
    ('支持船用海洋石油286，水深286米？', '海洋石油286'),
    ('支持船用海洋石油681？支持船用海洋石油286。', '海洋石油286'),
    ('不要用海洋石油681作为支持船，支持船改为海洋石油286。', '海洋石油286'),
    ('昨天支持船用海洋石油681，现在支持船用海洋石油286。', '海洋石油286'),
    ('之前支持船用海洋石油681，支持船改为海洋石油286。', '海洋石油286'),
    ('此前，支持船用海洋石油681，现在支持船改为海洋石油286。', '海洋石油286'),
    ('支持船用海洋石油286，如果天气允许，水深改为300米。', '海洋石油286'),
    ('支持船用海洋石油286，水深改为300米，取消这个修改。', '海洋石油286'),
    ('用286。', '海洋石油286'),
    ('用286，水深300米。', '海洋石油286'),
    ('母船用Oceanic。', 'DSV-Oceanic'),
    ('确认支持船为海洋石油286。', '海洋石油286'),
    ('确认将支持船修改为海洋石油286。', '海洋石油286'),
    ('确认支持船为海洋石油286？', None),
])
def test_vessel_assertion_evidence(kb, message, expected, model_has_value):
    candidates = [slot_candidate('support_vessel', '海洋石油286')] if model_has_value else []
    result = ground_explicit_values(
        extraction_result(*candidates), message, kb=kb, fields=FIELDS,
        current_state={'support_vessel': '海洋石油681'}, task_type='pipeline_inspection',
    )
    values = [candidate['normalized_value'] for candidate in result['slot_candidates']
              if candidate['canonical_key'] == 'support_vessel']
    assert values == ([] if expected is None else [expected])


@pytest.mark.parametrize('message', [
    '水深改为320米，如果天气允许，支持船用海洋石油286。',
    '昨天支持船用海洋石油286。水深改为320米。',
    '水深改为320米，母船不要用海洋石油286。',
    '水深改为320米，支持船用海洋石油286？',
    '水深改为320米，支持船用海洋石油286，撤销支持船修改。',
    '水深改为320米，支持船用海洋石油2860。',
])
def test_nonassertive_vessel_preserves_slot_metadata_and_other_updates(dialogue, message):
    manager, llm = dialogue
    manager.slot_store.slots['support_vessel'] = Slot(
        'support_vessel', value='海洋石油681', status='valid',
    )
    manager.slot_store.slots['intent_id'] = Slot(
        'intent_id', value='TI2026102001', status='valid',
    )
    manager._rebuild_cache()
    before = copy.deepcopy(manager.slot_store.slots['support_vessel'].to_dict())
    llm.queue_extraction(extraction_result(
        slot_candidate('support_vessel', '海洋石油286'),
        slot_candidate('water_depth', 320),
    ))

    manager.process(message)

    assert manager.slot_store.slots['support_vessel'].to_dict() == before
    assert manager.task_state['water_depth'] == 320
    llm.queue_extraction(extraction_result())
    manager.process('不要用海洋石油681作为支持船，支持船改为海洋石油286。')
    assert manager.task_state['support_vessel'] == '海洋石油286'
    restored = DialogueManager(llm, manager.kb)
    restored.load_snapshot(manager.export_snapshot())
    assert restored.slot_store.get_task_state() == manager.slot_store.get_task_state()


def test_combined_questions_and_withdrawal_preserve_all_affected_fields(dialogue):
    manager, llm = dialogue
    manager.slot_store.slots['support_vessel'] = Slot(
        'support_vessel', value='海洋石油681', status='valid',
    )
    manager.slot_store.slots['payload'] = Slot(
        'payload', value=['激光标尺'], status='valid', value_type='list',
    )
    manager._rebuild_cache()
    protected = ['support_vessel', 'payload', 'equipment_unit_id', 'equipment_type',
                 'equipment_family', 'equipment_name']
    before = {key: copy.deepcopy(manager.slot_store.slots[key].to_dict()) for key in protected}
    llm.queue_extraction(extraction_result(
        slot_candidate('support_vessel', '海洋石油286'),
        slot_candidate('equipment_unit_id', 'LROV-150-001'),
        slot_candidate('water_depth', 320),
        list_mutations=[{
            'field': 'payload', 'operation': 'clear', 'items': [], 'target_items': [],
            'raw_text': '删除激光标尺', 'confidence': 1.0, 'source': 'user_input',
        }],
    ))

    manager.process('机器人用LROV-150-001？支持船用286？删除激光标尺，取消这个修改。水深改为320米。')

    assert {key: manager.slot_store.slots[key].to_dict() for key in protected} == before
    assert manager.task_state['water_depth'] == 320
