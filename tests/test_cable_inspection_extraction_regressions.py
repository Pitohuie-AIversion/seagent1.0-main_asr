"""Regressions for pipeline inspection extraction, explicit grounding, and payload synergy."""
import pytest
from src.dialogue_manager import DialogueManager
from src.knowledge_retriever import KnowledgeBase
from src.slots.slot_store import Slot
from src.handlers.explicit_value_grounding import ground_explicit_values
from src.extraction.extractor import ParameterExtractor
from tests.interaction_plan_support import ScriptedLLM, make_plan, slot_candidate


@pytest.fixture(params=[(False, False), (True, False), (True, True)], ids=["legacy", "patch-v2", "norm-v2"])
def pipeline_dialogue(request, monkeypatch):
    patch, norm = request.param
    monkeypatch.setattr("src.dialogue_manager.is_task_patch_v2_enabled", lambda: patch)
    monkeypatch.setattr("src.dialogue_manager.is_normalization_contract_v2_enabled", lambda: norm)
    llm = ScriptedLLM(default_plan=make_plan("WRITE"), default_reply="已更新任务配置。")
    dm = DialogueManager(llm, KnowledgeBase())
    dm.slot_store.init_task_slots(dm.builder.get_schema("pipeline_inspection", "normal"))
    unit = dm.kb.resolve_robot_unit("LROV-150-001", "pipeline_inspection")
    seed = {
        "task_type_key": "pipeline_inspection",
        "task_type": "管缆巡检",
        "water_depth": 300,
        "start_point": {"lat": 20.1, "lon": 115.2},
        "end_point": {"lat": 20.3, "lon": 115.5},
        "equipment_family": unit["robot"]["family_full_name"],
        "equipment_type": unit["robot"]["full_name"],
        "equipment_unit_id": unit["unit_id"],
        "equipment_name": unit["display_name"],
    }
    for key, value in seed.items():
        dm.slot_store.slots[key] = Slot(key, value=value, status="valid")
    dm._rebuild_cache()
    dm.phase = "collecting"
    return dm, llm


def test_cable_type_deterministic_grounding():
    """验证即使用户在复合句中提出管缆类型，模型漏抽时 ground_explicit_values 亦能精确补齐。"""
    kb = KnowledgeBase()
    fields = [
        {"key": "cable_type", "type": "string", "allowed_values": ["海底油气管道", "电力电缆", "光纤通信缆"]},
        {"key": "water_depth", "type": "number"},
    ]

    # 1. 复合句包含海底油气管道，模型原本漏抽
    extraction = {"slot_candidates": [slot_candidate("water_depth", 300)]}
    msg = "那帮我安排海底油气管道巡检吧，明天上午九点开始，预计两个小时。水深大概300米。"
    res = ground_explicit_values(extraction, msg, kb=kb, fields=fields, current_state={}, task_type="pipeline_inspection")
    cands = {c["canonical_key"]: c["normalized_value"] for c in res["slot_candidates"]}
    assert cands.get("cable_type") == "海底油气管道"
    assert cands.get("water_depth") == 300

    # 2. 电力电缆口语海缆
    extraction2 = {"slot_candidates": []}
    msg2 = "安排海缆巡检任务，起点东经115度北纬20度。"
    res2 = ground_explicit_values(extraction2, msg2, kb=kb, fields=fields, current_state={}, task_type="pipeline_inspection")
    cands2 = {c["canonical_key"]: c["normalized_value"] for c in res2["slot_candidates"]}
    assert cands2.get("cable_type") == "电力电缆"

    # 3. 光纤通信缆
    extraction3 = {"slot_candidates": []}
    msg3 = "执行光纤通信缆巡检作业。"
    res3 = ground_explicit_values(extraction3, msg3, kb=kb, fields=fields, current_state={}, task_type="pipeline_inspection")
    cands3 = {c["canonical_key"]: c["normalized_value"] for c in res3["slot_candidates"]}
    assert cands3.get("cable_type") == "光纤通信缆"

    # 4. 否定场景严禁误抽
    extraction4 = {"slot_candidates": []}
    msg4 = "不要安排海底油气管道巡检，换成别的。"
    res4 = ground_explicit_values(extraction4, msg4, kb=kb, fields=fields, current_state={}, task_type="pipeline_inspection")
    cands4 = {c["canonical_key"]: c["normalized_value"] for c in res4["slot_candidates"]}
    assert "cable_type" not in cands4


def test_compound_robot_vessel_payload_turn(pipeline_dialogue):
    """
    验证类似 test_cable_prompt.py 的复合多槽位更新：
    '机器人就用 LROV-150-001，支持船选择海洋石油681，工具带侧扫声呐系统和腐蚀检测探头。'
    设备单机、支持船与载荷变异同时生效，且设备级联不会覆盖新指定的载荷。
    """
    dm, llm = pipeline_dialogue
    msg = "机器人就用 LROV-150-001，支持船选择海洋石油681，工具带侧扫声呐系统和腐蚀检测探头。"
    
    # 模拟大模型提取结果
    llm.queue_extraction({
        "slot_candidates": [
            slot_candidate("equipment_unit_id", "LROV-150-001"),
            slot_candidate("support_vessel", "海洋石油681"),
        ],
        "list_mutations": [{
            "field": "payload",
            "operation": "set",
            "items": ["侧扫声呐系统", "腐蚀检测探头"],
            "target_items": [],
            "raw_text": "工具带侧扫声呐系统和腐蚀检测探头",
            "confidence": 0.95,
            "source": "user_input",
        }],
        "time_relation": None,
        "unresolved": [],
    })

    dm.process(msg)

    assert dm.task_state.get("equipment_unit_id") == "LROV-150-001"
    assert dm.task_state.get("support_vessel") == "海洋石油681"
    assert dm.task_state.get("payload") == ["侧扫声呐系统", "腐蚀检测探头"]
    assert dm.slot_store.slots["payload"].status == "valid"
    assert dm.slot_store.slots["equipment_unit_id"].status == "valid"
    assert dm.slot_store.slots["support_vessel"].status == "valid"


def test_meta_unresolved_filtering_coverage():
    """验证模型在 unresolved 中输出思考元推理时被干净剔除，不污染业务状态。"""
    raw_unresolved = [
        "用户指令为确认发布任务，未提供新的参数修改或补充，本轮无字段更新。",
        "本轮输入为确认，slot_candidates 应为空",
        "根据规则第三条，无需更新字段",
        "水深超过最大作业水深600米",  # 真实业务异常，必须保留
    ]
    cleaned = ParameterExtractor._filter_meta_unresolved(raw_unresolved)
    assert cleaned == ["水深超过最大作业水深600米"]
