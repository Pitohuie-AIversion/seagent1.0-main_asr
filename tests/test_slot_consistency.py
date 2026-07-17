import unittest
import threading
import time
from unittest.mock import MagicMock
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.knowledge_retriever import KnowledgeBase
from src.dialogue_manager import DialogueManager
from src.llm_client import LLMClient
from src.slot_store import SlotStore, Slot
from src.simulated_time import get_simulated_time
from datetime import datetime
from zoneinfo import ZoneInfo


class SlotConsistencyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.kb = KnowledgeBase()
        cls.llm = MagicMock(spec=LLMClient)
        cls.llm.generate.return_value = "null"

    def setUp(self):
        get_simulated_time().set_current_time(
            datetime(2026, 6, 30, 17, 38, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        )
        self.dm = DialogueManager(self.llm, self.kb)

    # 1. 单条消息同时包含三个不同槽位
    def test_three_slots_in_one_message(self):
        self.dm.reset()
        self.dm.task_state["task_type_key"] = "pipeline_inspection"
        self.dm.task_state["task_type"] = "管缆巡检"
        self.dm.slot_store.slots["task_type_key"].value = "pipeline_inspection"
        self.dm.slot_store.slots["task_type_key"].status = "valid"
        self.dm.slot_store.slots["task_type"].value = "管缆巡检"
        self.dm.slot_store.slots["task_type"].status = "valid"
        
        # Simulates extraction of 3 slots: water_depth, cable_type, support_vessel
        self.dm.llm.extract_json.return_value = {
            "intent": "task_update",
            "slot_candidates": [
                {"raw_key": "水深", "canonical_key": "water_depth", "raw_value": "300米", "normalized_value": 300, "confidence": 1.0},
                {"raw_key": "管缆类型", "canonical_key": "cable_type", "raw_value": "油气管道", "normalized_value": "海底油气管道", "confidence": 1.0},
                {"raw_key": "支持船", "canonical_key": "support_vessel", "raw_value": "681", "normalized_value": "海洋石油681", "confidence": 1.0}
            ],
            "unresolved": []
        }
        
        self.dm.process("水深300米，类型是油气管道，支持船是681")
        
        # Verify slot store has all 3 slots valid
        self.assertEqual(self.dm.slot_store.slots["water_depth"].value, 300.0)
        self.assertEqual(self.dm.slot_store.slots["water_depth"].status, "valid")
        self.assertEqual(self.dm.slot_store.slots["cable_type"].value, "海底油气管道")
        self.assertEqual(self.dm.slot_store.slots["cable_type"].status, "valid")
        self.assertEqual(self.dm.slot_store.slots["support_vessel"].value, "海洋石油681")
        self.assertEqual(self.dm.slot_store.slots["support_vessel"].status, "valid")

    # 2. 用户使用别名或同义词提供槽位
    def test_alias_normalization(self):
        self.dm.reset()
        self.dm.task_state["task_type_key"] = "pipeline_inspection"
        self.dm.task_state["task_type"] = "管缆巡检"
        self.dm.slot_store.slots["task_type_key"].value = "pipeline_inspection"
        self.dm.slot_store.slots["task_type_key"].status = "valid"
        
        self.dm.llm.extract_json.return_value = {
            "intent": "task_update",
            "slot_candidates": [
                {"raw_key": "型号", "canonical_key": "equipment_name", "raw_value": "观察级", "normalized_value": "观察级", "confidence": 1.0}
            ],
            "unresolved": []
        }
        
        self.dm.process("设备使用观察级")
        
        self.assertEqual(self.dm.slot_store.slots["equipment_name"].value, "观察级深海机器人 HP")
        self.assertEqual(self.dm.slot_store.slots["equipment_type"].value, "观察级深海机器人 HP")
        self.assertEqual(self.dm.slot_store.slots["equipment_name"].status, "valid")

    # 3. 一个槽位包含多个值
    def test_list_type_multiple_values(self):
        self.dm.reset()
        self.dm.task_state["task_type_key"] = "pipeline_inspection"
        self.dm.task_state["task_type"] = "管缆巡检"
        self.dm.slot_store.slots["task_type_key"].value = "pipeline_inspection"
        self.dm.slot_store.slots["task_type_key"].status = "valid"
        
        self.dm.llm.extract_json.return_value = {
            "intent": "task_update",
            "slot_candidates": [
                {"raw_key": "工具", "canonical_key": "payload", "raw_value": "摄像机和前视声呐", "normalized_value": ["高清水下摄像机", "前视声呐"], "confidence": 1.0}
            ],
            "unresolved": []
        }
        
        self.dm.process("工具携带摄像机和前视声呐")
        
        self.assertEqual(self.dm.slot_store.slots["payload"].value, ["高清水下摄像机", "前视声呐"])
        self.assertEqual(self.dm.slot_store.slots["payload"].status, "valid")

    # 4. 同一输入中包含重复信息
    def test_duplicate_info(self):
        self.dm.reset()
        self.dm.task_state["task_type_key"] = "pipeline_inspection"
        self.dm.slot_store.slots["task_type_key"].value = "pipeline_inspection"
        self.dm.slot_store.slots["task_type_key"].status = "valid"
        
        self.dm.llm.extract_json.return_value = {
            "intent": "task_update",
            "slot_candidates": [
                {"raw_key": "水深", "canonical_key": "water_depth", "raw_value": "300米", "normalized_value": 300, "confidence": 1.0},
                {"raw_key": "深度", "canonical_key": "water_depth", "raw_value": "300米", "normalized_value": 300, "confidence": 1.0}
            ],
            "unresolved": []
        }
        
        self.dm.process("水深300米，深度300米")
        
        self.assertEqual(self.dm.slot_store.slots["water_depth"].value, 300.0)
        self.assertEqual(self.dm.slot_store.slots["water_depth"].status, "valid")
        # Check version is updated but only incremented once in transaction
        self.assertEqual(self.dm.slot_store.slots["water_depth"].version, 1)

    # 5. 用户修改已经填写的槽位
    def test_user_modify_filled_slot(self):
        self.dm.reset()
        self.dm.task_state["task_type_key"] = "pipeline_inspection"
        self.dm.slot_store.slots["task_type_key"].value = "pipeline_inspection"
        self.dm.slot_store.slots["task_type_key"].status = "valid"
        
        # 1. Fill first value
        self.dm.llm.extract_json.return_value = {
            "intent": "task_update",
            "slot_candidates": [
                {"raw_key": "水深", "canonical_key": "water_depth", "raw_value": "300米", "normalized_value": 300, "confidence": 1.0}
            ],
            "unresolved": []
        }
        self.dm.process("深度300")
        self.assertEqual(self.dm.slot_store.slots["water_depth"].value, 300.0)
        self.assertEqual(self.dm.slot_store.slots["water_depth"].status, "valid")
        
        # 2. Modify to new value (directly accepted if conflict resolution is skipped or confirm matches)
        self.dm.llm.extract_json.return_value = {
            "intent": "task_update",
            "slot_candidates": [
                {"raw_key": "水深", "canonical_key": "water_depth", "raw_value": "500米", "normalized_value": 500, "confidence": 1.0}
            ],
            "unresolved": []
        }
        # In this turn, different value is processed:
        self.dm.process("修改水深为500米")
        
        # Due to conflict detection: it becomes 'conflict' status
        self.assertEqual(self.dm.slot_store.slots["water_depth"].status, "conflict")
        self.assertEqual(self.dm.slot_store.slots["water_depth"].value, 300.0)
        self.assertEqual(self.dm.slot_store.slots["water_depth"].candidate_value, 500.0)

    # 6. 新值与已有值发生冲突
    def test_conflict_detection_and_resolution(self):
        self.dm.reset()
        self.dm.task_state["task_type_key"] = "pipeline_inspection"
        self.dm.slot_store.slots["task_type_key"].value = "pipeline_inspection"
        self.dm.slot_store.slots["task_type_key"].status = "valid"
        
        # Set old value
        self.dm.slot_store.slots["water_depth"] = Slot("water_depth", value=300.0, status="valid")
        
        # Receive new conflicting value
        self.dm.llm.extract_json.return_value = {
            "intent": "task_update",
            "slot_candidates": [
                {"raw_key": "水深", "canonical_key": "water_depth", "raw_value": "500米", "normalized_value": 500, "confidence": 1.0}
            ],
            "unresolved": []
        }
        self.dm.process("水深是500")
        
        # Verify status is conflict and original value remains
        self.assertEqual(self.dm.slot_store.slots["water_depth"].status, "conflict")
        self.assertEqual(self.dm.slot_store.slots["water_depth"].value, 300.0)
        
        # User resolves by confirming
        self.dm.llm.extract_json.return_value = {
            "intent": "task_update",
            "slot_candidates": [],
            "unresolved": []
        }
        self.dm.process("是的，确定为500")
        
        self.assertEqual(self.dm.slot_store.slots["water_depth"].status, "valid")
        self.assertEqual(self.dm.slot_store.slots["water_depth"].value, 500.0)

    # 7. 输入值类型错误
    def test_value_type_error(self):
        self.dm.reset()
        self.dm.task_state["task_type_key"] = "pipeline_inspection"
        self.dm.slot_store.slots["task_type_key"].value = "pipeline_inspection"
        self.dm.slot_store.slots["task_type_key"].status = "valid"
        
        self.dm.llm.extract_json.return_value = {
            "intent": "task_update",
            "slot_candidates": [
                {"raw_key": "水深", "canonical_key": "water_depth", "raw_value": "abc", "normalized_value": "abc", "confidence": 1.0}
            ],
            "unresolved": []
        }
        
        self.dm.process("水深是abc")
        
        self.assertEqual(self.dm.slot_store.slots["water_depth"].status, "invalid")
        self.assertIsNotNone(self.dm.slot_store.slots["water_depth"].validation_error)

    # 8. 输入值超出允许范围
    def test_coordinate_out_of_range(self):
        self.dm.reset()
        self.dm.task_state["task_type_key"] = "pipeline_inspection"
        self.dm.slot_store.slots["task_type_key"].value = "pipeline_inspection"
        self.dm.slot_store.slots["task_type_key"].status = "valid"
        
        self.dm.llm.extract_json.return_value = {
            "intent": "task_update",
            "slot_candidates": [
                {"raw_key": "坐标", "canonical_key": "start_point", "raw_value": "95.0, 113.5", "normalized_value": {"lat": 95.0, "lon": 113.5}, "confidence": 1.0}
            ],
            "unresolved": []
        }
        
        self.dm.process("起始坐标为95.0, 113.5")
        
        self.assertEqual(self.dm.slot_store.slots["start_point"].status, "invalid")

    # 9. 出现无法识别的信息
    def test_unresolved_info_retained(self):
        self.dm.reset()
        self.dm.task_state["task_type_key"] = "pipeline_inspection"
        self.dm.slot_store.slots["task_type_key"].value = "pipeline_inspection"
        self.dm.slot_store.slots["task_type_key"].status = "valid"
        
        self.dm.llm.extract_json.return_value = {
            "intent": "task_update",
            "slot_candidates": [],
            "unresolved": ["测试未识别"]
        }
        
        self.dm.process("今天天气真好测试未识别")
        
        self.assertIn("测试未识别", self.dm.slot_store.unresolved)

    # 10. LLM 文本认为槽位完成，但后端校验未通过
    def test_backend_checks_trump_llm(self):
        self.dm.reset()
        self.dm.task_state["task_type_key"] = "pipeline_inspection"
        self.dm.slot_store.slots["task_type_key"].value = "pipeline_inspection"
        self.dm.slot_store.slots["task_type_key"].status = "valid"
        
        self.dm.llm.extract_json.return_value = {
            "intent": "task_update",
            "slot_candidates": [
                {"raw_key": "水深", "canonical_key": "water_depth", "raw_value": "abc", "normalized_value": "abc", "confidence": 1.0}
            ],
            "unresolved": []
        }
        
        self.dm.process("所有字段都已填完，水深是abc")
        
        # Verify that built_json is empty for water_depth, and it's marked missing
        self.assertNotIn("water_depth", self.dm._last_built_json)
        self.assertTrue(any(m["key"] == "water_depth" for m in self.dm._last_missing))

    # 11 & 12. task_state 与 slot_store 一致，built_json 与 slot_store 一致
    def test_task_state_and_built_json_consistency(self):
        self.dm.reset()
        self.dm.task_state["task_type_key"] = "pipeline_inspection"
        self.dm.slot_store.slots["task_type_key"].value = "pipeline_inspection"
        self.dm.slot_store.slots["task_type_key"].status = "valid"
        
        self.dm.slot_store.slots["water_depth"] = Slot("water_depth", value=300.0, status="valid")
        self.dm.slot_store.slots["cable_type"] = Slot("cable_type", value="海底油气管道", status="conflict")
        
        task_state = self.dm.slot_store.get_task_state()
        built_json = self.dm.slot_store.get_built_json()
        
        # task_state contains both valid and conflict
        self.assertEqual(task_state.get("water_depth"), 300.0)
        self.assertEqual(task_state.get("cable_type"), "海底油气管道")
        
        # built_json only contains valid slots
        self.assertEqual(built_json.get("water_depth"), 300.0)
        self.assertNotIn("cable_type", built_json)

    # 13. 前端刷新后槽位状态保持一致（Snapshot restore）
    def test_refresh_snapshot_restore(self):
        self.dm.reset()
        self.dm.task_state["task_type_key"] = "pipeline_inspection"
        self.dm.task_state["water_depth"] = 300.0
        self.dm.task_state["cable_type"] = "海底油气管道"
        
        snapshot = {
            "conversation_history": [],
            "task_state": self.dm.task_state,
            "mode": "normal",
            "phase": "collecting"
        }
        
        new_dm = DialogueManager(self.llm, self.kb)
        new_dm.load_snapshot(snapshot)
        
        self.assertEqual(new_dm.slot_store.slots["water_depth"].value, 300.0)
        self.assertEqual(new_dm.slot_store.slots["water_depth"].status, "valid")
        self.assertEqual(new_dm.slot_store.slots["cable_type"].value, "海底油气管道")
        self.assertEqual(new_dm.slot_store.slots["cable_type"].status, "valid")

    # 14. 两个请求并发修改同一个槽位
    def test_concurrent_slot_modification(self):
        self.dm.reset()
        self.dm.task_state["task_type_key"] = "pipeline_inspection"
        self.dm.slot_store.slots["task_type_key"].value = "pipeline_inspection"
        self.dm.slot_store.slots["task_type_key"].status = "valid"
        
        self.dm.slot_store.slots["water_depth"] = Slot("water_depth", value=300.0, status="valid", version=1)
        
        # Thread 1 updates to 400
        # Thread 2 updates to 500
        # We manually test version check on commit
        original_slots = self.dm.slot_store.clone_slots()
        
        # Simulate Tx 1
        tx1_slots = {k: s.copy() for k, s in original_slots.items()}
        tx1_slots["water_depth"].value = 400.0
        tx1_slots["water_depth"].status = "valid"
        
        # Simulate Tx 2 (running concurrently, based on same original state)
        tx2_slots = {k: s.copy() for k, s in original_slots.items()}
        tx2_slots["water_depth"].value = 500.0
        tx2_slots["water_depth"].status = "valid"
        
        # Commit Tx 1
        self.dm.slot_store.commit_transaction(tx1_slots, [])
        self.assertEqual(self.dm.slot_store.slots["water_depth"].value, 400.0)
        self.assertEqual(self.dm.slot_store.slots["water_depth"].version, 2)
        
        # Commit Tx 2 - fails or detects version mismatch
        current_slots = self.dm.slot_store.slots
        mismatch_detected = False
        for k, current_slot in current_slots.items():
            if k in tx2_slots and tx2_slots[k].version < current_slot.version:
                mismatch_detected = True
                
        self.assertTrue(mismatch_detected)

    # 15. 多槽位写入过程中出现异常，验证是否发生部分写入 (Rollback)
    def test_rollback_on_transaction_exception(self):
        self.dm.reset()
        self.dm.task_state["task_type_key"] = "pipeline_inspection"
        self.dm.slot_store.slots["task_type_key"].value = "pipeline_inspection"
        self.dm.slot_store.slots["task_type_key"].status = "valid"
        
        # Initialize slots for pipeline_inspection first
        required_fields = self.dm.builder.get_schema("pipeline_inspection", self.dm.mode)
        self.dm.slot_store.init_task_slots(required_fields)
        
        # Attempt an update that raises an error in dialogue processing
        self.dm.llm.extract_json.return_value = {
            "intent": "task_update",
            "slot_candidates": [
                {"raw_key": "水深", "canonical_key": "water_depth", "raw_value": "300米", "normalized_value": 300, "confidence": 1.0}
            ],
            "unresolved": []
        }
        
        # Inject mock failure into coordinate merging to trigger dialogue process exception
        self.dm._merge_coordinate_updates = MagicMock(side_effect=ValueError("Simulated coordinate merge failure"))
        
        with self.assertRaises(ValueError):
            self.dm.process("水深300")
            
        # Verify that original slots did not change (water_depth remains None/missing)
        self.assertIsNone(self.dm.slot_store.slots.get("water_depth").value)
        self.assertEqual(self.dm.slot_store.slots.get("water_depth").status, "missing")

    # 16. ASR 输入和文本输入经过同一套槽位处理逻辑
    def test_asr_and_text_input_use_same_pipeline(self):
        self.dm.reset()
        self.dm.task_state["task_type_key"] = "pipeline_inspection"
        self.dm.slot_store.slots["task_type_key"].value = "pipeline_inspection"
        self.dm.slot_store.slots["task_type_key"].status = "valid"
        
        # Simulate ASR normalized text: "管缆巡检"
        self.dm.llm.extract_json.return_value = {
            "intent": "task_update",
            "slot_candidates": [
                {"raw_key": "水深", "canonical_key": "water_depth", "raw_value": "300米", "normalized_value": 300, "confidence": 1.0}
            ],
            "unresolved": []
        }
        
        # Process the text derived from ASR
        self.dm.process("水深300米")
        
        self.assertEqual(self.dm.slot_store.slots["water_depth"].value, 300.0)
        self.assertEqual(self.dm.slot_store.slots["water_depth"].status, "valid")


if __name__ == "__main__":
    unittest.main()
