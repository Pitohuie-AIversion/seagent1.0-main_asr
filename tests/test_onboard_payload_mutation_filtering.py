import pytest
from src.knowledge_retriever import KnowledgeBase
from src.slot_store import SlotStore, Slot
from src.output_builder import OutputBuilder


def test_onboard_payloads_skipped_in_mutation_when_robot_selected():
    kb = KnowledgeBase()
    store = SlotStore(kb)
    builder = OutputBuilder(kb)

    schema_field = {"key": "payload", "allowed_values_ref": "payload_options.pipeline_inspection"}
    mutation = {
        "field": "payload",
        "operation": "add",
        "items": [
            "高清水下摄像机,LED 水下照明灯,前视声呐,USBL 定位设备,深度传感器,激光标尺,腐蚀检测探头,厚度检测传感器,泄漏检测传感器,INS 惯性导航系统,DVL 多普勒测速仪,水质传感器"
        ],
        "raw_text": "按照你推荐的携带",
    }

    new_slots = {
        "task_type_key": Slot(slot_name="task_type_key", value="pipeline_inspection", value_type="string", status="valid"),
        "equipment_type": Slot(slot_name="equipment_type", value="观察级深海机器人 75HP", value_type="string", status="valid"),
        "payload": Slot(slot_name="payload", value=[], value_type="list", status="missing"),
    }

    res = store.apply_list_mutation(
        new_slots=new_slots,
        mutation=mutation,
        required_schema=[schema_field],
    )

    assert res.get("success") is True
    val = new_slots["payload"].value
    assert isinstance(val, list)
    # Native onboard items must be filtered out
    assert "高清水下摄像机" not in val
    assert "前视声呐" not in val
    assert "USBL定位设备" not in val
    # Optional supported items must be included
    assert "激光标尺" in val
    assert "腐蚀检测探头" in val
    assert "厚度检测传感器" in val
