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

    def classify_interaction(self, messages, **kwargs):
        user_text = messages[-1]["content"] if messages else ""
        if "介绍" in user_text or "能力" in user_text or "能干" in user_text:
            return {
                "operation": "READ",
                "dialogue_mode": "knowledge_qa",
                "query_intent": "TASK_CATALOG",
                "subject_type": "task_spec",
                "subject_text": "pipeline_inspection",
                "relation": "info",
                "source_policy": "project_kb",
                "needs_clarification": False,
                "clarification_reason": None,
                "emergency_action": None,
                "confidence": 0.9,
                "reason_code": "task_catalog_query",
            }
        return {
            "operation": "WRITE",
            "dialogue_mode": "task_collection",
            "query_intent": None,
            "subject_type": "task_spec",
            "subject_text": "pipeline_inspection",
            "relation": "create",
            "source_policy": "user_explicit",
            "needs_clarification": False,
            "clarification_reason": None,
            "emergency_action": None,
            "confidence": 0.9,
            "reason_code": "user_task_start",
        }


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


def test_referential_task_initiation_carryover():
    """测试多轮对话中，Turn 1 咨询管缆巡检任务后，Turn 2 输入口语化“那开始这个任务”能 100% 自动继承任务类型并进入收集阶段。"""
    llm = DummyLLM()
    kb = KnowledgeBase()
    dm = DialogueManager(llm=llm, kb=kb)

    r1 = dm.process("介绍一下管缆巡检任务")
    assert "管缆巡检" in r1
    assert dm._last_discussed_task_type == "pipeline_inspection"

    r2 = dm.process("那开始这个任务")
    assert dm.slot_store.get_task_state().get("task_type_key") == "pipeline_inspection"
    assert dm.phase == "collecting"


def test_referential_all_entities_carryover():
    """未选任务时保留指代候选，不能跳过任务 schema 写成有效槽位。"""
    llm = DummyLLM()
    kb = KnowledgeBase()

    # 1. 机器人指代继承
    dm_robot = DialogueManager(llm=llm, kb=kb)
    dm_robot.process("系统支持金牛座机器人吗")
    assert dm_robot._last_discussed_robot is not None
    dm_robot.process("就用这个机器人")
    assert dm_robot._pending_referential_candidates[0]["canonical_key"] == "equipment_family"
    assert not dm_robot.slot_store.get_task_state()

    # 2. 油田海域指代继承
    dm_oil = DialogueManager(llm=llm, kb=kb)
    dm_oil.process("介绍一下流花11-1油田")
    assert dm_oil._last_discussed_oilfield == "流花11-1油田"
    dm_oil.process("就去这个油田")
    assert dm_oil._pending_referential_candidates[0]["normalized_value"] == "流花11-1油田"
    assert not dm_oil.slot_store.get_task_state()

    # 3. 载荷工具指代继承
    dm_payload = DialogueManager(llm=llm, kb=kb)
    dm_payload.process("系统支持水下摄像机载荷吗")
    assert dm_payload._last_discussed_payload == "高清水下摄像机"
    dm_payload.process("就带这个工具")
    assert dm_payload._pending_referential_candidates[0]["canonical_key"] == "payload"
    assert dm_payload._pending_referential_candidates[0]["normalized_value"] == ["高清水下摄像机"]
    assert not dm_payload.slot_store.get_task_state()

