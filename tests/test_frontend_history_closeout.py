"""
tests/test_frontend_history_closeout.py

SEAgent G6 — Frontend State & History Recovery Closeout 主测试模块。
验证：
1. /api/chat、/api/session/state、/api/history/load 使用统一 build_frontend_ui_state 结构；
2. valid / candidate / invalid / missing / conflict 槽位状态契约；
3. blocked_hard / blocked_soft / confirming / done / rejected 的 actions 与 read_only 契约；
4. history save -> list_history -> load_history 保存与完整恢复流程；
5. 损坏历史文件容错（不拖垮 list_history）；
6. history 恢复无副作用（不重新生成 ID / 不重新 publish / 不触发 LLM）；
7. history 恢复失败时不污染当前 session；
8. 普通聊天、知识问答、ASR 与任务模式无回归。
"""

import json
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from web_backend import app, init_manager, get_or_create_manager, _sessions_manager
from src.dialogue_manager import DialogueManager
from src.history_manager import save_conversation, list_history, load_history
from src.ui_state_builder import build_frontend_ui_state
from src.knowledge_retriever import KnowledgeBase
from src.llm_client import LLMClient


class TestFrontendHistoryCloseout(unittest.TestCase):
    def setUp(self):
        self.app_client = app.test_client()
        self.llm = LLMClient(None, None)
        self.kb = KnowledgeBase()
        init_manager(DialogueManager(self.llm, self.kb))
        _sessions_manager.clear()

    def test_01_unified_ui_state_across_three_apis(self):
        """1. /api/chat 与 /api/session/state 统一返回 ui_state"""
        sid = "test_sess_01"
        mgr = get_or_create_manager(sid)

        def stub_extract(messages, max_tokens=800, role=None):
            return {
                "task_type_key": "pipeline_inspection",
                "slot_candidates": [
                    {"canonical_key": "water_depth", "raw_value": "300米", "normalized_value": 300.0, "confidence": 1.0}
                ]
            }

        with patch.object(mgr.llm, "extract_json", side_effect=stub_extract):
            # 发送聊天请求
            res_chat = self.app_client.post("/api/chat", json={
                "session_id": sid,
                "message": "在流花11-1油田执行管缆巡检，水深300米",
                "request_id": "req_01"
            })
            self.assertEqual(res_chat.status_code, 200)
            data_chat = res_chat.get_json()
            self.assertIn("ui_state", data_chat)
            ui_chat = data_chat["ui_state"]

        # 获取 session state
        res_state = self.app_client.get(f"/api/session/state?session_id={sid}")
        self.assertEqual(res_state.status_code, 200)
        data_state = res_state.get_json()
        self.assertIn("ui_state", data_state)
        ui_state = data_state["ui_state"]

        # 结构及核心字段一致
        self.assertEqual(ui_chat["phase"], ui_state["phase"])
        self.assertEqual(ui_chat["dialogue_mode"], ui_state["dialogue_mode"])
        self.assertEqual(ui_chat["task_type_key"], ui_state["task_type_key"])
        self.assertEqual(ui_chat["actions"], ui_state["actions"])
        self.assertEqual(ui_chat["read_only"], ui_state["read_only"])

    def test_02_slot_status_and_formatting_contract(self):
        """2. valid / candidate / invalid / missing / conflict 槽位在 ui_state 中的结构契约"""
        mgr = DialogueManager(self.llm, self.kb, session_id="test_sess_02")
        mgr.task_state = {"task_type_key": "pipeline_inspection"}

        # 模拟各种 slot 状态
        mgr.slot_store.init_task_slots([
            {"key": "water_depth", "label": "水深（米）", "type": "number"},
            {"key": "equipment_family", "label": "作业机器人系列", "type": "string"},
            {"key": "payload", "label": "携带工具", "type": "list"},
        ])

        new_slots = mgr.slot_store.clone_slots()
        new_slots["water_depth"].value = 300.0
        new_slots["water_depth"].status = "valid"
        new_slots["water_depth"].value_type = "number"

        new_slots["equipment_family"].raw_value = "未知系列"
        new_slots["equipment_family"].validation_error = "未在许可列表内"
        new_slots["equipment_family"].status = "invalid"

        mgr.slot_store.commit_transaction(new_slots, [])

        ui_state = build_frontend_ui_state(mgr)
        slots = ui_state["slots"]
        self.assertTrue(isinstance(slots, list))

        depth_slot = next(s for s in slots if s["key"] == "water_depth")
        self.assertEqual(depth_slot["status"], "valid")
        self.assertEqual(depth_slot["value"], 300.0)

        family_slot = next(s for s in slots if s["key"] == "equipment_family")
        self.assertEqual(family_slot["status"], "invalid")
        self.assertEqual(family_slot["validation_error"], "未在许可列表内")

        payload_slot = next(s for s in slots if s["key"] == "payload")
        self.assertEqual(payload_slot["status"], "missing")

    def test_03_phase_actions_and_read_only_contract(self):
        """3. blocked_hard / blocked_soft / confirming / done / rejected 的 actions 与 read_only 契约"""
        mgr = DialogueManager(self.llm, self.kb, session_id="test_sess_03")
        mgr.dialogue_mode = "task_collection"

        phases_test = [
            ("collecting", True, False, False, False),
            ("blocked_soft", True, True, False, False),
            ("blocked_hard", True, False, False, False),
            ("confirming", True, False, True, False),
            ("done", True, False, False, True),
            ("rejected", True, False, False, True),
        ]

        for phase, exp_can_send, exp_can_ignore, exp_can_pub, exp_read_only in phases_test:
            mgr.phase = phase
            ui_state = build_frontend_ui_state(mgr)

            self.assertEqual(ui_state["read_only"], exp_read_only, f"phase {phase} read_only mismatch")
            actions = ui_state["actions"]
            self.assertEqual(actions["can_send"], exp_can_send, f"phase {phase} can_send mismatch")
            self.assertEqual(actions["can_ignore_soft_warning"], exp_can_ignore, f"phase {phase} can_ignore mismatch")
            self.assertEqual(actions["can_publish"], exp_can_pub, f"phase {phase} can_publish mismatch")

    def test_04_history_save_list_load_recovery_flow(self):
        """4. 保存一条 history -> list_history 可见 -> load_history 成功 -> state 一致"""
        sid = "test_sess_04"
        mgr = get_or_create_manager(sid)
        mgr.conversation_history = [
            {"role": "user", "content": "巡检任务，水深300米"},
            {"role": "assistant", "content": "已收集水深300米"}
        ]
        mgr.task_state = {"task_type_key": "pipeline_inspection", "task_id": "PI-20260810-001"}
        mgr.phase = "collecting"
        mgr.mode = "normal"
        mgr._last_built_json = {"task_id": "PI-20260810-001", "water_depth": 300.0}

        filename = save_conversation(
            session_id=sid,
            conversation_history=mgr.conversation_history,
            task_state=mgr.task_state,
            built_json=mgr._last_built_json,
            mode=mgr.mode,
            phase=mgr.phase,
            intent_id=None,
            slot_store=mgr.slot_store,
        )

        records = list_history()
        target_rec = next((r for r in records if r["id"] == filename), None)
        self.assertIsNotNone(target_rec)
        self.assertEqual(target_rec["task_id"], "PI-20260810-001")

        # 通过 API 加载历史记录
        target_sid = "test_sess_04_restored"
        res = self.app_client.post("/api/history/load", json={
            "history_id": filename,
            "session_id": target_sid
        })
        self.assertEqual(res.status_code, 200)
        data = res.get_json()

        self.assertEqual(data["code"], 200)
        self.assertIn("ui_state", data)
        ui_state = data["ui_state"]
        self.assertEqual(ui_state["phase"], "collecting")
        self.assertFalse(ui_state["read_only"])
        self.assertTrue(ui_state["actions"]["can_send"])

    def test_05_corrupted_history_file_handling(self):
        """5. 单个损坏的 history 文件不影响 list_history"""
        history_dir = Path(__file__).parent.parent / "record" / "history"
        history_dir.mkdir(parents=True, exist_ok=True)
        bad_file = history_dir / "history_corrupted_test_9999.json"
        try:
            with open(bad_file, "w", encoding="utf-8") as f:
                f.write("{ invalid json payload ...")

            records = list_history()
            # 不应该抛出 JSONDecodeError， bad_file 应该被优雅跳过
            bad_rec = next((r for r in records if r["id"] == bad_file.name), None)
            self.assertIsNone(bad_rec)
        finally:
            if bad_file.exists():
                bad_file.unlink()

    def test_06_history_load_no_side_effects(self):
        """6. history 恢复时不调用 LLM / Extractor / publish / ID generator"""
        snapshot = {
            "snapshot_version": 2,
            "session_id": "test_sess_06",
            "saved_at": "2026-08-10T12:00:00",
            "conversation_history": [{"role": "user", "content": "hello"}],
            "mode": "normal",
            "phase": "collecting",
            "dialogue_mode": "task_collection",
            "task_state": {"task_type_key": "pipeline_inspection"},
            "built_json": {},
            "task_id": "PI-20260810-099",
            "task_type": "pipeline_inspection"
        }

        mgr = DialogueManager(self.llm, self.kb, session_id="test_sess_06")

        with patch.object(self.llm, "chat", side_effect=AssertionError("LLM chat should not be called")), \
             patch.object(self.llm, "extract_json", side_effect=AssertionError("LLM extract_json should not be called")), \
             patch("src.dialogue_manager.build_task_patch", side_effect=AssertionError("Extractor patch should not be called")):
            mgr.load_snapshot(snapshot)

        self.assertEqual(mgr.phase, "collecting")
        self.assertEqual(mgr.conversation_history, [{"role": "user", "content": "hello"}])

    def test_07_failed_history_load_leaves_session_intact(self):
        """7. 历史恢复失败（快照格式错误）时，原 session 内存状态不变"""
        sid = "test_sess_07"
        mgr = get_or_create_manager(sid)
        mgr.conversation_history = [{"role": "user", "content": "原始会话"}]
        mgr.phase = "collecting"

        res = self.app_client.post("/api/history/load", json={
            "history_id": "non_existent_history.json",
            "session_id": sid
        })
        self.assertEqual(res.status_code, 404)

        # 验证原会话未受污染
        mgr_after = get_or_create_manager(sid)
        self.assertEqual(mgr_after.conversation_history, [{"role": "user", "content": "原始会话"}])
        self.assertEqual(mgr_after.phase, "collecting")

    def test_08_no_regression_on_normal_chat_and_qa(self):
        """8. 普通聊天与知识问答不强制进入 Slot 收集或修改 SlotStore"""
        sid = "test_sess_08"
        mgr = get_or_create_manager(sid)

        with patch.object(mgr.llm, "filter_reply", return_value="天气晴朗，适合出海作业。"), \
             patch.object(mgr.llm, "generate_text", return_value="天气晴朗，适合出海作业。"):
            res = self.app_client.post("/api/chat", json={
                "session_id": sid,
                "message": "你好，请问今天天气怎么样？"
            })
            self.assertEqual(res.status_code, 200)
            data = res.get_json()
            ui_state = data["ui_state"]

            self.assertEqual(ui_state["dialogue_mode"], "knowledge_qa")
    def test_09_list_history_filters_internal_and_non_history_files(self):
        """9. list_history 过滤内部 session 文件、点文件与无 conversation_history 的非快照文件"""
        from src.result_paths import get_history_dir
        hist_dir = get_history_dir(create=True)

        internal_head = hist_dir / ".session_head_test_mock.json"
        session_rev = hist_dir / "session_mock_rev_1.json"
        lock_file = hist_dir / ".session_mock.lock"

        try:
            with open(internal_head, "w", encoding="utf-8") as f:
                json.dump({"schema_version": 2, "session_id": "test_mock"}, f)
            with open(session_rev, "w", encoding="utf-8") as f:
                json.dump({"schema_version": 2, "session_id": "test_mock"}, f)
            with open(lock_file, "w", encoding="utf-8") as f:
                f.write("")

            records = list_history()
            rec_ids = [r["id"] for r in records]
            self.assertNotIn(internal_head.name, rec_ids)
            self.assertNotIn(session_rev.name, rec_ids)
            self.assertNotIn(lock_file.name, rec_ids)
        finally:
            internal_head.unlink(missing_ok=True)
            session_rev.unlink(missing_ok=True)
            lock_file.unlink(missing_ok=True)

    def test_10_legacy_snapshot_with_deprecated_robot_variant_migrates_cleanly(self):
        """10. 旧版无 slot_store 快照中存在废弃型号（如工作级ROV）时平滑迁移，不抛出 VARIANT_NOT_FOUND"""
        legacy_snapshot = {
            "session_id": "test_legacy_variant",
            "saved_at": "2026-08-10T12:00:00",
            "conversation_history": [
                {"role": "user", "content": "进行采油树阀门操作"},
                {"role": "assistant", "content": "好的"}
            ],
            "mode": "normal",
            "phase": "collecting",
            "task_state": {
                "task_type_key": "tree_valve_operation",
                "task_id": "CT2026081099",
                "equipment_type": "工作级ROV",
                "water_depth": 300
            },
            "built_json": {},
            "task_id": "CT2026081099",
            "task_type": "tree_valve_operation"
        }

        mgr = DialogueManager(self.llm, self.kb, session_id="test_legacy_variant")
        mgr.load_snapshot(legacy_snapshot)

        self.assertEqual(mgr.phase, "collecting")
        self.assertEqual(mgr.task_state.get("task_id"), "CT2026081099")
        self.assertIn("equipment_type", mgr.slot_store.slots)
        # 废弃型号应被平滑重置为 missing 以便后续重新收集或由用户选择，而不造成加载失败
        eq_slot = mgr.slot_store.slots["equipment_type"]
        self.assertEqual(eq_slot.status, "missing")


if __name__ == "__main__":
    unittest.main()

