"""Invalid parent selectors must never broaden a robot-unit lookup."""

import pytest

from src.knowledge_retriever import KnowledgeBase


@pytest.fixture(scope='module')
def kb():
    return KnowledgeBase()


@pytest.mark.parametrize('selector', ['OBSROV-75-001', '观察级一号机'])
@pytest.mark.parametrize('variant,family', [
    ('不存在的型号', None),
    ('通用工作级深海机器人 250HP', None),  # Not compatible with inspection.
    (None, '不存在的系列'),
    ('观察级深海机器人 75HP', '轻型工作级深海机器人'),
    ('观察级深海机器人 75HP', '不存在的系列'),
])
def test_invalid_or_conflicting_parent_never_resolves_unrelated_unit(kb, selector, variant, family):
    assert kb.resolve_robot_unit(selector, 'pipeline_inspection', variant, family) is None


@pytest.mark.parametrize('variant,family', [
    ('观察级深海机器人 75HP', None),
    (None, '观察级深海机器人'),
    ('观察级深海机器人', None),  # The compatibility API also accepts a family here.
    ('观察级深海机器人 75HP', '观察级深海机器人'),
])
def test_matching_parent_still_resolves_exact_unit(kb, variant, family):
    unit = kb.resolve_robot_unit('OBSROV-75-001', 'pipeline_inspection', variant, family)
    assert unit is not None
    assert unit['unit_id'] == 'OBSROV-75-001'
