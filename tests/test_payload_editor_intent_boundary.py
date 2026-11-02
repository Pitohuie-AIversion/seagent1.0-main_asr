"""Only standalone editor commands may bypass the model planner."""
import pytest

from src.dialogue_manager import DialogueManager
from src.handlers.payload_mutation import PayloadMutationManager
from src.knowledge_retriever import KnowledgeBase
from tests.interaction_plan_support import ScriptedLLM, make_plan


@pytest.mark.parametrize('message', [
    '请解释 DVL 定位失锁、浑浊度、海流与水下作业安全的关系，并结合系统中的油田环境、禁入区、载荷和支持船规则说明。只查询知识，不创建或修改任何任务。',
    '修改载荷需要注意什么？',
    '载荷卡片是什么？',
    '请先介绍载荷和修改任务的规则。',
    '不要修改载荷，只回答问题。',
    '支持船改成海洋石油681，载荷保持不变。',
    '把载荷更换成机械扫描声呐。',
])
def test_questions_and_parameter_updates_do_not_open_editor(message):
    assert not PayloadMutationManager.is_payload_modification_request(message)


@pytest.mark.parametrize('message', ['修改载荷', '重新选择载荷', '请帮我修改一下载荷。', '调出载荷卡片', 'modify payload'])
def test_standalone_editor_commands_remain_available(message):
    assert PayloadMutationManager.is_payload_modification_request(message)


def test_read_question_reaches_model_without_creating_payload_slot(monkeypatch):
    llm = ScriptedLLM(default_plan=make_plan('READ', query_intent='KNOWLEDGE_QA'))
    manager = DialogueManager(llm, KnowledgeBase())
    before = manager.slot_store.export_snapshot()
    monkeypatch.setattr(manager, '_handle_non_task_route', lambda *args: '载荷配置受设备兼容性约束。')
    reply = manager.process('请解释载荷规则，只查询知识，不创建或修改任何任务。')
    assert llm.classify_calls
    assert reply == '载荷配置受设备兼容性约束。'
    assert manager.slot_store.export_snapshot() == before
    assert manager.editing_slot is None
