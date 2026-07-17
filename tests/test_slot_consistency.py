import unittest
import threading
import time
import io
import json
import logging
import tempfile
from unittest.mock import MagicMock, patch
from pathlib import Path
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.knowledge_retriever import KnowledgeBase
from src.dialogue_manager import DialogueManager
from src.llm_client import LLMClient
from src.slot_store import SlotStore, Slot, SlotVersionConflict, SnapshotValidationError
from src.simulated_time import get_simulated_time
from src.history_manager import save_conversation, load_history
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

    # 1. 单条消息同时写入三个槽位
    def test_01_single_message_three_slots(self):
        self.dm.reset()
        self.llm.extract_json.return_value = {
            "intent": "TASK_CREATE",
            "slot_candidates": [
                {"raw_key": "任务类型", "canonical_key": "task_type", "raw_value": "管缆巡检", "normalized_value": "管缆巡检", "confidence": 1.0},
                {"raw_key": "水深", "canonical_key": "water_depth", "raw_value": "300米", "normalized_value": 300.0, "confidence": 0.95},
                {"raw_key": "管缆类型", "canonical_key": "cable_type", "raw_value": "电力电缆", "normalized_value": "电力电缆", "confidence": 0.90}
            ],
            "unresolved": []
        }
        self.dm.process("我要新建管缆巡检任务，水深300米，电力电缆")
        self.assertEqual(self.dm.slot_store.slots["task_type"].value, "管缆巡检")
        self.assertEqual(self.dm.slot_store.slots["water_depth"].value, 300.0)
        self.assertEqual(self.dm.slot_store.slots["cable_type"].value, "电力电缆")
        assert_ssot_consistency(self, self.dm)

    # 2. alias 映射到 canonical field
    def test_02_alias_mapping_canonical_key(self):
        self.dm.reset()
        self.dm.slot_store.slots["task_type_key"] = Slot("task_type_key", value="pipeline_inspection", status="valid")
        self.dm.slot_store.slots["task_type"] = Slot("task_type", value="管缆巡检", status="valid")
        self.dm.task_state = self.dm.slot_store.get_task_state()

        self.llm.extract_json.return_value = {
            "intent": "TASK_UPDATE",
            "slot_candidates": [
                {"raw_key": "深度", "canonical_key": "water_depth", "raw_value": "500米", "normalized_value": 500.0, "confidence": 1.0}
            ],
            "unresolved": []
        }
        self.dm.process("水深深度为500米")
        self.assertIn("water_depth", self.dm.slot_store.slots)
        self.assertEqual(self.dm.slot_store.slots["water_depth"].value, 500.0)
        assert_ssot_consistency(self, self.dm)

    # 3. 一个槽位包含多个值 (多值列表)
    def test_03_multi_value_slot(self):
        self.dm.reset()
        self.dm.slot_store.slots["task_type_key"] = Slot("task_type_key", value="pipeline_inspection", status="valid")
        self.dm.slot_store.slots["task_type"] = Slot("task_type", value="管缆巡检", status="valid")
        self.dm.task_state = self.dm.slot_store.get_task_state()

        self.llm.extract_json.return_value = {
            "intent": "TASK_UPDATE",
            "slot_candidates": [
                {"raw_key": "负载工具", "canonical_key": "payload", "raw_value": "高清水下摄像机,前视声呐", "normalized_value": ["高清水下摄像机", "前视声呐"], "confidence": 1.0}
            ],
            "unresolved": []
        }
        self.dm.process("携带高清水下摄像机和前视声呐")
        self.assertEqual(self.dm.slot_store.slots["payload"].value, ["高清水下摄像机", "前视声呐"])
        assert_ssot_consistency(self, self.dm)

    # 4. 重复输入按明确规则处理
    def test_04_duplicate_inputs_handling(self):
        self.dm.reset()
        self.dm.slot_store.slots["task_type_key"] = Slot("task_type_key", value="pipeline_inspection", status="valid")
        self.dm.slot_store.slots["task_type"] = Slot("task_type", value="管缆巡检", status="valid")
        self.dm.task_state = self.dm.slot_store.get_task_state()

        self.llm.extract_json.return_value = {
            "intent": "TASK_UPDATE",
            "slot_candidates": [
                {"raw_key": "水深", "canonical_key": "water_depth", "raw_value": "300米", "normalized_value": 300.0, "confidence": 1.0}
            ],
            "unresolved": []
        }
        self.dm.process("水深300米")
        ver1 = self.dm.slot_store.version
        # 再次发送完全相同的数据
        self.dm.process("水深300米")
        ver2 = self.dm.slot_store.version
        self.assertEqual(ver1, ver2)
        assert_ssot_consistency(self, self.dm)

    # 5. 用户修改已有槽位
    def test_05_update_existing_valid_slot(self):
        self.dm.reset()
        self.dm.slot_store.slots["task_type_key"] = Slot("task_type_key", value="pipeline_inspection", status="valid")
        self.dm.slot_store.slots["task_type"] = Slot("task_type", value="管缆巡检", status="valid")
        self.dm.task_state = self.dm.slot_store.get_task_state()

        self.llm.extract_json.return_value = {
            "intent": "TASK_UPDATE",
            "slot_candidates": [
                {"raw_key": "水深", "canonical_key": "water_depth", "raw_value": "300米", "normalized_value": 300.0, "confidence": 1.0}
            ],
            "unresolved": []
        }
        self.dm.process("水深300米")
        slot_ver1 = self.dm.slot_store.slots["water_depth"].version

        self.llm.extract_json.return_value = {
            "intent": "TASK_UPDATE",
            "slot_candidates": [
                {"raw_key": "水深", "canonical_key": "water_depth", "raw_value": "450米", "normalized_value": 450.0, "confidence": 1.0}
            ],
            "unresolved": []
        }
        self.dm.process("改成水深450米")
        self.assertEqual(self.dm.slot_store.slots["water_depth"].value, 450.0)
        self.assertGreater(self.dm.slot_store.slots["water_depth"].version, slot_ver1)
        assert_ssot_consistency(self, self.dm)

    # 6. 新旧值冲突时保存 value 和 candidate_value
    def test_06_conflict_value_and_candidate_value(self):
        store = SlotStore(self.kb)
        store.slots["water_depth"] = Slot("water_depth", value=300.0, status="valid", version=1)
        
        snap_slots, snap_unresolved, snap_ver = store.snapshot()
        # 写入冲突候选
        snap_slots["water_depth"].candidate_value = 600.0
        snap_slots["water_depth"].status = "conflict"
        snap_slots["water_depth"].validation_error = "Conflict detected"
        store.commit_transaction(snap_slots, snap_unresolved, expected_version=snap_ver)

        self.assertEqual(store.slots["water_depth"].value, 300.0)
        self.assertEqual(store.slots["water_depth"].candidate_value, 600.0)
        self.assertEqual(store.slots["water_depth"].status, "conflict")
        # 冲突槽位不得进入 task_state
        self.assertNotIn("water_depth", store.get_task_state())

    # 7. 类型错误保留 raw_value 和 validation_error
    def test_07_type_validation_error(self):
        store = SlotStore(self.kb)
        snap_slots, snap_unresolved, snap_ver = store.snapshot()
        snap_slots["water_depth"] = Slot("water_depth", value=None, status="invalid", raw_value="五百米左右", validation_error="Expected float")
        store.commit_transaction(snap_slots, snap_unresolved, expected_version=snap_ver)

        slot = store.slots["water_depth"]
        self.assertEqual(slot.status, "invalid")
        self.assertEqual(slot.raw_value, "五百米左右")
        self.assertEqual(slot.validation_error, "Expected float")
        self.assertNotIn("water_depth", store.get_task_state())

    # 8. 值域错误不得进入 task_state 和 built_json
    def test_08_invalid_domain_value_excluded_from_task_state(self):
        store = SlotStore(self.kb)
        snap_slots, snap_unresolved, snap_ver = store.snapshot()
        snap_slots["cable_type"] = Slot("cable_type", value="非法缆线", status="invalid", validation_error="Out of domain")
        store.commit_transaction(snap_slots, snap_unresolved, expected_version=snap_ver)

        self.assertNotIn("cable_type", store.get_task_state())
        self.assertNotIn("cable_type", store.get_built_json())

    # 9. 无法识别的任务信息进入 unresolved
    def test_09_unrecognized_input_in_unresolved(self):
        self.dm.reset()
        self.llm.extract_json.return_value = {
            "intent": "TASK_UPDATE",
            "slot_candidates": [],
            "unresolved": ["某些无法理解的内容"]
        }
        self.dm.process("测试处理无法理解的内容")
        self.assertIn("某些无法理解的内容", self.dm.slot_store.unresolved)

    # 10. GENERAL_CHAT 不修改 SlotStore
    def test_10_general_chat_leaves_slot_store_untouched(self):
        self.dm.reset()
        initial_ver = self.dm.slot_store.version
        initial_state = self.dm.slot_store.get_task_state()

        self.llm.extract_json.return_value = {
            "intent": "GENERAL_CHAT",
            "slot_candidates": [],
            "unresolved": []
        }
        reply = self.dm.process("你好")
        self.assertEqual(self.dm.slot_store.version, initial_ver)
        self.assertEqual(self.dm.slot_store.get_task_state(), initial_state)
        assert_ssot_consistency(self, self.dm)

    # 11. UNKNOWN 不修改 SlotStore
    def test_11_unknown_intent_leaves_slot_store_untouched(self):
        self.dm.reset()
        initial_ver = self.dm.slot_store.version

        self.llm.extract_json.return_value = {
            "intent": "UNKNOWN",
            "slot_candidates": [],
            "unresolved": []
        }
        reply = self.dm.process("???")
        self.assertEqual(self.dm.slot_store.version, initial_ver)
        self.assertIn("对不起", reply)
        assert_ssot_consistency(self, self.dm)

    # 12. Mock 模式“你好”能够正常对话
    def test_12_mock_mode_greetings(self):
        client = LLMClient(llm_instance=None, tokenizer=None)
        dm = DialogueManager(client, self.kb)
        reply = dm.process("你好")
        self.assertIn("您好", reply)
        self.assertEqual(dm.slot_store.version, 0)
        assert_ssot_consistency(self, dm)

    # 13. Mock 模式“你能做什么”能够正常对话
    def test_13_mock_mode_capabilities(self):
        client = LLMClient(llm_instance=None, tokenizer=None)
        dm = DialogueManager(client, self.kb)
        reply = dm.process("你能做什么")
        self.assertIn("可以协助您", reply)
        self.assertEqual(dm.slot_store.version, 0)
        assert_ssot_consistency(self, dm)

    # 14. TASK_CREATE 可以写入 SlotStore
    def test_14_task_create_updates_slot_store(self):
        self.dm.reset()
        self.llm.extract_json.return_value = {
            "intent": "TASK_CREATE",
            "slot_candidates": [
                {"raw_key": "任务类型", "canonical_key": "task_type", "raw_value": "管缆巡检", "normalized_value": "管缆巡检", "confidence": 1.0}
            ],
            "unresolved": []
        }
        self.dm.process("新建管缆巡检")
        self.assertEqual(self.dm.slot_store.slots["task_type"].value, "管缆巡检")
        self.assertGreater(self.dm.slot_store.version, 0)
        assert_ssot_consistency(self, self.dm)

    # 15. TASK_UPDATE 可以写入 SlotStore
    def test_15_task_update_updates_slot_store(self):
        self.dm.reset()
        self.dm.slot_store.slots["task_type_key"] = Slot("task_type_key", value="pipeline_inspection", status="valid")
        self.dm.slot_store.slots["task_type"] = Slot("task_type", value="管缆巡检", status="valid")
        self.dm.task_state = self.dm.slot_store.get_task_state()

        self.llm.extract_json.return_value = {
            "intent": "TASK_UPDATE",
            "slot_candidates": [
                {"raw_key": "水深", "canonical_key": "water_depth", "raw_value": "300米", "normalized_value": 300.0, "confidence": 1.0}
            ],
            "unresolved": []
        }
        self.dm.process("水深300米")
        self.assertEqual(self.dm.slot_store.slots["water_depth"].value, 300.0)
        assert_ssot_consistency(self, self.dm)

    # 16. task_state 与 SlotStore 一致
    def test_16_ssot_task_state_consistency(self):
        self.dm.reset()
        self.llm.extract_json.return_value = {
            "intent": "TASK_CREATE",
            "slot_candidates": [
                {"raw_key": "任务类型", "canonical_key": "task_type", "raw_value": "管缆巡检", "normalized_value": "管缆巡检", "confidence": 1.0}
            ],
            "unresolved": []
        }
        self.dm.process("新建管缆巡检")
        self.assertEqual(self.dm.task_state, self.dm.slot_store.get_task_state())

    # 17. built_json 与 SlotStore 一致
    def test_17_ssot_built_json_consistency(self):
        self.dm.reset()
        self.llm.extract_json.return_value = {
            "intent": "TASK_CREATE",
            "slot_candidates": [
                {"raw_key": "任务类型", "canonical_key": "task_type", "raw_value": "管缆巡检", "normalized_value": "管缆巡检", "confidence": 1.0}
            ],
            "unresolved": []
        }
        self.dm.process("新建管缆巡检")
        self.assertEqual(self.dm._last_built_json, self.dm.slot_store.get_built_json())

    # 18. missing_slots 从 SlotStore 派生
    def test_18_missing_slots_derived_from_slot_store(self):
        self.dm.reset()
        self.llm.extract_json.return_value = {
            "intent": "TASK_CREATE",
            "slot_candidates": [
                {"raw_key": "任务类型", "canonical_key": "task_type", "raw_value": "管缆巡检", "normalized_value": "管缆巡检", "confidence": 1.0}
            ],
            "unresolved": []
        }
        self.dm.process("新建管缆巡检")
        schema = self.dm.builder.get_schema("pipeline_inspection", self.dm.mode)
        expected_missing = self.dm.slot_store.get_missing_slots(schema)
        self.assertEqual(self.dm._last_missing, expected_missing)

    # 19. 同一请求的业务槽位和 task_id 只有一次事务提交 (Test A)
    def test_19_test_a_single_commit_transaction_per_request(self):
        self.dm.reset()
        commit_spy = MagicMock(wraps=self.dm.slot_store.commit_transaction)
        self.dm.slot_store.commit_transaction = commit_spy

        self.llm.extract_json.return_value = {
            "intent": "TASK_CREATE",
            "slot_candidates": [
                {"raw_key": "任务类型", "canonical_key": "task_type", "raw_value": "管缆巡检", "normalized_value": "管缆巡检", "confidence": 1.0}
            ],
            "unresolved": []
        }

        self.dm.process("新建管缆巡检任务")
        # 验证事务提交仅被调用 1 次
        self.assertEqual(commit_spy.call_count, 1)
        # 验证 task_id 包含在当次提交中
        committed_slots = commit_spy.call_args[0][0]
        self.assertIn("task_id", committed_slots)
        self.assertIsNotNone(committed_slots["task_id"].value)
        assert_ssot_consistency(self, self.dm)

    # 20. 模拟 task_id 生成异常时全部状态零修改 (Test B)
    def test_20_test_b_task_id_exception_leaves_state_untouched(self):
        self.dm.reset()
        initial_ver = self.dm.slot_store.version
        initial_state = self.dm.slot_store.get_task_state()
        initial_hist_len = len(self.dm.conversation_history)

        self.llm.extract_json.return_value = {
            "intent": "TASK_CREATE",
            "slot_candidates": [
                {"raw_key": "任务类型", "canonical_key": "task_type", "raw_value": "管缆巡检", "normalized_value": "管缆巡检", "confidence": 1.0}
            ],
            "unresolved": []
        }

        with patch.object(self.dm.builder, "_generate_task_id", side_effect=RuntimeError("Task ID generation failed")):
            with self.assertRaises(RuntimeError):
                self.dm.process("新建管缆巡检任务")

        # 验证全部状态 100% 未被污染
        self.assertEqual(self.dm.slot_store.version, initial_ver)
        self.assertEqual(self.dm.slot_store.get_task_state(), initial_state)
        self.assertEqual(len(self.dm.conversation_history), initial_hist_len)
        assert_ssot_consistency(self, self.dm)

    # 21. 模拟主 commit 失败时全部状态零修改
    def test_21_main_commit_failure_leaves_state_untouched(self):
        self.dm.reset()
        initial_ver = self.dm.slot_store.version
        initial_hist_len = len(self.dm.conversation_history)

        self.dm.slot_store.commit_transaction = MagicMock(side_effect=SlotVersionConflict("Version conflict test"))

        self.llm.extract_json.return_value = {
            "intent": "TASK_UPDATE",
            "slot_candidates": [
                {"raw_key": "水深", "canonical_key": "water_depth", "raw_value": "300米", "normalized_value": 300.0, "confidence": 1.0}
            ],
            "unresolved": []
        }

        with self.assertRaises(SlotVersionConflict):
            self.dm.process("水深300米")

        self.assertEqual(self.dm.slot_store.version, initial_ver)
        self.assertEqual(len(self.dm.conversation_history), initial_hist_len)

    # 22. expected_version 不一致时抛出 SlotVersionConflict
    def test_22_version_mismatch_raises_slot_version_conflict(self):
        store = SlotStore(self.kb)
        snap_slots, snap_unresolved, snap_ver = store.snapshot()
        with self.assertRaises(SlotVersionConflict):
            store.commit_transaction(snap_slots, snap_unresolved, expected_version=snap_ver + 999)

    # 23. 两个线程基于相同 version 提交时，不允许旧数据覆盖新数据 (并发测试)
    def test_23_concurrency_optimistic_lock(self):
        store = SlotStore(self.kb)
        store.slots["water_depth"] = Slot("water_depth", value=None, status="missing")
        snap_slots1, snap_unresolved1, ver1 = store.snapshot()
        snap_slots2, snap_unresolved2, ver2 = store.snapshot()

        snap_slots1["water_depth"].value = 300.0
        snap_slots1["water_depth"].status = "valid"
        store.commit_transaction(snap_slots1, snap_unresolved1, expected_version=ver1)

        snap_slots2["water_depth"].value = 999.0
        snap_slots2["water_depth"].status = "valid"
        with self.assertRaises(SlotVersionConflict):
            store.commit_transaction(snap_slots2, snap_unresolved2, expected_version=ver2)

        self.assertEqual(store.slots["water_depth"].value, 300.0)

    # 24. 历史快照完整恢复
    def test_24_history_snapshot_full_restoration(self):
        store = SlotStore(self.kb)
        store.slots["water_depth"] = Slot("water_depth", value=300.0, status="valid", version=2)
        store.version = 5
        snap = store.export_snapshot()

        new_store = SlotStore.from_snapshot(snap, self.kb)
        self.assertEqual(new_store.version, 5)
        self.assertEqual(new_store.slots["water_depth"].value, 300.0)

    # 25. legacy 快照转换恢复
    def test_25_legacy_snapshot_conversion(self):
        legacy_snap = {
            "task_state": {"task_type": "管缆巡检", "water_depth": 400.0},
            "conversation_history": [],
            "mode": "normal",
            "phase": "collecting"
        }
        self.dm.load_snapshot(legacy_snap)
        self.assertEqual(self.dm.task_state.get("water_depth"), 400.0)
        assert_ssot_consistency(self, self.dm)

    # 26. 非法快照恢复失败后，原会话全部状态保持不变 (Test C)
    def test_26_test_c_invalid_snapshot_restoration_leaves_state_untouched(self):
        self.dm.reset()
        self.dm.slot_store.slots["water_depth"] = Slot("water_depth", value=250.0, status="valid")
        self.dm.slot_store.version = 3
        self.dm.mode = "normal"
        self.dm.phase = "collecting"
        self.dm.task_state = self.dm.slot_store.get_task_state()
        self.dm._last_built_json = self.dm.slot_store.get_built_json()
        initial_hist = list(self.dm.conversation_history)

        invalid_snapshot = {
            "store_version": -10,  # 非法 store_version
            "slots": {},
            "unresolved": []
        }

        with self.assertRaises(SnapshotValidationError):
            self.dm.load_snapshot(invalid_snapshot)

        # 验证全部状态 100% 保持恢复前的原样
        self.assertEqual(self.dm.slot_store.version, 3)
        self.assertEqual(self.dm.slot_store.slots["water_depth"].value, 250.0)
        self.assertEqual(self.dm.mode, "normal")
        self.assertEqual(self.dm.phase, "collecting")
        self.assertEqual(self.dm.conversation_history, initial_hist)
        assert_ssot_consistency(self, self.dm)

    # 27. 快照恢复路径不存在直接 self.slot_store.slots 赋值
    def test_27_no_direct_slots_assignment(self):
        snap = {
            "slot_store": {
                "store_version": 1,
                "slots": {
                    "water_depth": {"slot_name": "water_depth", "value": 100.0, "status": "valid", "version": 1}
                },
                "unresolved": []
            },
            "mode": "normal",
            "phase": "collecting",
            "conversation_history": []
        }
        orig_store = self.dm.slot_store
        self.dm.load_snapshot(snap)
        # 验证 load_snapshot 创建了全新的 SlotStore 对象，而非在原对象的 .slots 上直接赋值
        self.assertIsNot(self.dm.slot_store, orig_store)
        assert_ssot_consistency(self, self.dm)

    # 28. ASR 文本和直接文本经过相同槽位流水线
    def test_28_asr_text_and_direct_text_same_pipeline(self):
        web_backend._shared_asr = MagicMock()
        web_backend._shared_asr.transcribe_file.return_value = {
            "text": "我要执行管缆巡检",
            "language_hint": "zh",
            "device": "cpu",
            "elapsed_ms": 10.0,
            "segments": []
        }
        web_backend._shared_llm.extract_json.return_value = {
            "intent": "TASK_CREATE",
            "slot_candidates": [
                {"raw_key": "任务类型", "canonical_key": "task_type", "raw_value": "管缆巡检", "normalized_value": "管缆巡检", "confidence": 1.0}
            ],
            "unresolved": []
        }

        data_file = (io.BytesIO(b"dummy wav audio data"), "test.wav")
        res_asr = self.client.post("/api/asr", data={"audio": data_file}, content_type="multipart/form-data")
        self.assertEqual(res_asr.status_code, 200)
        corrected_text = res_asr.get_json()["corrected_text"]

        res_chat_a = self.client.post("/api/chat", json={"session_id": "sess_pipeline_a", "message": corrected_text})
        data_a = res_chat_a.get_json()

        res_chat_b = self.client.post("/api/chat", json={"session_id": "sess_pipeline_b", "message": "我要执行管缆巡检"})
        data_b = res_chat_b.get_json()

        coll_a = {k: v for k, v in data_a["collected"].items() if k != "task_id"}
        coll_b = {k: v for k, v in data_b["collected"].items() if k != "task_id"}
        self.assertEqual(coll_a, coll_b)

    # 29. /api/chat 返回 409 时包含 request_id
    def test_29_api_chat_409_includes_request_id(self):
        mgr = web_backend.get_or_create_manager("sess_409_test")
        mgr.process = MagicMock(side_effect=SlotVersionConflict("Conflict simulation"))

        res = self.client.post("/api/chat", json={"session_id": "sess_409_test", "message": "并发冲突"})
        self.assertEqual(res.status_code, 409)
        data = res.get_json()
        self.assertEqual(data["code"], 409)
        self.assertEqual(data["error"], "SlotVersionConflict")
        self.assertIn("request_id", data)

    # 30. /api/chat 返回 500 时不泄露 traceback、文件路径或模型信息
    def test_30_api_chat_500_hides_traceback_and_paths(self):
        mgr = web_backend.get_or_create_manager("sess_500_test")
        mgr.process = MagicMock(side_effect=RuntimeError("Secret path leak: /root/private/model_weights.bin"))

        res = self.client.post("/api/chat", json={"session_id": "sess_500_test", "message": "触发500"})
        self.assertEqual(res.status_code, 500)
        data = res.get_json()
        self.assertEqual(data["code"], 500)
        self.assertEqual(data["msg"], "服务器内部错误，请稍后重试。")
        self.assertIn("request_id", data)
        self.assertNotIn("Traceback", data["msg"])
        self.assertNotIn("/root/private", data["msg"])

    # 31. 前端刷新/历史恢复后 collected、missing、task_type 一致
    def test_31_frontend_refresh_and_history_load_consistency(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            with patch("src.history_manager.get_history_dir", return_value=tmp_path):
                self.dm.reset()
                self.dm.slot_store.slots["task_type_key"] = Slot("task_type_key", value="pipeline_inspection", status="valid")
                self.dm.slot_store.slots["task_type"] = Slot("task_type", value="管缆巡检", status="valid")
                self.dm.slot_store.slots["water_depth"] = Slot("water_depth", value=350.0, status="valid")

                filename = save_conversation(
                    session_id="sess_ui_refresh",
                    conversation_history=[],
                    task_state=self.dm.slot_store.get_task_state(),
                    built_json=self.dm.slot_store.get_built_json(),
                    mode=self.dm.mode,
                    phase=self.dm.phase,
                    slot_store=self.dm.slot_store.export_snapshot()
                )

                res = self.client.post("/api/history/load", json={"history_id": filename, "session_id": "sess_ui_target"})
                self.assertEqual(res.status_code, 200)
                data = res.get_json()
                self.assertEqual(data["task_type"], "pipeline_inspection")
                self.assertEqual(data["built_json"]["water_depth"], 350.0)
                self.assertIsNotNone(data["missing"])


if __name__ == "__main__":
    unittest.main()
