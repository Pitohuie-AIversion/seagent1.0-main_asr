"""Model statements must agree with committed scalar and coordinate slots."""

import pytest

from src.handlers.write_reply_grounder import WriteReplyGrounder


def ground(prose, *, state=None, updates=None):
    dummy = {} if (state and "support_vessel" in state) else {"support_vessel": "海洋石油681"}
    return WriteReplyGrounder.ground_write_reply(
        prose,
        accepted_updates=updates if updates is not None else dummy,
        task_state=state or {},
        unresolved_inputs=[],
    )


FIELD_CASES = [
    ("task_type", "管缆巡检", "任务类型已设置为管缆埋设", "管缆埋设"),
    ("cable_type", "电力电缆", "管缆类型已设置为光纤通信缆", "光纤通信缆"),
    ("cable_type", "电力电缆", "当前电缆类型为光纤通信缆", "光纤通信缆"),
    ("start_point", {"lat": 20, "lon": 115}, "当前起始点经纬度为北纬21度，东经114度", "北纬21度"),
    ("end_point", {"lat": 20.1, "lon": 115.1}, "已将终点设置为(21.2, 114.2)", "114.2"),
    ("oilfield_coordinates", {"lat": 20, "lon": 115}, "油田坐标已记录为北纬21度，东经114度", "北纬21度"),
    ("equipment_type", "轻型工作级深海机器人 150HP", "已将设备型号设置为通用工作级深海机器人 250HP", "250HP"),
    ("equipment_family", "轻型工作级深海机器人", "当前机器人系列为通用工作级深海机器人", "通用工作级"),
    ("equipment_unit_id", "LROV-150-001", "机器人编号已设置为WROV-250-001", "WROV-250-001"),
    ("equipment_name", "天鹰座一号机", "当前设备全称为不存在号", "不存在号"),
    ("wellhead_id", "WH-01", "井口编号已设置为WH-02", "WH-02"),
    ("oilfield_name", "流花油田", "已将油田名称设置为番禺油田", "番禺油田"),
    ("support_vessel", "海洋石油681", "已记录支持船：海洋石油286", "海洋石油286"),
]


@pytest.mark.parametrize("key,saved,claim,wrong", FIELD_CASES)
@pytest.mark.parametrize("source", ["missing", "retained", "updated"])
def test_unsupported_field_assertions_are_removed_without_losing_other_guidance(key, saved, claim, wrong, source):
    state = {key: saved} if source == "retained" else {}
    updates = {key: saved} if source == "updated" else None
    result = ground(claim + "，请提供作业参数。", state=state, updates=updates)
    assert wrong not in result
    assert "请提供作业参数。" in result
    if source == "retained":
        assert "当前已保存：" in result
    assert state == ({key: saved} if source == "retained" else {})


@pytest.mark.parametrize("prose", [
    "已记录水深5000米。",
    "当前水深为5000米。",
    "水深：5000米。",
    "已将水深设置为5千米。",
    "水深已记录为三百米。",
    "当前水深为零点五千米。",
])
def test_missing_depth_cannot_be_claimed_as_a_saved_value(prose):
    result = ground(prose + "请提供起点。")
    assert "5000" not in result and "5千米" not in result
    assert "请提供起点。" in result
    assert prose not in result


@pytest.mark.parametrize("prose,wrong", [
    ("水深已记录为三百米。", "三百"),
    ("当前水深为五千米。", "五千"),
    ("已将水深设置为零点五千米。", "零点五"),
])
def test_chinese_depth_claims_are_corrected_to_the_saved_depth(prose, wrong):
    result = ground(prose, state={"water_depth": 100})
    assert wrong not in result
    assert "100 米" in result


@pytest.mark.parametrize("prose", ["水深已记录为三百米。", "当前水深为零点三千米。"])
def test_equivalent_chinese_depth_values_keep_the_original_prose(prose):
    assert prose in ground(prose, state={"water_depth": 300})


