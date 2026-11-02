import pytest
from src.knowledge_retriever import KnowledgeBase
from src.slots.slot_store import SlotStore, Slot
from src.dispatch.output_builder import OutputBuilder


def test_onboard_payloads_skipped_in_mutation_when_robot_selected():
    kb = KnowledgeBase()
    store = SlotStore(kb)
    builder = OutputBuilder(kb)

    schema_field = {"key": "payload", "allowed_values_ref": "payload_options.pipeline_inspection"}
    mutation = {
        "field": "payload",
        "operation": "add",
        "items": [
            "单目水下成像系统,高清水下摄像机,LED 水下照明灯,前视声呐,USBL 定位设备,深度传感器,激光标尺,腐蚀检测探头,厚度检测传感器,泄漏检测传感器,INS 惯性导航系统,DVL 多普勒测速仪,水质传感器"
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
    assert "单目水下成像系统" not in val
    assert "前视声呐" not in val
    assert "USBL定位设备" not in val
    # Optional supported items must be included
    assert "高清水下摄像机" in val
    assert "激光标尺" in val
    assert "腐蚀检测探头" in val
    assert "厚度检测传感器" in val


@pytest.mark.parametrize("operation", ["add", "set", "override"])
@pytest.mark.parametrize("existing", [[], ["浑水水下成像系统"]])
def test_onboard_payload_only_mutation_fails_with_explanation(operation, existing):
    kb = KnowledgeBase()
    store = SlotStore(kb)

    schema_field = {"key": "payload", "allowed_values_ref": "payload_options.pipeline_inspection"}
    mutation = {
        "field": "payload",
        "operation": operation,
        "items": ["高清水下摄像机"],
        "raw_text": "载荷选高清水下摄像机",
    }

    new_slots = {
        "task_type_key": Slot(slot_name="task_type_key", value="pipeline_inspection", value_type="string", status="valid"),
        "equipment_type": Slot(slot_name="equipment_type", value="轻型工作级深海机器人 150HP", value_type="string", status="valid"),
        "payload": Slot(slot_name="payload", value=existing.copy(), value_type="list", status="valid" if existing else "missing"),
    }

    res = store.apply_list_mutation(
        new_slots=new_slots,
        mutation=mutation,
        required_schema=[schema_field],
    )

    assert res.get("success") is False
    assert "标配机载设备" in str(res.get("error"))
    # Payload should remain untouched and not corrupted
    assert new_slots["payload"].value == existing
    assert new_slots["payload"].status == ("valid" if existing else "missing")


@pytest.mark.parametrize("items", [["高清水下摄像机"], ["云台摄像机"], []])
def test_empty_effective_replacement_preserves_confirmed_payload(items):
    store = SlotStore(KnowledgeBase())
    original = ["浑水水下成像系统", "激光标尺"]
    slots = {
        "task_type_key": Slot("task_type_key", value="pipeline_inspection", status="valid"),
        "equipment_type": Slot("equipment_type", value="轻型工作级深海机器人 150HP", status="valid"),
        "payload": Slot("payload", value=original.copy(), value_type="list", status="valid"),
    }
    result = store.apply_list_mutation(
        slots,
        {"field": "payload", "operation": "replace", "items": items,
         "target_items": ["浑水水下成像系统"], "raw_text": "更换成机载摄像机"},
        required_schema=[{"key": "payload", "allowed_values_ref": "payload_options.pipeline_inspection"}],
    )
    assert result["success"] is False
    assert result["changed"] is False
    assert result["new_value"] == original
    assert slots["payload"].value == original
    assert slots["payload"].status == "valid"
    assert "标配" in result["error"] if items else "新载荷" in result["error"]
