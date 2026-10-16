"""Payload labels in full task descriptions must not become tool names."""

from datetime import datetime
from types import SimpleNamespace

import pytest

from src.dialogue_manager import DialogueManager
from src.handlers.payload_mutation import PayloadMutationManager
from src.knowledge_retriever import KnowledgeBase
from src.slots.slot_store import Slot
from tests.interaction_plan_support import ScriptedLLM, extraction_result, make_plan, slot_candidate


FULL_TASK = (
    "我想做管缆巡检，开始时间为2026年10月16日09:00，结束时间为2026年10月16日12:00，"
    "管缆类型为海底油气管道，起始点北纬20.1度东经115.2度，结束点北纬20.3度东经115.5度，"
    "水深300米，使用观察级深海机器人75HP，具体机器人编号为OBSROV-75-001，"
    "携带工具为高清水下摄像机和前视声呐，支持船为海洋石油681。"
)


@pytest.mark.parametrize("label", ["携带工具为", "携带工具是", "携带工具：", "携带工具: "])
def test_full_task_payload_label_is_not_part_of_operand(label):
    manager = PayloadMutationManager(SimpleNamespace(kb=KnowledgeBase()))
    extracted = extraction_result(slot_candidate("payload", ["双目水下成像系统"]))

    manager.normalize_payload_list_mutations(
        extracted, FULL_TASK.replace("携带工具为", label), {},
    )

    assert extracted["slot_candidates"] == []
    assert len(extracted["list_mutations"]) == 1
    assert extracted["list_mutations"][0]["items"] == ["高清水下摄像机", "前视声呐"]


@pytest.mark.parametrize("message", [
    "携带工具和支持船我还没决定，请先保留为空。",
    "携带工具暂时未定，水深改为320米。",
    "载荷尚未选择。",
    "携带的工具我还没有确认。",
])
@pytest.mark.parametrize("existing", [[], ["高清水下摄像机"]])
def test_pending_payload_subject_is_not_an_add_command(message, existing):
    manager = PayloadMutationManager(SimpleNamespace(kb=KnowledgeBase()))
    extracted = extraction_result(slot_candidate("payload", ["双目水下成像系统"]))
    slots = {"payload": Slot("payload", value=existing, status="valid" if existing else "missing")}

    manager.normalize_payload_list_mutations(extracted, message, slots)

    assert extracted["slot_candidates"] == []
    assert extracted["list_mutations"] == []
    assert slots["payload"].value == existing


def test_pending_payload_clause_keeps_independent_explicit_add():
    manager = PayloadMutationManager(SimpleNamespace(kb=KnowledgeBase()))
    extracted = extraction_result()
    manager.normalize_payload_list_mutations(
        extracted, "携带工具暂时未定，但先添加高清水下摄像机。", {},
    )
    assert len(extracted["list_mutations"]) == 1
    assert extracted["list_mutations"][0]["items"] == ["高清水下摄像机"]


@pytest.fixture(params=[(False, False), (True, False), (True, True)], ids=["legacy", "patch-v2", "norm-v2"])
def dialogue(request, monkeypatch):
    patch, norm = request.param
    monkeypatch.setattr("src.dialogue_manager.is_task_patch_v2_enabled", lambda: patch)
    monkeypatch.setattr("src.dialogue_manager.is_normalization_contract_v2_enabled", lambda: norm)
    monkeypatch.setattr("src.extraction.extractor.get_feature_flag", lambda name: False)
    monkeypatch.setattr("src.temporal.simulated_time.get_current_datetime", lambda: datetime(2026, 10, 15, 8))
    llm = ScriptedLLM(default_plan=make_plan("WRITE"), default_reply="参数已接收。")
    return DialogueManager(llm, KnowledgeBase()), llm