@pytest.mark.parametrize("prose", [
    "请确认管缆类型为光纤通信缆。",
    "管缆类型：建议选择光纤通信缆。",
    "管缆类型：尚未选择。",
    "管缆类型为光纤通信缆吗？",
    "是否已将设备型号设置为通用工作级深海机器人250HP？",
    "推荐设备型号为通用工作级深海机器人250HP。",
    "例如，起始点经纬度为北纬21度，东经114度。",
    "如果起点为北纬21度，东经114度，应重新规划路线。",
    "请提供起点坐标：北纬21度，东经114度这样的格式即可。",
    "当前水深是5000米吗？",
    "尚未记录水深5000米。",
    "管缆类型为光纤通信缆时，需要核对检测工具。",
    "设备型号为轻型工作级深海机器人150HP的话，请核对载荷。",
])
def test_questions_pending_values_recommendations_and_examples_are_preserved(prose):
    assert prose in ground(prose)


@pytest.mark.parametrize("key,value,prose", [
    ("cable_type", "电力电缆", "当前管缆类型为电力电缆。"),
    ("cable_type", "电力电缆", "已记录电缆类型为电力。"),
    ("cable_type", "电力电缆", "管缆类型已设置为电力电缆（原光纤通信缆）。"),
    ("start_point", {"lat": 20, "lon": 115}, "当前起点为北纬20.0度，东经115.0度。"),
    ("start_point", {"lat": 20, "lon": 115}, "已记录起始点坐标为(20, 115)。"),
    ("end_point", {"lat": 20.1, "lon": 115.1}, "终点坐标已记录为东经115.1度北纬20.1度。"),
    ("equipment_type", "轻型工作级深海机器人 150HP", "设备型号已设置为轻型工作级深海机器人150HP。"),
    ("equipment_unit_id", "LROV-150-001", "当前机器人编号为LROV-150-001。"),
])
@pytest.mark.parametrize("updated", [False, True])
def test_equivalent_saved_values_and_before_after_explanations_survive(key, value, prose, updated):
    assert prose in ground(prose, state={} if updated else {key: value}, updates={key: value} if updated else None)


@pytest.mark.parametrize("prose", [
    "**当前管缆类型**：**光纤通信缆**",
    "1. **管缆类型**：**光纤通信缆**",
    "当前**管缆类型**：**光纤通信缆**。",
    "请提供起点，当前管缆类型为光纤通信缆。",
])
def test_false_markdown_and_trailing_claims_do_not_leave_formatting_or_punctuation(prose):
    result = ground(prose, state={"cable_type": "电力电缆"})
    assert "光纤通信缆" not in result
    assert "**" not in result and "，。" not in result
    assert not result.startswith("1.")
    if "请提供起点" in prose:
        assert "请提供起点。" in result


def test_multiple_field_assertions_are_independently_checked_in_one_sentence():
    result = ground(
        "当前管缆类型为光纤通信缆，起点为北纬20度，东经115度，请提供终点。",
        state={"cable_type": "电力电缆", "start_point": {"lat": 20, "lon": 115}},
    )
    assert "光纤通信缆" not in result
    assert "起点为北纬20度，东经115度" in result
    assert "请提供终点。" in result


def test_robot_identifiers_must_match_the_complete_saved_identifier():
    result = ground("机器人编号已设置为LROV-150-00。", state={"equipment_unit_id": "LROV-150-001"})
    assert "已设置为LROV-150-00" not in result
    assert "具体机器人编号：LROV-150-001" in result


@pytest.mark.parametrize("model,claim,keep", [
    ("轻型工作级深海机器人150HP", "设备型号为50HP。", False),
    ("轻型工作级深海机器人150HP", "设备型号为150HP。", True),
    ("通用工作级深海机器人250HP", "设备型号为50HP。", False),
    ("通用工作级深海机器人1500HP", "设备型号为150。", False),
])
def test_short_robot_model_values_respect_numeric_boundaries(model, claim, keep):
    result = ground(claim, state={"equipment_type": model})
    assert (claim in result) is keep


