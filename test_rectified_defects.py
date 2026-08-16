import pytest
from src.dialogue_manager import DialogueManager
from src.knowledge_retriever import KnowledgeBase
from src.normalizer import FieldNormalizer

@pytest.fixture
def dm():
    kb = KnowledgeBase()
    return DialogueManager(kb=kb)

def test_no_negative_coordinate_hallucination(dm):
    """缺陷 1: 消除负数坐标幻觉，不产生非洲大西洋负数坐标与 C028 假警告"""
    reply = dm.process("安排通用工作级001在流花11-1执行采油树插入，开始时间2026-08-20 15:00，结束时间2026-08-20 09:00")
    coord_slot = dm.slot_store.slots.get("oilfield_coordinates")
    if coord_slot and coord_slot.value:
        assert coord_slot.value == {"lat": 20.815, "lon": 115.735}
    assert "(-20, 15)" not in reply
    assert "C028" not in reply

def test_no_self_class_conflict(dm):
    """缺陷 2: 消除同类别误报自身冲突"""
    reply = dm.process("安排观察级001明天早上8点去陵水17-2的最深处1550米进行海底管缆埋设作业")
    assert "conflicts with active valid class '管缆埋设机器人'" not in reply

def test_post_publish_guidance(dm):
    """缺陷 4: 已发布任务原地修改返回明确友好指引"""
    dm.phase = "done"
    dm.task_state["intent_id"] = "intent_test_123"
    reply = dm.process("把水深改成200米")
    assert "已正式确认发布" in reply and "无法就地修改参数" in reply

def test_burial_sonar_payload():
    """缺陷 5: 管缆埋设包含声呐与喷冲模块时，载荷规范化包含前视声呐"""
    kb = KnowledgeBase()
    allowed_payloads = kb.assets.get("payload_options", {}).get("pipeline_burial", {}).get("common", [])
    fn = FieldNormalizer()
    res = fn.normalize(["高压水射流喷冲埋设模块", "前视声呐"], allowed_payloads, "list")
    assert res is not None
    assert "高压水射流喷冲埋设模块" in res
    assert "前视声呐" in res
