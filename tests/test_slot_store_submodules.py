"""
tests/test_slot_store_submodules.py - 槽位存储子模块（列表变异引擎与快照编解码器）专有单元测试
"""

import copy
import pytest

from src.slots.slot_store import (
    Slot,
    SlotStore,
    SnapshotValidationError,
    SlotVersionConflict,
    ValidationAcknowledgement,
    normalize_payload_match_key,
    normalize_slot_value_type,
)
from src.slots.slot_list_mutation import SlotListMutationEngine
from src.slots.slot_snapshot_codec import SlotSnapshotCodec


class TestSlotStoreSubmodules:
    """验证 SlotListMutationEngine 与 SlotSnapshotCodec 的隔离性与契约行为"""

    def test_list_mutation_engine_add_remove_clear(self):
        store = SlotStore()
        engine = SlotListMutationEngine(store)

        slots = {"payload": Slot("payload", value=[], value_type="list", status="valid")}

        # 1. Add
        mutation_add = {
            "field": "payload",
            "operation": "add",
            "items": ["单目水下成像系统", "深度传感器"],
        }
        res_add = engine.apply_list_mutation(slots, mutation_add)
        assert res_add["success"] is True
        assert res_add["changed"] is True
        assert "单目水下成像系统" in res_add["new_value"]
        assert "深度传感器" in res_add["new_value"]

        # 2. Remove
        mutation_remove = {
            "field": "payload",
            "operation": "remove",
            "items": ["单目水下成像系统"],
        }
        res_remove = engine.apply_list_mutation(slots, mutation_remove)
        assert res_remove["success"] is True
        assert "单目水下成像系统" not in res_remove["new_value"]
        assert "深度传感器" in res_remove["new_value"]

        # 3. Clear
        mutation_clear = {
            "field": "payload",
            "operation": "clear",
        }
        res_clear = engine.apply_list_mutation(slots, mutation_clear)
        assert res_clear["success"] is True
        assert res_clear["new_value"] == []

    def test_list_mutation_engine_onboard_protection(self):
        class MockKB:
            assets = {}
            def get_rov(self, eq_type):
                return {
                    "onboard_payloads": ["高清水下摄像机", "高度计"],
                }

        store = SlotStore(kb=MockKB())
        engine = SlotListMutationEngine(store)

        slots = {
            "equipment_type": Slot("equipment_type", value="ROV_DEMO", status="valid"),
            "payload": Slot("payload", value=[], value_type="list", status="valid"),
        }

        # 添加已被包含为机载默认工具的载荷，应被智能忽略而不报错
        mutation = {
            "field": "payload",
            "operation": "add",
            "items": ["高清水下摄像机", "云台摄像机"],  # 属于机载摄像或其近义词
        }
        res = engine.apply_list_mutation(slots, mutation)
        assert res["success"] is True
        # 机载工具不计入外挂载荷
        assert "高清水下摄像机" not in res["new_value"]

    def test_list_mutation_engine_unknown_operation_fails(self):
        store = SlotStore()
        engine = SlotListMutationEngine(store)

        slots = {"payload": Slot("payload", value=["设备A"], value_type="list", status="valid")}
        mutation = {
            "field": "payload",
            "operation": "invalid_op_xyz",
        }
        res = engine.apply_list_mutation(slots, mutation)
        assert res["success"] is False
        assert "不支持" in res["error"]

    def test_snapshot_codec_roundtrip(self):
        store = SlotStore()
        store.slots["task_type_key"] = Slot("task_type_key", value="pipeline_inspection", status="valid")
        store.slots["water_depth"] = Slot("water_depth", value=180.0, value_type="number", status="valid")
        store.unresolved = ["未决测试条目1"]
        store.version = 5

        codec = SlotSnapshotCodec(store)
        snapshot = codec.export_snapshot()

        assert snapshot["snapshot_schema_version"] == 2
        assert snapshot["store_version"] == 5
        assert "pipeline_inspection" == snapshot["slots"]["task_type_key"]["value"]
        assert snapshot["slots"]["water_depth"]["value"] == 180.0
        assert snapshot["unresolved"] == ["未决测试条目1"]

        # 创建新 store 并恢复该快照
        new_store = SlotStore()
        new_codec = SlotSnapshotCodec(new_store)
        new_codec.restore_snapshot(snapshot)

        assert new_store.version == 5
        assert new_store.slots["task_type_key"].value == "pipeline_inspection"
        assert new_store.slots["water_depth"].value == 180.0
        assert new_store.unresolved == ["未决测试条目1"]

    def test_snapshot_codec_rejects_corrupted_snapshot(self):
        store = SlotStore()
        codec = SlotSnapshotCodec(store)

        # 1. 非字典
        with pytest.raises(SnapshotValidationError, match="Snapshot must be a dictionary"):
            codec.restore_snapshot("not_a_dict")

        # 2. 不支持的快照版本
        with pytest.raises(SnapshotValidationError, match="Unsupported snapshot_schema_version"):
            codec.restore_snapshot({"snapshot_schema_version": 999, "slots": {}})

        # 3. 槽位类型错误（如非 dict）
        with pytest.raises(SnapshotValidationError, match="slots must be a dictionary"):
            codec.restore_snapshot({"snapshot_schema_version": 2, "slots": "invalid"})

    def test_slot_store_delegates_smoothly(self):
        store = SlotStore()
        assert hasattr(store, "_list_mutation_engine")
        assert hasattr(store, "_snapshot_codec")

        # 验证原有调用方式完全保持兼容
        slots = {"payload": Slot("payload", value=[], value_type="list", status="valid")}
        res = store.apply_list_mutation(slots, {"field": "payload", "operation": "add", "items": ["测试工具A"]})
        assert res["success"] is True

        snap = store.export_snapshot()
        assert isinstance(snap, dict)
        assert snap.get("snapshot_schema_version") == 2
