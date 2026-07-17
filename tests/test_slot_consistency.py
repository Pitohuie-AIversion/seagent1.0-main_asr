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
from src.slot_store import SlotStore, Slot, SlotVersionConflict
from src.simulated_time import get_simulated_time
from datetime import datetime
from zoneinfo import ZoneInfo
import web_backend
from web_backend import app


class SlotConsistencyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.kb = KnowledgeBase()
        cls.llm = MagicMock(spec=LLMClient)
        cls.llm.generate.return_value = "已接收到您的任务输入"
        cls.llm.filter_reply.side_effect = lambda text, *args, **kwargs: text if isinstance(text, str) else "已接收到您的任务输入"
        cls.llm.extract_json.return_value = {"intent": "task_update", "slot_candidates": [], "unresolved": []}
        app.testing = True
        web_backend.init_manager(DialogueManager(cls.llm, cls.kb))

    def setUp(self):
        get_simulated_time().set_current_time(
            datetime(2026, 6, 30, 17, 38, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        )
        self.dm = DialogueManager(self.llm, self.kb)
        self.client = app.test_client()

    # 1. 两个事务基于同一 store version，第二个因为版本过时被拒绝 (Optimistic Lock)
    def test_optimistic_version_conflict(self):
        store = SlotStore(self.kb)
        slots, unresolved, ver = store.snapshot()
        
        # Tx1 modifies store
        tx1_slots = {k: s.copy() for k, s in slots.items()}
        tx1_slots["task_type_key"] = Slot("task_type_key", value="pipeline_inspection", status="valid")
        store.commit_transaction(tx1_slots, unresolved, request_id="req_1", expected_version=ver)
        self.assertEqual(store.version, 1)
        self.assertEqual(store.slots["task_type_key"].value, "pipeline_inspection")

        # Tx2 attempts to commit with stale expected_version=0
        tx2_slots = {k: s.copy() for k, s in slots.items()}
        tx2_slots["task_type_key"] = Slot("task_type_key", value="tree_valve_operation", status="valid")
        
        with self.assertRaises(SlotVersionConflict):
            store.commit_transaction(tx2_slots, unresolved, request_id="req_2", expected_version=ver)

        # Confirm Tx2 did NOT overwrite Tx1
        self.assertEqual(store.version, 1)
        self.assertEqual(store.slots["task_type_key"].value, "pipeline_inspection")

    # 2. 多线程并发提交同一槽位 (Multi-thread Concurrency)
    def test_multithreaded_concurrent_commit(self):
        store = SlotStore(self.kb)
        slots, unresolved, ver = store.snapshot()

        success_count = 0
        conflict_count = 0
        lock = threading.Lock()

        def worker(val, req_id):
            nonlocal success_count, conflict_count
            tx_slots = {k: s.copy() for k, s in slots.items()}
            tx_slots["water_depth"] = Slot("water_depth", value=val, status="valid")
            try:
                store.commit_transaction(tx_slots, unresolved, request_id=req_id, expected_version=ver)
                with lock:
                    success_count += 1
            except SlotVersionConflict:
                with lock:
                    conflict_count += 1

        threads = [threading.Thread(target=worker, args=(100 + i, f"req_thread_{i}")) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(success_count, 1)
        self.assertEqual(conflict_count, 9)
        self.assertEqual(store.version, 1)

    # 3. 提交内部模拟异常 (Atomic Rollback on Commit Exception)
    def test_transaction_commit_exception(self):
        store = SlotStore(self.kb)
        slots, unresolved, ver = store.snapshot()

        # Inject broken object that fails inside commit comparison
        class BrokenObj:
            def __eq__(self, other):
                raise RuntimeError("Simulated internal error")

        tx_slots = {k: s.copy() for k, s in slots.items()}
        tx_slots["task_type_key"] = Slot("task_type_key", value=BrokenObj(), status="valid")

        with self.assertRaises(RuntimeError):
            store.commit_transaction(tx_slots, unresolved, request_id="req_fail", expected_version=ver)

        self.assertEqual(store.version, 0)
        self.assertEqual(store.unresolved, [])

    # 4. 无变化提交 (No-change Commit)
    def test_no_change_commit(self):
        store = SlotStore(self.kb)
        slots, unresolved, ver = store.snapshot()
        store.commit_transaction(slots, unresolved, request_id="req_same", expected_version=ver)
        
        self.assertEqual(store.version, 0)
        self.assertEqual(store.slots["task_type_key"].version, 0)

    # 5. candidate_value 单独变化 (Candidate Value Change Increments Version)
    def test_candidate_value_change_increments_version(self):
        store = SlotStore(self.kb)
        slots, unresolved, ver = store.snapshot()
        
        tx_slots = {k: s.copy() for k, s in slots.items()}
        tx_slots["water_depth"] = Slot("water_depth", value=None, candidate_value=300.0, status="candidate")
        
        store.commit_transaction(tx_slots, unresolved, request_id="req_cand", expected_version=ver)
        
        self.assertEqual(store.version, 1)
        self.assertEqual(store.slots["water_depth"].version, 1)

    # 6. unresolved 单独变化 (Unresolved Change Increments Store Version)
    def test_unresolved_change_increments_store_version(self):
        store = SlotStore(self.kb)
        slots, unresolved, ver = store.snapshot()
        
        tx_unresolved = ["测试未识别内容"]
        store.commit_transaction(slots, tx_unresolved, request_id="req_unres", expected_version=ver)
        
        self.assertEqual(store.version, 1)
        self.assertEqual(store.unresolved, ["测试未识别内容"])

    # 7. 任务类型切换 (Task Type Switch Clears Old Dynamic Slots)
    def test_task_type_switch_clears_old_slots(self):
        self.dm.reset()
        # 1. Pipeline Inspection
        self.dm.llm.extract_json.return_value = {
            "intent": "task_update",
            "slot_candidates": [
                {"raw_key": "任务类型", "canonical_key": "task_type", "raw_value": "管缆巡检", "normalized_value": "管缆巡检", "confidence": 1.0},
                {"raw_key": "管缆类型", "canonical_key": "cable_type", "raw_value": "油气管道", "normalized_value": "海底油气管道", "confidence": 1.0},
                {"raw_key": "起始坐标", "canonical_key": "start_point", "raw_value": "19.5, 113.2", "normalized_value": {"lat": 19.5, "lon": 113.2}, "confidence": 1.0}
            ],
            "unresolved": []
        }
        self.dm.process("执行管缆巡检，电缆类型是油气管道，起始坐标是19.5, 113.2")
        
        self.assertEqual(self.dm.slot_store.slots["task_type_key"].value, "pipeline_inspection")
        self.assertIn("cable_type", self.dm.slot_store.slots)
        self.assertIn("start_point", self.dm.slot_store.slots)

        # 2. Switch to Tree Valve Operation
        self.dm.llm.extract_json.return_value = {
            "intent": "task_update",
            "slot_candidates": [
                {"raw_key": "任务类型", "canonical_key": "task_type", "raw_value": "采油树控制面板插入", "normalized_value": "采油树控制面板插入", "confidence": 1.0},
                {"raw_key": "井口编号", "canonical_key": "wellhead_id", "raw_value": "WH-01", "normalized_value": "WH-01", "confidence": 1.0}
            ],
            "unresolved": []
        }
        self.dm.process("切换为采油树控制面板插入，井口WH-01")

        # Confirm Task A specific slots (cable_type, start_point) are purged
        self.assertEqual(self.dm.slot_store.slots["task_type_key"].value, "tree_valve_operation")
        self.assertNotIn("cable_type", self.dm.slot_store.slots)
        self.assertNotIn("start_point", self.dm.slot_store.slots)
        self.assertNotIn("cable_type", self.dm.task_state)
        self.assertNotIn("cable_type", self.dm._last_built_json)
        self.assertFalse(any(m["key"] == "cable_type" for m in self.dm._last_missing))

    # 8. invalid/conflict 不进入 task_state 和 built_json
    def test_invalid_and_conflict_excluded_from_facts(self):
        self.dm.reset()
        self.dm.slot_store.slots["task_type_key"] = Slot("task_type_key", value="pipeline_inspection", status="valid")
        self.dm.slot_store.slots["water_depth"] = Slot("water_depth", value=300.0, status="valid")
        self.dm.slot_store.slots["cable_type"] = Slot("cable_type", value="海底油气管道", candidate_value="电力电缆", status="conflict")
        self.dm.slot_store.slots["support_vessel"] = Slot("support_vessel", value=None, candidate_value="invalid_ship", status="invalid", validation_error="Invalid vessel")

        task_state = self.dm.slot_store.get_task_state()
        built_json = self.dm.slot_store.get_built_json()

        # task_state & built_json ONLY contain valid slots
        self.assertEqual(task_state, {"task_type_key": "pipeline_inspection", "water_depth": 300.0})
        self.assertEqual(built_json, {"task_type_key": "pipeline_inspection", "water_depth": 300.0})
        self.assertNotIn("cable_type", task_state)
        self.assertNotIn("support_vessel", task_state)

    # 9. 快照恢复 (Snapshot Restoration Integrity)
    def test_snapshot_restore_full_integrity(self):
        store = SlotStore(self.kb)
        required_fields = self.dm.builder.get_schema("pipeline_inspection", "normal")
        store.init_task_slots(required_fields)

        store.slots["water_depth"] = Slot(
            slot_name="water_depth",
            value=300.0,
            status="valid",
            raw_value="300米",
            candidate_value=None,
            validation_error=None,
            version=2
        )
        store.unresolved = ["未识别参数"]

        slots_dict, unresolved_list, store_ver = store.snapshot()

        new_store = SlotStore(self.kb)
        new_store.slots = slots_dict
        new_store.unresolved = unresolved_list
        new_store.version = store_ver

        self.assertEqual(new_store.slots["water_depth"].value, 300.0)
        self.assertEqual(new_store.slots["water_depth"].status, "valid")
        self.assertEqual(new_store.slots["water_depth"].raw_value, "300米")
        self.assertEqual(new_store.slots["water_depth"].version, 2)
        self.assertEqual(new_store.unresolved, ["未识别参数"])
        self.assertEqual(new_store.version, store_ver)

    # 10. ASR与文本输入到达相同的处理逻辑
    def test_asr_and_text_input_pipeline(self):
        self.dm.reset()
        web_backend._shared_llm.extract_json.return_value = {
            "intent": "task_update",
            "slot_candidates": [
                {"raw_key": "任务类型", "canonical_key": "task_type", "raw_value": "管缆巡检", "normalized_value": "管缆巡检", "confidence": 1.0}
            ],
            "unresolved": []
        }
        res = self.client.post("/api/chat", json={"session_id": "test_asr_session", "message": "我要进行管缆巡检"})
        if res.status_code != 200:
            self.fail(f"API chat failed: {res.get_json()}")
        data = res.get_json()
        self.assertEqual(data["task_type"], "pipeline_inspection")

    # 11. 普通对话不修改 SlotStore version
    def test_ordinary_chat_does_not_mutate_slot_store(self):
        self.dm.reset()
        initial_ver = self.dm.slot_store.version

        reply = self.dm.process("你是谁")
        self.assertIn("专业的水下多智能体任务决策大模型", reply)
        self.assertEqual(self.dm.slot_store.version, initial_ver)
        self.assertEqual(self.dm.slot_store.unresolved, [])

    # 12. /api/chat 发生异常时返回结构化 JSON (HTTP 409 / 400 / 500)
    def test_api_chat_structured_json_error(self):
        # Trigger SlotVersionConflict error by mocking DialogueManager.process
        mgr = web_backend.get_or_create_manager("test_conflict_session")
        mgr.process = MagicMock(side_effect=SlotVersionConflict("Concurrent edit detected"))

        res = self.client.post("/api/chat", json={"session_id": "test_conflict_session", "message": "修改水深"})
        self.assertEqual(res.status_code, 409)
        data = res.get_json()
        self.assertEqual(data["code"], 409)
        self.assertEqual(data["error"], "SlotVersionConflict")
        self.assertIn("并发版本冲突", data["msg"])


if __name__ == "__main__":
    unittest.main()
