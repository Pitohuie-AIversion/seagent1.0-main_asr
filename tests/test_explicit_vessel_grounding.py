"""Explicit vessel statements must survive incomplete mixed model extraction."""
import pytest

from src.handlers.explicit_value_grounding import ground_explicit_values
from src.knowledge_retriever import KnowledgeBase
from src.slots.slot_store import Slot
from tests.test_explicit_coordinate_device_corrections import dialogue
from tests.interaction_plan_support import extraction_result, slot_candidate

FIELDS = [{'key': 'support_vessel', 'allowed_values': ['海洋石油681', '海洋石油286', 'DSV-Oceanic']}]


def ground(message, fields=FIELDS):
    return ground_explicit_values({'slot_candidates': []}, message, kb=KnowledgeBase(),
                                 fields=fields, current_state={}, task_type='tree_valve_operation')['slot_candidates']


def test_real_withdrawal_mixed_message_preserves_explicit_ship():
    message = '使用通用工作级001，带阀门扭矩工具和电气飞线插拔工具，支持船用海洋石油286。井口编号改成WC16-B2，作业结束时间改为后天下午1点。'
    assert next(candidate for candidate in ground(message) if candidate['canonical_key'] == 'support_vessel')['normalized_value'] == '海洋石油286'


@pytest.mark.parametrize('message', [
    '支持船不要用海洋石油286。', '如果支持船用海洋石油286，会怎样？',
    '支持船用海洋石油286可以吗？', '支持船用海洋石油286？',
    '水深改为300米，支持船用海洋石油286？', '支持船不是海洋石油286。',
    '支持船用海洋石油2860。', '支持船用未知船。',
    '支持船用海洋石油286，支持船用海洋石油681。',
    '水深改为320米，如果天气允许，支持船用海洋石油286。',
    '假如海况允许，支持船用海洋石油286。',
    '支持船用海洋石油286，等等，不要执行这个支持船变更。',
    '支持船用海洋石油286，等等，先不要改。',
])
def test_mentions_and_conflicts_do_not_write_ship(message):
    assert not ground(message)


def test_ship_is_not_written_outside_schema():
    assert not ground('支持船用海洋石油286。', fields=[])


def test_mixed_write_commits_ship_when_model_omits_it(dialogue):
    manager, llm = dialogue
    llm.queue_extraction(extraction_result(slot_candidate('water_depth', 320)))
    manager.process('水深改为320米，支持船用海洋石油286。')
    assert manager.task_state['water_depth'] == 320
    assert manager.task_state['support_vessel'] == '海洋石油286'


@pytest.mark.parametrize('message', [
    '如果天气不好，不出海。支持船用海洋石油286。',
    '水深不变，支持船用海洋石油286。',
])
def test_independent_affirmative_ship_selection(message):
    assert next(c for c in ground(message) if c['canonical_key'] == 'support_vessel')['normalized_value'] == '海洋石油286'


def test_conditional_mixed_write_does_not_replace_existing_ship(dialogue):
    manager, llm = dialogue
    manager.slot_store.slots['support_vessel'] = Slot('support_vessel', value='海洋石油681', status='valid')
    manager._rebuild_cache()
    llm.queue_extraction(extraction_result(slot_candidate('water_depth', 320)))
    manager.process('水深改为320米，如果天气允许，支持船用海洋石油286。')
    assert manager.task_state['water_depth'] == 320
    assert manager.task_state['support_vessel'] == '海洋石油681'