@pytest.mark.parametrize("tools,expected", [
    ("高清水下摄像机和前视声呐", ["高清水下摄像机"]),
    ("单目水下成像系统和前视声呐", None),
    ("双目水下成像系统和机械扫描声呐", ["双目水下成像系统", "机械扫描声呐"]),
])
def test_first_turn_payload_uses_selected_robot_validation(dialogue, tools, expected):
    dm, llm = dialogue
    message = FULL_TASK.replace("高清水下摄像机和前视声呐", tools)
    # A fresh dialogue first requests a selector, then the task's full schema.
    llm.queue_extraction(extraction_result(
        slot_candidate("task_type_key", "pipeline_inspection"),
        slot_candidate("task_type", "管缆巡检"),
    ))
    values = {
        "task_type_key": "pipeline_inspection", "task_type": "管缆巡检",
        "equipment_type": "观察级深海机器人 75HP", "equipment_unit_id": "OBSROV-75-001",
        "water_depth": 300, "support_vessel": "海洋石油681",
        "start_time": "2026-10-16T09:00:00", "end_time": "2026-10-16T12:00:00",
        "cable_type": "海底油气管道",
        "start_point": {"lat": 20.1, "lon": 115.2},
        "end_point": {"lat": 20.3, "lon": 115.5},
    }
    raw_times = {"start_time": "2026年10月16日09:00", "end_time": "2026年10月16日12:00"}
    extracted = extraction_result(
        *(slot_candidate(key, value, raw_value=raw_times.get(key, value)) for key, value in values.items()),
        slot_candidate("payload", ["双目水下成像系统", "机械扫描声呐"]),
    )
    # A model may infer a duration from the interval and echo the full message.
    # Robot horsepower in that evidence must not become a duration operand.
    extracted["time_relation"] = {
        "has_duration": True, "raw_text": message, "duration_seconds": 10800,
        "target": "duration", "action": "SET", "keep_existing_duration": False,
        "confidence": 0.99,
    }
    llm.queue_extraction(extracted)

    dm.process(message)

    for key, value in values.items():
        assert dm.task_state[key] == value
    payload = dm.slot_store.slots["payload"]
    if expected is None:
        assert not payload.value
        assert "标配机载设备" in payload.validation_error
        assert "工具为" not in payload.validation_error
    else:
        assert payload.value == expected
        assert payload.status == "valid"
        assert not payload.validation_error


@pytest.mark.parametrize("message", [
    "携带工具为不存在的摄像机和机械扫描声呐。",
    "携带工具为工具为双目水下成像系统和机械扫描声呐。",
])
def test_unknown_tool_with_field_label_keeps_confirmed_payload(dialogue, message):
    dm, llm = dialogue
    dm.slot_store.init_task_slots(dm.builder.get_schema("pipeline_inspection", "normal"))
    for key, value in {
        "task_type_key": "pipeline_inspection", "task_type": "管缆巡检",
        "equipment_type": "观察级深海机器人 75HP", "payload": ["腐蚀检测探头"],
    }.items():
        dm.slot_store.slots[key] = Slot(key, value=value, status="valid")
    dm._rebuild_cache()
    llm.queue_extraction(extraction_result(slot_candidate("payload", ["双目水下成像系统", "机械扫描声呐"])))

    dm.process(message)

    assert dm.task_state["payload"] == ["腐蚀检测探头"]
    assert dm.slot_store.slots["payload"].validation_error


def test_pending_payload_remains_missing_without_invalid_tool_error(dialogue):
    dm, llm = dialogue
    dm.slot_store.init_task_slots(dm.builder.get_schema("pipeline_inspection", "normal"))
    for key, value in {
        "task_type_key": "pipeline_inspection", "task_type": "管缆巡检",
        "equipment_type": "观察级深海机器人 75HP",
    }.items():
        dm.slot_store.slots[key] = Slot(key, value=value, status="valid")
    dm._rebuild_cache()
    llm.queue_extraction(extraction_result(
        slot_candidate("water_depth", 320),
        slot_candidate("payload", ["双目水下成像系统"]),
    ))

    reply = dm.process("水深改为320米，携带工具和支持船我还没决定，请先保留为空。")

    assert dm.task_state["water_depth"] == 320
    assert not dm.task_state.get("payload")
    assert not dm.task_state.get("support_vessel")
    assert dm.slot_store.slots["payload"].status == "missing"
    assert not dm.slot_store.slots["payload"].validation_error
    assert "载荷操作失败" not in reply
