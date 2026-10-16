"""Ground onboard/optional explanations in the selected robot's catalogue."""

from datetime import datetime

import pytest

from src.dialogue_manager import DialogueManager
from src.handlers.write_reply_grounder import WriteReplyGrounder
from src.knowledge_retriever import KnowledgeBase
from src.slots.slot_store import Slot
from tests.interaction_plan_support import ScriptedLLM, extraction_result, make_plan, slot_candidate


REAL_FALSE_EXPLANATION = (
    "您指定的“高清水下摄像机”已确认。经核查，该设备为所选机器人"
    "“观察级深海机器人 75HP-001”的标配自带载荷（含单目水下成像系统、云台摄像机等），"
    "无需额外挂载，配置有效。"
)


@pytest.fixture(scope="module")
def robot():
    return KnowledgeBase()._find_rov("观察级深海机器人 75HP")


def ground(reply, robot):
    return WriteReplyGrounder.ground_write_reply(
        reply,
        accepted_updates={"payload": ["高清水下摄像机"]},
        unresolved_inputs=[],
        task_state={"payload": ["高清水下摄像机"]},
        payload_configuration=robot,
    )


@pytest.mark.parametrize("prose", [
    REAL_FALSE_EXPLANATION,
    "高清水下摄像机属于标配自带载荷，无需额外挂载。",
    "双目水下成像系统是机器人自带载荷。",
])
def test_optional_payload_cannot_be_described_as_onboard(prose, robot):
    result = ground(prose + "\n请核对支持船编号。", robot)
    assert "标配自带载荷" not in result
    assert "无需额外挂载" not in result
    assert "是机器人自带载荷" not in result
    assert "属于选配载荷" in result
    assert "请核对支持船编号。" in result


def test_onboard_payload_cannot_be_described_as_optional(robot):
    result = ground("前视声呐系统属于选配载荷，需要额外挂载。", robot)
    assert "需要额外挂载" not in result
    assert "前视声呐系统属于机载设备" in result


@pytest.mark.parametrize("prose", [
    "前视声呐系统是机器人自带载荷，无需额外挂载。",
    "高清水下摄像机属于选配载荷。",
    "高清水下摄像机不是标配，需要另行选配。",
    "高清水下摄像机并非机载设备。",
    "高清水下摄像机是标配自带设备吗？",
    "请确认高清水下摄像机是否属于自带载荷。",
    "建议选择高清水下摄像机作为选配载荷。",
    "高清水下摄像机与机载相机的区别需要参考配置说明。",
    "未知工具是机器人自带载荷。",
])
def test_catalogue_guard_preserves_correct_guidance_and_unclassified_prose(prose, robot):
    assert prose in ground(prose, robot)


@pytest.mark.parametrize("with_update", [False, True])
def test_dialogue_write_receives_selected_equipment_catalogue(monkeypatch, with_update):
    monkeypatch.setattr("src.temporal.simulated_time.get_current_datetime", lambda: datetime(2026, 10, 15, 8))
    llm = ScriptedLLM(default_plan=make_plan("WRITE"), default_reply=REAL_FALSE_EXPLANATION)
    dm = DialogueManager(llm, KnowledgeBase())
    dm.slot_store.init_task_slots(dm.builder.get_schema("pipeline_inspection", "normal"))
    for key, value in {
        "task_type_key": "pipeline_inspection", "task_type": "管缆巡检",
        "equipment_type": "观察级深海机器人 75HP", "payload": ["高清水下摄像机"],
    }.items():
        dm.slot_store.slots[key] = Slot(key, value=value, status="valid")
    dm._rebuild_cache()
    llm.queue_extraction(extraction_result(*(
        [slot_candidate("water_depth", 300)] if with_update else []
    )))
    result = dm.process("水深改为300米。" if with_update else "保留现有携带工具。")
    assert dm.task_state["payload"] == ["高清水下摄像机"]
    assert "标配自带载荷" not in result
    assert "属于选配载荷" in result
