import pytest
from src.dialogue_manager import DialogueManager
from src.knowledge_retriever import KnowledgeBase
from src.intent_router import IntentRouteResult
from src.interaction_plan import InteractionPlan


class DummyLLM:
    def chat(self, messages, **kwargs):
        return "当前支持的机器人包括：观察级深海机器人75HP、工作级深海机器人150HP、水下无人自主航行器 324CC等。"

    def filter_reply(self, reply, **kwargs):
        return reply

    def extract_json(self, messages, **kwargs):
        return {}


@pytest.fixture
def manager():
    llm = DummyLLM()
    kb = KnowledgeBase()
    dm = DialogueManager(llm=llm, kb=kb)
    return dm


def test_system_capability_query(manager):
    """测试询问系统能力（你具备什么能力、你能干什么、你会什么）不会误报物理设备不存在。"""
    queries = ["你具备什么能力", "你能干什么", "你会什么", "系统有什么能力", "自我介绍"]
    for q in queries:
        route = IntentRouteResult(
            interaction_type="QUERY",
            confidence=0.9,
            reason="system_capability",
            query_intent="DEVICE_CAPABILITY",
            interaction_plan=InteractionPlan(
                schema_version=1,
                operation="READ",
                dialogue_mode="knowledge_qa",
                query_intent="DEVICE_CAPABILITY",
                subject_type="system_rule",
                subject_text="SEAgent capabilities",
                relation="capabilities",
                source_policy="project_kb",
                needs_clarification=False,
                clarification_reason=None,
                emergency_action=None,
                confidence=0.9,
                reason_code="system_capability",
            ),
        )
        reply = manager._handle_knowledge_query(q, route, request_id="test_req")
        assert "项目知识库中未找到该设备信息" not in reply
        assert "知识与状态查询" in reply
        assert "任务创建与准入" in reply


def test_broad_device_list_query(manager):
    """测试询问“当前支持的所有机器人”能够正确识别并返回机器人设备列表而非误报未找到。"""
    queries = ["当前支持的所有机器人", "目前支持的所有机器人", "支持的全部机器人"]
    for q in queries:
        route = IntentRouteResult(
            interaction_type="QUERY",
            confidence=0.9,
            reason="device_list",
            query_intent="DEVICE_CAPABILITY",
            interaction_plan=InteractionPlan(
                schema_version=1,
                operation="READ",
                dialogue_mode="knowledge_qa",
                query_intent="DEVICE_CAPABILITY",
                subject_type="device_family",
                subject_text=q,
                relation="list",
                source_policy="project_kb",
                needs_clarification=False,
                clarification_reason=None,
                emergency_action=None,
                confidence=0.9,
                reason_code="device_list",
            ),
        )
        reply = manager._handle_knowledge_query(q, route, request_id="test_req")
        print(f"\n[DEBUG_TEST] query={q!r} -> reply={reply!r}")
        assert "项目知识库中未找到该设备信息" not in reply
        assert ("观察级" in reply or "工作级" in reply or "机器人" in reply)
