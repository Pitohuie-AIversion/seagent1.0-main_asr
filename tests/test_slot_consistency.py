import unittest
import threading
import time
import io
import logging
import tempfile
from unittest.mock import MagicMock, patch
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.knowledge_retriever import KnowledgeBase
from src.dialogue_manager import DialogueManager
from src.llm_client import LLMClient
from src.slot_store import SlotStore, Slot, SlotVersionConflict, SnapshotValidationError
from src.simulated_time import get_simulated_time
from src.history_manager import save_conversation, load_history
from datetime import datetime
from zoneinfo import ZoneInfo
import web_backend
from web_backend import app


def assert_ssot_consistency(test_case, dm):
    """
    SSOT 校验辅助函数：验证 dm.task_state 和 dm._last_built_json 完全从 slot_store 派生，
    且包含相同的 valid 槽位事实。
    """
    expected_task_state = dm.slot_store.get_task_state()
    expected_built_json = dm.slot_store.get_built_json()

    test_case.assertEqual(dm.task_state, expected_task_state)
    test_case.assertEqual(dm._last_built_json, expected_built_json)


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

    # 2. 完整 SlotStore 快照恢复 (restore_snapshot & from_snapshot) 与基础槽位补齐
    def test_slot_store_restore_snapshot_and_base_slots_backfill(self):
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
        # 验证基础槽位被自动补齐且不增加版本
        self.assertIn("task_type_key", store.slots)
        self.assertIn("intent_id", store.slots)
        slot = store.slots["water_depth"]
        self.assertEqual(slot.value, 300.0)
        self.assertEqual(slot.status, "candidate")
        self.assertEqual(slot.raw_value, "大约三百米")
        self.assertEqual(slot.confidence, 0.92)
        self.assertEqual(slot.version, 4)

    # 3. 快照结构校验与 SnapshotValidationError 抛出
    def test_snapshot_validation_errors(self):
        store = SlotStore(self.kb)
        
        # 1) 负数 store_version
        with self.assertRaises(SnapshotValidationError):
            store.restore_snapshot({"store_version": -1, "slots": {}, "unresolved": []})
            
        # 2) 非法 status
        with self.assertRaises(SnapshotValidationError):
            store.restore_snapshot({"store_version": 1, "slots": {"water_depth": {"status": "illegal_status"}}, "unresolved": []})
            
        # 3) 负数 slot version
        with self.assertRaises(SnapshotValidationError):
            store.restore_snapshot({"store_version": 1, "slots": {"water_depth": {"status": "valid", "version": -5}}, "unresolved": []})
            
        # 4) slots 不是 dict
        with self.assertRaises(SnapshotValidationError):
            store.restore_snapshot({"store_version": 1, "slots": [], "unresolved": []})

        # 5) unresolved 不是 list
        with self.assertRaises(SnapshotValidationError):
            store.restore_snapshot({"store_version": 1, "slots": {}, "unresolved": "not_a_list"})

    # 4. 真正历史文件与 /api/history/load HTTP API 端到端测试
    def test_history_file_and_api_load_e2e(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            with patch("src.history_manager.HISTORY_DIR", tmp_path):
                self.dm.reset()
                self.dm.slot_store.slots["task_type_key"] = Slot("task_type_key", value="pipeline_inspection", status="valid")
                self.dm.slot_store.slots["water_depth"] = Slot("water_depth", value=450.0, status="valid")
                self.dm.slot_store.version = 5

                # 真实调用 save_conversation 写入文件
                filename = save_conversation(
                    session_id="sess_e2e_api",
                    conversation_history=[{"role": "user", "content": "水深450米"}],
                    task_state=self.dm.slot_store.get_task_state(),
                    built_json=self.dm.slot_store.get_built_json(),
                    mode=self.dm.mode,
                    phase=self.dm.phase,
                    intent_id="sess_e2e_api",
                    slot_store=self.dm.slot_store.export_snapshot()
                )

                # 实际通过 HTTP POST /api/history/load 恢复
                res = self.client.post("/api/history/load", json={"history_id": filename, "session_id": "sess_restored_target"})
                self.assertEqual(res.status_code, 200)

                # 获取后端实际恢复的 manager 实例
                restored_mgr = web_backend.get_or_create_manager("sess_restored_target")
                self.assertEqual(restored_mgr.slot_store.version, 5)
                self.assertEqual(restored_mgr.task_state.get("water_depth"), 450.0)
                self.assertEqual(restored_mgr._last_built_json.get("water_depth"), 450.0)
                assert_ssot_consistency(self, restored_mgr)

    # 5. 非法与未识别 Intent Fail Closed (严格白名单与0写入)
    def test_illegal_intent_fails_closed(self):
        illegal_intents = ["INVALID_INTENT", "", None, 123, {}, "CHAT_UNKNOWN"]
        for bad_intent in illegal_intents:
            self.dm.reset()
            initial_ver = self.dm.slot_store.version

            # 即使包含 slot_candidates，只要 intent 非法，也绝对不能写入 SlotStore
            self.llm.extract_json.return_value = {
                "intent": bad_intent,
                "slot_candidates": [
                    {"raw_key": "水深", "canonical_key": "water_depth", "raw_value": "300米", "normalized_value": 300, "confidence": 0.9}
                ],
                "unresolved": []
            }

            reply = self.dm.process("非法意图消息测试")
            self.assertEqual(self.dm.slot_store.version, initial_ver)
            self.assertNotIn("water_depth", self.dm.task_state)
            self.assertIn("对不起", reply)
            assert_ssot_consistency(self, self.dm)

    # 6. 油田确认与取消事务化处理及 SSOT 断言
    def test_oilfield_confirmation_transaction(self):
        self.dm.reset()
        # 预置 pending 油田信息
        snap_slots, snap_unresolved, snap_ver = self.dm.slot_store.snapshot()
        snap_slots["pending_oilfield_name"] = Slot("pending_oilfield_name", value="流花11-1", status="valid")
        snap_slots["pending_oilfield_candidates"] = Slot("pending_oilfield_candidates", value=[{"name": "流花11-1油田", "id": "OF001", "confidence": 0.95}], status="valid")
        self.dm.slot_store.commit_transaction(snap_slots, snap_unresolved, expected_version=snap_ver)
        self.dm.task_state = self.dm.slot_store.get_task_state()

        # 触发确认油田
        reply = self.dm.process("是的，采用该油田")
        self.assertIn("已确认油田名称为“流花11-1油田”", reply)
        self.assertEqual(self.dm.slot_store.slots["oilfield_name"].value, "流花11-1油田")
        self.assertEqual(self.dm.slot_store.slots["oilfield_name"].source, "entity_linker")
        self.assertEqual(self.dm.slot_store.slots["oilfield_name"].raw_value, "流花11-1")
        self.assertIsNone(self.dm.slot_store.slots["pending_oilfield_name"].value)
        assert_ssot_consistency(self, self.dm)

    # 7. intent_id 事务化生成与 5 者完全一致
    def test_intent_id_transaction_consistency(self):
        self.dm.reset()
        self.dm.mode = "emergency"
        # 预置完整紧急模式管缆巡检任务必填项
        snap_slots, snap_unresolved, snap_ver = self.dm.slot_store.snapshot()
        snap_slots["task_type"] = Slot("task_type", value="管缆巡检", status="valid")
        snap_slots["task_type_key"] = Slot("task_type_key", value="pipeline_inspection", status="valid")
        snap_slots["start_time"] = Slot("start_time", value="2026-06-30T17:38:00", status="valid")
        snap_slots["start_point"] = Slot("start_point", value={"lat": 19.8, "lon": 113.5}, status="valid")
        snap_slots["end_point"] = Slot("end_point", value={"lat": 19.9, "lon": 113.6}, status="valid")
        snap_slots["water_depth"] = Slot("water_depth", value=300.0, status="valid")
        snap_slots["equipment_type"] = Slot("equipment_type", value="轻型工作级深海机器人 HP", status="valid")
        self.dm.slot_store.commit_transaction(snap_slots, snap_unresolved, expected_version=snap_ver)
        self.dm.task_state = self.dm.slot_store.get_task_state()
        self.dm.phase = "confirming"

        self.llm.extract_json.return_value = {"intent": "TASK_UPDATE", "slot_candidates": [], "unresolved": []}

        # 用户发送确认完成任务
        self.dm.process("确认执行任务")
        self.assertEqual(self.dm.phase, "done")
        
        # 验证 intent_id 事务化写入
        ti_intent_id = self.dm.slot_store.slots.get("intent_id").value
        self.assertIsNotNone(ti_intent_id)
        self.assertEqual(self.dm.slot_store.slots["intent_id"].source, "auto")
        self.assertEqual(self.dm.task_state["intent_id"], ti_intent_id)
        self.assertEqual(self.dm._last_built_json["intent_id"], ti_intent_id)
        assert_ssot_consistency(self, self.dm)

    # 8. 事务异常时外围状态与 ROV 候选 100% 零修改
    def test_outer_state_isolation_on_transaction_failure(self):
        self.dm.reset()
        initial_mode = self.dm.mode
        initial_phase = self.dm.phase
        initial_whitelist = set(self.dm._soft_whitelist)
        initial_rov_candidates = list(self.dm._pending_rov_candidates)

        # 模拟 commit_transaction 抛出乐观锁冲突
        self.dm.slot_store.commit_transaction = MagicMock(side_effect=SlotVersionConflict("Version conflict test"))
        
        self.llm.extract_json.return_value = {
            "intent": "TASK_UPDATE",
            "slot_candidates": [
                {"raw_key": "加急", "canonical_key": "emergency_mode", "raw_value": "加急", "normalized_value": True, "confidence": 1.0},
                {"raw_key": "ROV描述", "canonical_key": "rov_description", "raw_value": "深水大功率ROV", "normalized_value": "深水大功率ROV", "confidence": 1.0}
            ],
            "unresolved": []
        }

        with self.assertRaises(SlotVersionConflict):
            self.dm.process("紧急任务，需要深水大功率ROV")

        # 验证外围状态全部未修改
        self.assertEqual(self.dm.mode, initial_mode)
        self.assertEqual(self.dm.phase, initial_phase)
        self.assertEqual(self.dm._soft_whitelist, initial_whitelist)
        self.assertEqual(self.dm._pending_rov_candidates, initial_rov_candidates)
        assert_ssot_consistency(self, self.dm)

    # 9. HTTP 500 不泄露 Traceback 且带 request_id
    def test_http_500_no_traceback_leakage(self):
        mgr = web_backend.get_or_create_manager("sess_err_test")
        mgr.process = MagicMock(side_effect=RuntimeError("Failed loading sensitive file: /root/seagent/config/private.yaml"))

        res = self.client.post("/api/chat", json={"session_id": "sess_err_test", "message": "测试错误"})
        self.assertEqual(res.status_code, 500)
        data = res.get_json()
        self.assertEqual(data["code"], 500)
        self.assertEqual(data["error"], "InternalServerError")
        self.assertEqual(data["msg"], "服务器内部错误，请稍后重试。")
        self.assertIn("request_id", data)
        self.assertNotIn("/root/seagent/config/private.yaml", data["msg"])
        self.assertNotIn("Traceback", data["msg"])

    # 10. ASR 端到端链路与派生事实一致性
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

        data_file = (io.BytesIO(b"dummy wav audio data"), "test.wav")
        res_asr = self.client.post("/api/asr", data={"audio": data_file}, content_type="multipart/form-data")
        self.assertEqual(res_asr.status_code, 200)
        asr_json = res_asr.get_json()
        corrected_text = asr_json["corrected_text"]

        res_chat_a = self.client.post("/api/chat", json={"session_id": "sess_path_a", "message": corrected_text})
        self.assertEqual(res_chat_a.status_code, 200)
        data_a = res_chat_a.get_json()

        res_chat_b = self.client.post("/api/chat", json={"session_id": "sess_path_b", "message": "我要执行管缆巡检"})
        self.assertEqual(res_chat_b.status_code, 200)
        data_b = res_chat_b.get_json()

        coll_a = {k: v for k, v in data_a["collected"].items() if k != "task_id"}
        coll_b = {k: v for k, v in data_b["collected"].items() if k != "task_id"}
        self.assertEqual(data_a["task_type"], data_b["task_type"])
        self.assertEqual(coll_a, coll_b)
        self.assertEqual(data_a["missing"], data_b["missing"])


if __name__ == "__main__":
    unittest.main()