def test_notice_or_later_example_does_not_authorize_an_unsupported_field_claim():
    result = ground("请注意：管缆类型已设置为光纤通信缆，例如下一步可以提供起点。",
                    state={"cable_type": "电力电缆"})
    assert "管缆类型已设置为光纤通信缆" not in result
    assert "例如下一步可以提供起点。" in result


@pytest.fixture(params=[(False, False), (True, False), (True, True)])
def dialogue(request, monkeypatch):
    from src.dialogue_manager import DialogueManager
    from src.knowledge_retriever import KnowledgeBase
    from src.slots.slot_store import Slot
    from tests.interaction_plan_support import ScriptedLLM, make_plan

    patch, norm = request.param
    monkeypatch.setattr("src.dialogue_manager.is_task_patch_v2_enabled", lambda: patch)
    monkeypatch.setattr("src.dialogue_manager.is_normalization_contract_v2_enabled", lambda: norm)
    llm = ScriptedLLM(default_plan=make_plan("WRITE"))
    dm = DialogueManager(llm, KnowledgeBase())
    dm.slot_store.init_task_slots(dm.builder.get_schema("pipeline_inspection", "normal"))
    for key, value in {"task_type_key": "pipeline_inspection", "task_type": "管缆巡检",
                       "cable_type": "电力电缆", "start_point": {"lat": 20, "lon": 115}}.items():
        dm.slot_store.slots[key] = Slot(key, value=value, status="valid")
    dm._rebuild_cache()
    dm.phase = "collecting"
    return dm, llm


def test_dialogue_reply_cannot_claim_unrelated_cable_coordinate_or_missing_depth_writes(dialogue):
    from tests.interaction_plan_support import extraction_result, slot_candidate

    dm, llm = dialogue
    llm.default_reply = (
        "管缆类型已设置为光纤通信缆。当前起始点经纬度为北纬21度，东经114度。"
        "已记录水深5000米。请提供机器人型号。"
    )
    llm.queue_extraction(extraction_result(slot_candidate("cable_type", "电力电缆")))
    reply = dm.process("管缆类型继续使用电力电缆。")
    assert dm.task_state["cable_type"] == "电力电缆"
    assert dm.task_state["start_point"] == {"lat": 20, "lon": 115}
    assert not dm.task_state.get("water_depth")
    assert "光纤通信缆" not in reply and "北纬21度" not in reply and "5000" not in reply
    assert "请提供机器人型号" in reply


def test_dialogue_rejected_coordinate_does_not_become_a_success_claim(dialogue):
    from tests.interaction_plan_support import extraction_result, slot_candidate

    dm, llm = dialogue
    before = dm.slot_store.slots["start_point"].to_dict()
    llm.default_reply = "起点已设置为北纬91度，东经115度。请提供机器人型号。"
    llm.queue_extraction(extraction_result(slot_candidate("start_point", {"lat": 91, "lon": 115})))
    reply = dm.process("起点改为北纬91度，东经115度。")
    assert dm.slot_store.slots["start_point"].to_dict() == before
    assert dm.task_state["start_point"] == {"lat": 20, "lon": 115}
    assert "起点已设置为北纬91度" not in reply
    assert any(reason in reply for reason in ("无法", "无效", "非法", "超出", "不合法"))
    assert "请提供机器人型号" in reply


def test_dialogue_negated_coordinate_edit_preserves_saved_fact_and_scrubs_false_success(dialogue):
    from tests.interaction_plan_support import extraction_result, slot_candidate

    dm, llm = dialogue
    before = dm.slot_store.slots["start_point"].to_dict()
    llm.default_reply = "起点已设置为北纬21度，东经114度。请提供机器人型号。"
    llm.queue_extraction(extraction_result(slot_candidate("start_point", {"lat": 21, "lon": 114})))
    reply = dm.process("不要把起点改为北纬21度东经114度。")
    assert dm.slot_store.slots["start_point"].to_dict() == before
    assert dm.task_state["start_point"] == {"lat": 20, "lon": 115}
    assert "起点已设置为北纬21度" not in reply
    assert "北纬 20 度，东经 115 度" in reply
    assert "请提供机器人型号" in reply
