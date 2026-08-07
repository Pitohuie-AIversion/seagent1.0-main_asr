"""
tests/test_governance_invariants.py

SEAgent G0 Governance Baseline Invariant Tests.
测试并验证系统不可退化的 10 大系统不变量 (INV-01 ~ INV-10) 及控制分流边界。
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from web_backend import app, _sessions_manager, get_or_create_manager
from src.llm_client import LLMClient
from src.knowledge_retriever import KnowledgeBase
from src.dialogue_manager import DialogueManager
from src.slot_store import SlotStore, Slot
from src.validator import ValidationResult
from src.task_intent_builder import TaskIntentBuilder
from src.exceptions import IntentIdConflict, TaskPersistenceError
from src.simulated_time import get_current_datetime
from tests.fixtures.governance_corpus import GOVERNANCE_GOLDEN_CORPUS


def _make_dm(tmp_dir: Path) -> DialogueManager:
    state_file = tmp_dir / "state.yaml"
    shutil.copy("config/state.yaml", state_file)
    kb = KnowledgeBase()
    kb.state_info.state_file = state_file
    llm = LLMClient(None, None)
    return DialogueManager(llm, kb)


class TestGovernanceInvariants(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.tmp_path = Path(self._tmp)
        self.dm = _make_dm(self.tmp_path)
        self.task_dir = self.tmp_path / "task_intents"
        self.task_dir.mkdir(parents=True, exist_ok=True)
        _sessions_manager.clear()

    def tearDown(self):
        _sessions_manager.clear()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_inv01_query_read_only(self):
        """INV-01: QUERY 路径执行前后，SlotStore.version、export_snapshot()、task_state 不变。"""
        v_before = self.dm.slot_store.version
        snap_before = self.dm.slot_store.export_snapshot()
        state_before = dict(self.dm.task_state)

        # 发送问答查询
        reply = self.dm.process("什么是 DVL？", request_id="req_inv01")
        self.assertTrue(isinstance(reply, str) and len(reply) > 0)

        v_after = self.dm.slot_store.version
        snap_after = self.dm.slot_store.export_snapshot()
        state_after = self.dm.task_state

        self.assertEqual(v_before, v_after)
        self.assertEqual(snap_before, snap_after)
        self.assertEqual(state_before, state_after)
        self.assertNotEqual(self.dm.phase, "done")

    def test_inv02_write_only_mutates_task(self):
        """INV-02: 仅 WRITE 流程修改 SlotStore，常规闲聊/问答不更新槽位。"""
        v_before = self.dm.slot_store.version

        self.dm.process("你好", request_id="req_inv02_1")
        self.assertEqual(self.dm.slot_store.version, v_before)

        self.dm.process("AUV 和 ROV 有什么区别？", request_id="req_inv02_2")
        self.assertEqual(self.dm.slot_store.version, v_before)

        # 模拟合法槽位更新事务触发 SlotStore 变更
        slots = self.dm.slot_store.clone_slots()
        slots["task_type"] = Slot("task_type", value="管缆巡检", status="valid")
        self.dm.slot_store.commit_transaction(slots, [])
        self.assertGreater(self.dm.slot_store.version, v_before)

    def test_inv03_valid_slot_is_fact(self):
        """INV-03: SlotStore.get_task_state() 仅暴露 status == 'valid' 且 value != None 的槽位。"""
        slots = self.dm.slot_store.clone_slots()
        slots["test_valid"] = Slot("test_valid", value="val_1", status="valid")
        slots["test_cand"] = Slot("test_cand", value="val_2", status="candidate", candidate_value="val_2")
        slots["test_invalid"] = Slot("test_invalid", value="val_3", status="invalid")
        slots["test_none"] = Slot("test_none", value=None, status="valid")

        self.dm.slot_store.commit_transaction(slots, [])
        state = self.dm.slot_store.get_task_state()

        self.assertIn("test_valid", state)
        self.assertEqual(state["test_valid"], "val_1")
        self.assertNotIn("test_cand", state)
        self.assertNotIn("test_invalid", state)
        self.assertNotIn("test_none", state)

    def test_inv04_hard_cannot_be_bypassed(self):
        """INV-04: blocked_hard 状态下，确认/继续/忽略警告无法绕过硬约束。"""
        self.dm.phase = "blocked_hard"
        # 伪造一个硬告警
        mock_violation = MagicMock()
        mock_violation.severity = "hard"
        mock_violation.constraint_id = "HARD_TEST_01"
        mock_violation.message = "测试硬违规水深"
        self.dm._blocking_violations = [mock_violation]

        bypass_words = ["确认", "继续", "忽略警告", "没问题", "好的", "ok"]
        for word in bypass_words:
            reply = self.dm.process(word, request_id="req_inv04")
            self.assertEqual(self.dm.phase, "blocked_hard")
            self.assertIn("硬性约束不能通过确认或忽略警告绕过", reply)

    def test_inv05_soft_ack_is_distinct(self):
        """INV-05: blocked_soft 状态下，明确忽略软告警生成 ValidationAcknowledgement 绑定快照。"""
        self.dm.phase = "blocked_soft"
        mock_violation = MagicMock()
        mock_violation.severity = "soft"
        mock_violation.constraint_id = "SOFT_TEST_01"
        mock_violation.message = "水深过浅提示"
        mock_violation.related_fields = ["water_depth"]
        mock_violation.observed_value = 5.0
        self.dm._blocking_violations = [mock_violation]

        self.dm.task_state["water_depth"] = 5.0
        self.dm.task_state["task_type_key"] = "pipeline_inspection"

        # 用户确认忽略软告警
        self.dm.process("忽略警告", request_id="req_inv05")

        acks = self.dm.slot_store.validation_acknowledgements
        self.assertTrue(len(acks) > 0)

        ack = acks[0]
        self.assertEqual(ack.constraint_id, "SOFT_TEST_01")
        self.assertIsNotNone(ack.validation_fingerprint)
        self.assertIsNotNone(ack.task_version)

    def test_inv06_publish_fail_closed(self):
        """INV-06: 发布链路失败时 Fail-Closed：还原内存快照，phase 不为 done。"""
        now_str = get_current_datetime().strftime("%Y-%m-%d %H:%M:%S")

        slots = self.dm.slot_store.clone_slots()
        slots["intent_id"] = Slot("intent_id", value="TI20260810001", status="valid")
        slots["task_type_key"] = Slot("task_type_key", value="pipeline_inspection", status="valid")
        self.dm.slot_store.commit_transaction(slots, [])
        self.dm.task_state = self.dm.slot_store.get_task_state()
        self.dm._last_built_json = {"intent_id": "TI20260810001"}
        self.dm.phase = "confirming"

        mock_val_res = ValidationResult(
            overall_status="valid",
            validated_at=now_str,
            task_version=1,
            validation_version=1,
            validation_fingerprint="fp_test",
            state_snapshot={},
            violations=[],
        )

        with patch.object(self.dm, "_refresh_validation", return_value=mock_val_res):
            with patch.object(self.dm.slot_store, "get_missing_slots", return_value=[]):
                with patch.object(TaskIntentBuilder, "create_staging", side_effect=TaskPersistenceError("Disk Full Error")):
                    with self.assertRaises(TaskPersistenceError):
                        self.dm._handle_final_publish_confirmation("确认发布", request_id="req_inv06")

        # 验证 Fail-Closed 还原
        self.assertNotEqual(self.dm.phase, "done")
        self.assertEqual(self.dm.phase, "confirming")
        self.assertIsNone(self.dm.final_result)

    def test_inv07_duplicate_confirm_is_idempotent(self):
        """INV-07: 任务处于 done 阶段时，再次“确认”或“确认发布”幂等响应，无二次写盘。"""
        self.dm.phase = "done"
        self.dm.task_state["intent_id"] = "TI20260810999"
        self.dm._last_built_json = {"intent_id": "TI20260810999"}

        with patch.object(TaskIntentBuilder, "publish_staging") as mock_pub:
            reply_1 = self.dm.process("确认", request_id="req_inv07_1")
            reply_2 = self.dm.process("确认发布", request_id="req_inv07_2")

            self.assertIn("无需重复发布", reply_1)
            self.assertIn("无需重复发布", reply_2)
            mock_pub.assert_not_called()

    def test_inv08_session_isolation(self):
        """INV-08: 不同 session_id 的 DialogueManager 与 SlotStore 彻底物理隔离。"""
        dm_a = get_or_create_manager("sess_a")
        dm_b = get_or_create_manager("sess_b")

        slots_a = dm_a.slot_store.clone_slots()
        slots_a["task_type"] = Slot("task_type", value="管缆巡检", status="valid")
        dm_a.slot_store.commit_transaction(slots_a, [])
        dm_a.task_state = dm_a.slot_store.get_task_state()

        self.assertIn("task_type", dm_a.task_state)
        self.assertNotIn("task_type", dm_b.task_state)
        self.assertNotEqual(dm_a.slot_store, dm_b.slot_store)

    def test_inv09_final_no_overwrite(self):
        """INV-09: 目标 final 文件已存在时，拒绝无条件覆盖并抛出 IntentIdConflict。"""
        task_dir = self.task_dir
        with patch("src.task_intent_builder.get_task_dir", return_value=task_dir):
            ti_builder = TaskIntentBuilder(self.dm.kb)
            intent_id = "TI20260810001"
            final_file = task_dir / f"task_intent_{intent_id}.json"
            final_file.write_text(json.dumps({"existing": True}), encoding="utf-8")

            # 再次尝试发布相同的 intent_id
            dummy_artifact = {
                "schema_version": 2,
                "internal_id": "88888888-8888-4888-8888-888888888888",
                "task_id": "PI-20260810-001",
                "intent_id": intent_id,
                "task_type": "pipeline_inspection",
                "task_type_key": "pipeline_inspection",
                "priority": 7,
                "time": {"start": "2026-08-10 09:00:00", "end": "2026-08-10 18:00:00"},
                "location": {"oilfield": "东方1-1油田", "water_depth_m": 300},
                "task": {"type": "pipeline_inspection", "details": {}},
                "equipment": {"robot_type": "observation_rov", "payload": [], "support_vessel": "海洋石油681"},
                "conditions": {},
            }
            staging = ti_builder.create_staging(dummy_artifact)

            with self.assertRaises(IntentIdConflict):
                ti_builder.publish_staging(staging, dummy_artifact)

            # 验证原 final 文件内容未被破坏
            content = json.loads(final_file.read_text(encoding="utf-8"))
            self.assertTrue(content.get("existing"))

    def test_inv10_request_traceability(self):
        """INV-10: /api/chat 接收/生成的 request_id 真实透传至 DialogueManager.process()。"""
        client = app.test_client()

        with patch.object(DialogueManager, "process", return_value="ok") as mock_proc:
            res = client.post("/api/chat", json={
                "session_id": "sess_trace",
                "request_id": "req_custom_12345",
                "message": "测试透传",
            })
            self.assertEqual(res.status_code, 200)
            mock_proc.assert_called_once_with("测试透传", request_id="req_custom_12345")

    def test_query_control_distinction(self):
        """测试控制询问与硬控制动作的分流区别 (GC-31, GC-33)。"""
        # 1. 询问式："如果停止当前任务会怎样？" 应该作为 Query
        reply_query = self.dm.process("如果停止当前任务会怎样？", request_id="req_gc31")
        self.assertEqual(self.dm.control_state, "idle")

        # 2. 否定式："不要停止当前任务" 绝对不执行 stop
        reply_neg = self.dm.process("不要停止当前任务", request_id="req_gc33")
        self.assertEqual(self.dm.control_state, "idle")

    def test_golden_corpus_fixture_integrity(self):
        """校验 Governance Golden Corpus 语料库结构的完备性。"""
        self.assertEqual(len(GOVERNANCE_GOLDEN_CORPUS), 50)
        categories = {c["category"] for c in GOVERNANCE_GOLDEN_CORPUS}
        natures = {c["nature"] for c in GOVERNANCE_GOLDEN_CORPUS}

        self.assertIn("general_chat", categories)
        self.assertIn("persistence", categories)
        self.assertIn("emergency_control", categories)

        self.assertIn("invariant", natures)
        self.assertIn("known_defect", natures)
