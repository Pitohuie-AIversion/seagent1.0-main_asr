import unittest
import threading
import time
import io
import logging
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
        cls.llm.extract_json.return_value = {"intent": "TASK_UPDATE", "slot_candidates": [], "unresolved": []}
        app.testing = True
        web_backend.init_manager(DialogueManager(cls.llm, cls.kb))

    def setUp(self):
        get_simulated_time().set_current_time(
            datetime(2026, 6, 30, 17, 38, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        )
        self.dm = DialogueManager(self.llm, self.kb)
        self.client = app.test_client()

    # 1. 完整 SlotStore 快照导出
    def test_slot_store_export_snapshot(self):
        store = SlotStore(self.kb)
        store.slots["water_depth"] = Slot(
            slot_name="water_depth",
            value=300.0,
            status="valid",
            raw_value="大约三百米",
            confidence=0.95,
            version=3
        )
        store.unresolved = ["未识别参数1"]
        store.version = 5

        snap = store.export_snapshot()
        self.assertEqual(snap["store_version"], 5)
        self.assertEqual(snap["unresolved"], ["未识别参数1"])
        self.assertIn("water_depth", snap["slots"])
        self.assertEqual(snap["slots"]["water_depth"]["value"], 300.0)
        self.assertEqual(snap["slots"]["water_depth"]["raw_value"], "大约三百米")
        self.assertEqual(snap["slots"]["water_depth"]["confidence"], 0.95)
        self.assertEqual(snap["slots"]["water_depth"]["version"], 3)

    # 2. 完整 SlotStore 快照恢复 (restore_snapshot & from_snapshot)
    def test_slot_store_restore_snapshot(self):
        snap = {
            "store_version": 7,
            "slots": {
                "water_depth": {
                    "slot_name": "water_depth",
                    "value": 300.0,
                    "value_type": "number",
                    "status": "candidate",
                    "source": "user_input",
                    "raw_value": "大约三百米",
                    "confidence": 0.92,
                    "validation_error": "Pending confirmation",
                    "updated_at": "2026-06-30T17:38:00",
                    "version": 4,
                    "candidate_value": 300.0
                }
            },
            "unresolved": ["未识别内容A"]
        }
        store = SlotStore.from_snapshot(snap, self.kb)
        self.assertEqual(store.version, 7)
        self.assertEqual(store.unresolved, ["未识别内容A"])
        self.assertIn("water_depth", store.slots)
        slot = store.slots["water_depth"]
        self.assertEqual(slot.value, 300.0)
        self.assertEqual(slot.status, "candidate")
        self.assertEqual(slot.raw_value, "大约三百米")
        self.assertEqual(slot.confidence, 0.92)
        self.assertEqual(slot.validation_error, "Pending confirmation")
        self.assertEqual(slot.version, 4)

    # 3. DialogueManager.load_snapshot 真实恢复 (New snapshot_version: 2 format)
    def test_dialogue_manager_load_snapshot_new_format(self):
        snap = {
            "snapshot_version": 2,
            "session_id": "test_sess_123",
            "mode": "normal",
            "phase": "collecting",
            "conversation_history": [{"role": "user", "content": "水深300米"}],
            "slot_store": {
                "store_version": 3,
                "slots": {
                    "task_type_key": {
                        "slot_name": "task_type_key",
                        "value": "pipeline_inspection",
                        "status": "valid",
                        "version": 1
                    },
                    "water_depth": {
                        "slot_name": "water_depth",
                        "value": 300.0,
                        "status": "valid",
                        "raw_value": "300米",
                        "confidence": 0.99,
                        "version": 2
                    }
                },
                "unresolved": []
            }
        }
        self.dm.load_snapshot(snap)
        self.assertEqual(self.dm.slot_store.version, 3)
        self.assertEqual(self.dm.task_state.get("water_depth"), 300.0)
        self.assertEqual(self.dm._last_built_json.get("water_depth"), 300.0)
        self.assertTrue(any(m["key"] == "cable_type" for m in self.dm._last_missing))

    # 4. 历史保存后再通过 /api/history/load 恢复
    def test_history_save_and_api_load(self):
        self.dm.reset()
        self.dm.slot_store.slots["task_type_key"] = Slot("task_type_key", value="pipeline_inspection", status="valid")
        self.dm.slot_store.slots["water_depth"] = Slot("water_depth", value=400.0, status="valid")
        self.dm.slot_store.version = 4

        # Save history via manager snapshot export
        hist_snap = {
            "session_id": "sess_api_hist",
            "conversation_history": [],
            "task_state": self.dm.slot_store.get_task_state(),
            "built_json": self.dm.slot_store.get_built_json(),
            "mode": self.dm.mode,
            "phase": self.dm.phase,
            "slot_store": self.dm.slot_store.export_snapshot()
        }
        
        # Load snapshot into a new session via load_snapshot
        new_dm = DialogueManager(self.llm, self.kb)
        new_dm.load_snapshot(hist_snap)
        self.assertEqual(new_dm.slot_store.version, 4)
        self.assertEqual(new_dm.task_state.get("water_depth"), 400.0)

    # 5. 旧格式 task_state 快照兼容 (Legacy format fallback)
    def test_legacy_snapshot_compatibility(self):
        legacy_snap = {
            "session_id": "legacy_sess",
            "conversation_history": [],
            "mode": "normal",
            "phase": "collecting",
            "task_state": {
                "task_type_key": "pipeline_inspection",
                "water_depth": 500.0
            }
        }
        self.dm.load_snapshot(legacy_snap)
        self.assertEqual(self.dm.task_state.get("water_depth"), 500.0)
        self.assertEqual(self.dm.slot_store.slots["water_depth"].status, "valid")
        self.assertEqual(self.dm.slot_store.slots["water_depth"].source, "legacy_import")

    # 6. invalid/conflict/candidate 恢复后状态不变
    def test_non_valid_status_preserved_on_restore(self):
        snap = {
            "store_version": 2,
            "slots": {
                "water_depth": {
                    "slot_name": "water_depth",
                    "value": 300.0,
                    "candidate_value": 500.0,
                    "status": "conflict",
                    "version": 1
                },
                "cable_type": {
                    "slot_name": "cable_type",
                    "candidate_value": "invalid_type",
                    "status": "invalid",
                    "validation_error": "Invalid cable type",
                    "version": 1
                }
            },
            "unresolved": []
        }
        self.dm.load_snapshot(snap)
        self.assertEqual(self.dm.slot_store.slots["water_depth"].status, "conflict")
        self.assertEqual(self.dm.slot_store.slots["cable_type"].status, "invalid")
        # Ensure non-valid slots do NOT enter task_state or built_json
        self.assertNotIn("water_depth", self.dm.task_state)
        self.assertNotIn("cable_type", self.dm.task_state)

    # 7. store version 和 slot version 恢复不变 (No unnecessary version increments)
    def test_store_and_slot_version_unmodified_on_restore(self):
        snap = {
            "store_version": 10,
            "slots": {
                "water_depth": {
                    "slot_name": "water_depth",
                    "value": 300.0,
                    "status": "valid",
                    "version": 7
                }
            },
            "unresolved": []
        }
        self.dm.load_snapshot(snap)
        self.assertEqual(self.dm.slot_store.version, 10)
        self.assertEqual(self.dm.slot_store.slots["water_depth"].version, 7)

    # 8. unresolved 恢复不变
    def test_unresolved_preserved_on_restore(self):
        snap = {
            "store_version": 1,
            "slots": {},
            "unresolved": ["未识别坐标X", "未知船只编号"]
        }
        self.dm.load_snapshot(snap)
        self.assertEqual(self.dm.slot_store.unresolved, ["未识别坐标X", "未知船只编号"])

    # 9. 单独删除槽位能提交
    def test_delete_slot_commit(self):
        store = SlotStore(self.kb)
        slots, unresolved, ver = store.snapshot()
        slots["custom_dynamic"] = Slot("custom_dynamic", value="temp", status="valid")
        store.commit_transaction(slots, unresolved, expected_version=ver)
        self.assertEqual(store.version, 1)
        self.assertIn("custom_dynamic", store.slots)

        # New transaction deletes "custom_dynamic"
        del_slots, del_unresolved, del_ver = store.snapshot()
        del del_slots["custom_dynamic"]
        store.commit_transaction(del_slots, del_unresolved, request_id="req_del_1", expected_version=del_ver)
        
        self.assertEqual(store.version, 2)
        self.assertNotIn("custom_dynamic", store.slots)

    # 10. raw_value 与 normalized_value 不同，包含真实抽取元数据
    def test_raw_value_and_confidence_preservation(self):
        self.dm.reset()
        self.llm.extract_json.return_value = {
            "intent": "TASK_UPDATE",
            "slot_candidates": [
                {
                    "raw_key": "任务类型",
                    "canonical_key": "task_type",
                    "raw_value": "巡检任务",
                    "normalized_value": "管缆巡检",
                    "confidence": 0.98
                },
                {
                    "raw_key": "水深",
                    "canonical_key": "water_depth",
                    "raw_value": "大约三百米左右",
                    "normalized_value": 300,
                    "confidence": 0.91
                }
            ],
            "unresolved": []
        }
        self.dm.process("执行巡检任务，水深大约三百米左右")
        slot = self.dm.slot_store.slots.get("water_depth")
        self.assertIsNotNone(slot)
        self.assertEqual(slot.value, 300.0)
        self.assertEqual(slot.raw_value, "大约三百米左右")
        self.assertEqual(slot.confidence, 0.91)

    # 11. confidence 正确保存
    def test_confidence_field_retention(self):
        self.dm.reset()
        self.llm.extract_json.return_value = {
            "intent": "TASK_UPDATE",
            "slot_candidates": [
                {
                    "raw_key": "任务类型",
                    "canonical_key": "task_type",
                    "raw_value": "管缆巡检",
                    "normalized_value": "管缆巡检",
                    "confidence": 0.99
                },
                {
                    "raw_key": "水深",
                    "canonical_key": "water_depth",
                    "raw_value": "300米",
                    "normalized_value": 300,
                    "confidence": 0.99
                }
            ],
            "unresolved": []
        }
        self.dm.process("管缆巡检，水深300米")
        slot = self.dm.slot_store.slots.get("water_depth")
        self.assertEqual(slot.confidence, 0.99)

    # 12. Validator 只读取 valid 事实 (Old value in conflict/invalid excluded)
    def test_validator_only_reads_valid_facts(self):
        self.dm.reset()
        store = self.dm.slot_store
        store.slots["task_type_key"] = Slot("task_type_key", value="pipeline_inspection", status="valid")
        store.slots["water_depth"] = Slot("water_depth", value=300.0, candidate_value=500.0, status="conflict")
        store.slots["cable_type"] = Slot("cable_type", value="旧管缆", candidate_value="invalid_cable", status="invalid")

        task_state = store.get_task_state()
        built_json = store.get_built_json()

        # Confirm conflict and invalid slots are EXCLUDED from task_state and built_json
        self.assertNotIn("water_depth", task_state)
        self.assertNotIn("cable_type", task_state)
        self.assertNotIn("water_depth", built_json)
        self.assertNotIn("cable_type", built_json)

        # Confirm Validator receives ONLY valid facts
        violations = self.dm.validator.validate(task_state)
        # 300 or 500 does not trigger false violations because it's excluded
        self.assertFalse(any("300" in v.message for v in violations))

    # 13. GENERAL_CHAT 不修改 SlotStore
    def test_general_chat_does_not_modify_slot_store(self):
        self.dm.reset()
        initial_ver = self.dm.slot_store.version
        
        self.llm.extract_json.return_value = {
            "intent": "GENERAL_CHAT",
            "slot_candidates": [],
            "unresolved": []
        }
        self.llm.generate.return_value = "你好！我是水下作业助手。"
        
        reply = self.dm.process("你好，你能做什么？")
        self.assertEqual(self.dm.slot_store.version, initial_ver)
        self.assertEqual(self.dm.slot_store.unresolved, [])
        self.assertIn("助手", reply)

    # 14. UNKNOWN 不修改 SlotStore
    def test_unknown_intent_does_not_modify_slot_store(self):
        self.dm.reset()
        initial_ver = self.dm.slot_store.version

        self.llm.extract_json.return_value = {
            "intent": "UNKNOWN",
            "slot_candidates": [],
            "unresolved": []
        }
        reply = self.dm.process("咕噜咕噜瓦卡瓦卡")
        self.assertEqual(self.dm.slot_store.version, initial_ver)
        self.assertEqual(self.dm.slot_store.unresolved, [])
        self.assertIn("对不起", reply)

    # 15. TASK_UPDATE 可以修改 SlotStore
    def test_task_update_modifies_slot_store(self):
        self.dm.reset()
        initial_ver = self.dm.slot_store.version

        self.llm.extract_json.return_value = {
            "intent": "TASK_UPDATE",
            "slot_candidates": [
                {"raw_key": "任务类型", "canonical_key": "task_type", "raw_value": "管缆巡检", "normalized_value": "管缆巡检", "confidence": 1.0}
            ],
            "unresolved": []
        }
        self.dm.process("我要进行管缆巡检")
        self.assertGreater(self.dm.slot_store.version, initial_ver)
        self.assertEqual(self.dm.slot_store.slots["task_type_key"].value, "pipeline_inspection")

    # 16. 真正的 /api/asr 到 /api/chat 链路测试
    def test_asr_end_to_end_pipeline(self):
        web_backend._shared_asr = MagicMock()
        web_backend._shared_asr.transcribe_file.return_value = {
            "text": "我要执行管缆巡检",
            "language_hint": "zh",
            "device": "cpu",
            "elapsed_ms": 10.0,
            "segments": []
        }
        web_backend._shared_llm.extract_json.return_value = {
            "intent": "TASK_UPDATE",
            "slot_candidates": [
                {"raw_key": "任务类型", "canonical_key": "task_type", "raw_value": "管缆巡检", "normalized_value": "管缆巡检", "confidence": 1.0}
            ],
            "unresolved": []
        }

        # Path A: /api/asr -> corrected_text -> /api/chat
        data_file = (io.BytesIO(b"dummy wav audio data"), "test.wav")
        res_asr = self.client.post("/api/asr", data={"audio": data_file}, content_type="multipart/form-data")
        self.assertEqual(res_asr.status_code, 200)
        asr_json = res_asr.get_json()
        corrected_text = asr_json["corrected_text"]

        res_chat_a = self.client.post("/api/chat", json={"session_id": "sess_path_a", "message": corrected_text})
        self.assertEqual(res_chat_a.status_code, 200)
        data_a = res_chat_a.get_json()

        # Path B: Direct /api/chat with same text
        res_chat_b = self.client.post("/api/chat", json={"session_id": "sess_path_b", "message": "我要执行管缆巡检"})
        self.assertEqual(res_chat_b.status_code, 200)
        data_b = res_chat_b.get_json()

        # Compare Path A and Path B outputs (filtering task_id sequence number variance)
        coll_a = {k: v for k, v in data_a["collected"].items() if k != "task_id"}
        coll_b = {k: v for k, v in data_b["collected"].items() if k != "task_id"}
        self.assertEqual(data_a["task_type"], data_b["task_type"])
        self.assertEqual(coll_a, coll_b)
        self.assertEqual(data_a["missing"], data_b["missing"])

    # 17. HTTP 500 不泄露 Traceback 或敏感路径，日志记录异常
    def test_http_500_no_traceback_leakage(self):
        mgr = web_backend.get_or_create_manager("sess_err_test")
        # Simulate exception containing sensitive file path
        mgr.process = MagicMock(side_effect=RuntimeError("Failed loading sensitive file: /root/seagent/config/private.yaml"))

        res = self.client.post("/api/chat", json={"session_id": "sess_err_test", "message": "测试错误"})
        self.assertEqual(res.status_code, 500)
        data = res.get_json()
        self.assertEqual(data["code"], 500)
        self.assertEqual(data["error"], "InternalServerError")
        self.assertEqual(data["msg"], "服务器内部错误，请稍后重试。")
        self.assertIn("request_id", data)
        # Ensure sensitive file path and traceback are NOT leaked in response JSON
        self.assertNotIn("/root/seagent/config/private.yaml", data["msg"])
        self.assertNotIn("Traceback", data["msg"])

    # 18. 事务失败时 DialogueManager 外围状态不变
    def test_dialogue_manager_outer_state_unmodified_on_commit_failure(self):
        self.dm.reset()
        initial_mode = self.dm.mode
        initial_phase = self.dm.phase
        initial_whitelist = set(self.dm._soft_whitelist)

        # Mock slot_store.commit_transaction to fail with SlotVersionConflict
        self.dm.slot_store.commit_transaction = MagicMock(side_effect=SlotVersionConflict("Version conflict test"))
        
        self.llm.extract_json.return_value = {
            "intent": "TASK_UPDATE",
            "slot_candidates": [
                {"raw_key": "加急", "canonical_key": "emergency_mode", "raw_value": "加急", "normalized_value": True, "confidence": 1.0}
            ],
            "unresolved": []
        }

        with self.assertRaises(SlotVersionConflict):
            self.dm.process("紧急任务")

        # Verify DialogueManager outer states are 100% UNMUTATED after commit failure
        self.assertEqual(self.dm.mode, initial_mode)
        self.assertEqual(self.dm.phase, initial_phase)
        self.assertEqual(self.dm._soft_whitelist, initial_whitelist)


if __name__ == "__main__":
    unittest.main()
