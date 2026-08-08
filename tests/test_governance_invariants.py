"""
tests/test_governance_invariants.py

SEAgent G0.1 Governance Baseline Invariant Tests.
测试并验证系统不可退化的 11 大系统不变量 (INV-01 ~ INV-11) 及控制分流、真正写盘与自动生成逻辑。
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

    def test_inv02_real_write_path_task_create(self):
        """INV-02: 走真实 DM -> Router -> Extractor -> Normalizer -> SlotStore 链路完成任务类型建单。"""
        v_before = self.dm.slot_store.version

        def stub_llm_extract_json(messages, max_tokens=None):
            return {
                "slot_candidates": [
                    {
                        "canonical_key": "task_type",
                        "normalized_value": "管缆巡检",
                        "raw_value": "管缆巡检",
                        "confidence": 1.0,
                        "resolution_method": "canonical_exact",
                    }
                ]
            }

        with patch.object(self.dm.llm, "extract_json", side_effect=stub_llm_extract_json):
            self.dm.process("创建一个管缆巡检任务", request_id="req_inv02_create")

        self.assertGreater(self.dm.slot_store.version, v_before)
        self.assertEqual(self.dm.slot_store.get_task_state().get("task_type"), "管缆巡检")
        self.assertEqual(self.dm.slot_store.get_task_state().get("task_type_key"), "pipeline_inspection")

    def test_inv02_real_write_path_water_depth(self):
        """INV-02: 走真实 DM WRITE 链路更新水深为规范化数值 300.0。"""
        schema = self.dm.builder.get_schema("pipeline_inspection", self.dm.mode)
        self.dm.slot_store.init_task_slots(schema)
        slots = self.dm.slot_store.clone_slots()
        slots["task_type"] = Slot("task_type", value="管缆巡检", status="valid")
        slots["task_type_key"] = Slot("task_type_key", value="pipeline_inspection", status="valid")
        self.dm.slot_store.commit_transaction(slots, [])
        self.dm.task_state = self.dm.slot_store.get_task_state()

        v_before = self.dm.slot_store.version

        def stub_llm_extract_json(messages, max_tokens=None):
            return {
                "slot_candidates": [
                    {
                        "canonical_key": "water_depth",
                        "normalized_value": "300",
                        "raw_value": "300米",
                        "confidence": 0.95,
                        "resolution_method": "regex_rule",
                    }
                ]
            }

        with patch.object(self.dm.llm, "extract_json", side_effect=stub_llm_extract_json):
            self.dm.process("水深300米", request_id="req_inv02_depth")

        self.assertGreater(self.dm.slot_store.version, v_before)
        self.assertEqual(self.dm.slot_store.get_task_state().get("water_depth"), 300.0)

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

    def test_inv04_invalid_input_never_overwrites_valid_fact(self):
        """INV-04: 真实 Pipeline 端到端验证：非法新输入绝不作为正式事实写入/覆盖已有 valid 事实 (SlotStore.get_task_state())。"""
        schema = self.dm.builder.get_schema("pipeline_inspection", self.dm.mode)
        self.dm.slot_store.init_task_slots(schema)

        slots = self.dm.slot_store.clone_slots()
        slots["task_type"] = Slot("task_type", value="管缆巡检", status="valid")
        slots["task_type_key"] = Slot("task_type_key", value="pipeline_inspection", status="valid")
        slots["water_depth"] = Slot("water_depth", value=300.0, status="valid")
        self.dm.slot_store.commit_transaction(slots, [])
        self.dm.task_state = self.dm.slot_store.get_task_state()

        self.assertEqual(self.dm.slot_store.get_task_state().get("water_depth"), 300.0)

        def stub_llm_extract_json(messages, max_tokens=None):
            return {
                "slot_candidates": [
                    {
                        "canonical_key": "water_depth",
                        "normalized_value": "300abc",
                        "raw_value": "差不多很深",
                        "confidence": 0.9,
                        "resolution_method": "llm_semantic",
                    }
                ]
            }

        with patch.object(self.dm.llm, "extract_json", side_effect=stub_llm_extract_json):
            self.dm.process("水深改成差不多很深", request_id="req_inv04_invalid")

        state_after = self.dm.slot_store.get_task_state()
        self.assertNotIn("water_depth", state_after)

        slot = self.dm.slot_store.slots.get("water_depth")
        self.assertEqual(slot.value, 300.0)
        self.assertEqual(slot.candidate_value, "300abc")
        self.assertEqual(slot.raw_value, "差不多很深")
        self.assertEqual(slot.status, "conflict")
        self.assertIsNotNone(slot.validation_error)

    def test_inv05_hard_cannot_be_bypassed(self):
        """INV-05: blocked_hard 状态下，确认/继续/忽略警告无法绕过硬约束。"""
        self.dm.phase = "blocked_hard"
        mock_violation = MagicMock()
        mock_violation.severity = "hard"
        mock_violation.constraint_id = "HARD_TEST_01"
        mock_violation.message = "测试硬违规水深"
        self.dm._blocking_violations = [mock_violation]

        bypass_words = ["确认", "继续", "忽略警告", "没问题", "好的", "ok"]
        for word in bypass_words:
            reply = self.dm.process(word, request_id="req_inv05")
            self.assertEqual(self.dm.phase, "blocked_hard")
            self.assertIn("硬性约束不能通过确认或忽略警告绕过", reply)

    def test_inv06_soft_ack_is_distinct(self):
        """INV-06: blocked_soft 状态下，明确忽略软告警生成 ValidationAcknowledgement 绑定快照。"""
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

        self.dm.process("忽略警告", request_id="req_inv06")

        acks = self.dm.slot_store.validation_acknowledgements
        self.assertTrue(len(acks) > 0)

        ack = acks[0]
        self.assertEqual(ack.constraint_id, "SOFT_TEST_01")
        self.assertIsNotNone(ack.validation_fingerprint)
        self.assertIsNotNone(ack.task_version)

    def test_inv07_publish_fail_closed(self):
        """INV-07: 发布链路失败时 Fail-Closed：还原内存快照，phase 不为 done。"""
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
                        self.dm._handle_final_publish_confirmation("确认发布", request_id="req_inv07")

        self.assertNotEqual(self.dm.phase, "done")
        self.assertEqual(self.dm.phase, "confirming")
        self.assertIsNone(self.dm.final_result)

    def test_publish_success_path(self):
        """测试发布成功路径：真实 prepare -> create_staging -> publish_staging -> final 文件生成且 phase=done。"""
        now_str = get_current_datetime().strftime("%Y-%m-%d %H:%M:%S")

        valid_state = {
            "task_id": "PI-20260810-001",
            "internal_id": "88888888-8888-4888-8888-888888888888",
            "intent_id": "TI20260810001",
            "task_type": "管缆巡检",
            "task_type_key": "pipeline_inspection",
            "equipment_class": "observation_rov",
            "equipment_family": "观察级深海机器人",
            "equipment_specification": {"value": "观察级深海机器人", "unit": None},
            "equipment_unit_id": "OBSROV--001",
            "equipment_type": "observation_rov",
            "cable_type": "电力缆",
            "start_point": {"lat": 20.0, "lon": 110.0},
            "end_point": {"lat": 20.1, "lon": 110.1},
            "payload": ["高清摄像机"],
            "water_depth": 300,
            "support_vessel": "海洋石油681",
            "oilfield_name": "东方1-1油田",
            "start_time": now_str,
            "end_time": "2099-01-01 18:00:00",
        }
        slots = self.dm.slot_store.clone_slots()
        for k, v in valid_state.items():
            slots[k] = Slot(k, value=v, status="valid")
        self.dm.slot_store.commit_transaction(slots, [])
        self.dm.task_state = self.dm.slot_store.get_task_state()
        self.dm._last_built_json = dict(valid_state)
        self.dm.phase = "confirming"

        mock_snap = {"state_version": 0, "status_ref": "OBSROV-001"}
        mock_val_res = ValidationResult(
            overall_status="valid",
            validated_at=now_str,
            task_version=1,
            validation_version=1,
            validation_fingerprint="fp_test",
            state_snapshot=mock_snap,
            violations=[],
        )

        with patch("src.task_intent_builder.get_task_dir", return_value=self.task_dir):
            with patch.object(self.dm.kb.state_info, "check_runtime_availability", return_value={"available": True}):
                with patch.object(self.dm.kb, "get_unit_state_snapshot", return_value=mock_snap):
                    with patch.object(self.dm, "_refresh_validation", return_value=mock_val_res):
                        with patch.object(self.dm.slot_store, "get_missing_slots", return_value=[]):
                            reply = self.dm._handle_final_publish_confirmation("确认发布", request_id="req_pub_succ")

        self.assertEqual(self.dm.phase, "done")
        self.assertIsNotNone(self.dm.final_result)
        final_file = self.task_dir / "task_intent_TI20260810001.json"
        self.assertTrue(final_file.exists())

        data = json.loads(final_file.read_text(encoding="utf-8"))
        self.assertEqual(data.get("intent_id"), "TI20260810001")
        self.assertEqual(data.get("schema_version"), 2)

    def test_inv08_duplicate_confirm_is_idempotent(self):
        """INV-08: 任务处于 done 阶段时，再次“确认”或“确认发布”幂等响应，无二次写盘。"""
        self.dm.phase = "done"
        self.dm.task_state["intent_id"] = "TI20260810999"
        self.dm._last_built_json = {"intent_id": "TI20260810999"}

        with patch.object(TaskIntentBuilder, "publish_staging") as mock_pub:
            reply_1 = self.dm.process("确认", request_id="req_inv08_1")
            reply_2 = self.dm.process("确认发布", request_id="req_inv08_2")

            self.assertIn("无需重复发布", reply_1)
            self.assertIn("无需重复发布", reply_2)
            mock_pub.assert_not_called()

    def test_inv09_session_isolation(self):
        """INV-09: 不同 session_id 的 DialogueManager 与 SlotStore 彻底物理隔离。"""
        dm_a = get_or_create_manager("sess_a")
        dm_b = get_or_create_manager("sess_b")

        slots_a = dm_a.slot_store.clone_slots()
        slots_a["task_type"] = Slot("task_type", value="管缆巡检", status="valid")
        dm_a.slot_store.commit_transaction(slots_a, [])
        dm_a.task_state = dm_a.slot_store.get_task_state()

        self.assertIn("task_type", dm_a.task_state)
        self.assertNotIn("task_type", dm_b.task_state)
        self.assertNotEqual(dm_a.slot_store, dm_b.slot_store)

    def test_inv10_final_no_overwrite(self):
        """INV-10: 目标 final 文件已存在时，拒绝无条件覆盖并抛出 IntentIdConflict。"""
        task_dir = self.task_dir
        with patch("src.task_intent_builder.get_task_dir", return_value=task_dir):
            ti_builder = TaskIntentBuilder(self.dm.kb)
            intent_id = "TI20260810001"
            final_file = task_dir / f"task_intent_{intent_id}.json"
            final_file.write_text(json.dumps({"existing": True}), encoding="utf-8")

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

            content = json.loads(final_file.read_text(encoding="utf-8"))
            self.assertTrue(content.get("existing"))

    def test_inv11_request_traceability_explicit(self):
        """INV-11 Path A: 客户端显式传入 request_id 时，透传至 mgr.process 且与 API 响应完全一致。"""
        client = app.test_client()

        with patch.object(DialogueManager, "process", return_value="ok") as mock_proc:
            res = client.post("/api/chat", json={
                "session_id": "sess_trace_exp",
                "request_id": "req_custom_12345",
                "message": "测试透传",
            })
            self.assertEqual(res.status_code, 200)
            data = res.get_json()
            self.assertEqual(data.get("request_id"), "req_custom_12345")
            mock_proc.assert_called_once_with("测试透传", request_id="req_custom_12345")

    def test_inv11_request_traceability_auto_generated(self):
        """INV-11 Path B: 客户端未传入 request_id 时，API 自动生成合法 ID 并透传至 mgr.process。"""
        client = app.test_client()

        with patch.object(DialogueManager, "process", return_value="ok") as mock_proc:
            res = client.post("/api/chat", json={
                "session_id": "sess_trace_auto",
                "message": "自动生成测试",
            })
            self.assertEqual(res.status_code, 200)
            data = res.get_json()
            auto_req_id = data.get("request_id")
            self.assertTrue(auto_req_id and auto_req_id.startswith("req_"))
            mock_proc.assert_called_once_with("自动生成测试", request_id=auto_req_id)

    def test_real_task_modify_flow(self):
        """测试真实 task_modify 流程：水深从 300.0 修改为 500.0。"""
        schema = self.dm.builder.get_schema("pipeline_inspection", self.dm.mode)
        self.dm.slot_store.init_task_slots(schema)
        slots = self.dm.slot_store.clone_slots()
        slots["task_type"] = Slot("task_type", value="管缆巡检", status="valid")
        slots["task_type_key"] = Slot("task_type_key", value="pipeline_inspection", status="valid")
        slots["water_depth"] = Slot("water_depth", value=300.0, status="valid")
        self.dm.slot_store.commit_transaction(slots, [])
        self.dm.task_state = self.dm.slot_store.get_task_state()

        v_before = self.dm.slot_store.version

        mock_route = MagicMock()
        mock_route.dialogue_mode = "task_collection"

        mock_extract = {
            "slot_candidates": [
                {
                    "canonical_key": "water_depth",
                    "normalized_value": "500",
                    "raw_value": "500米",
                    "confidence": 0.95,
                    "resolution_method": "regex_rule",
                }
            ]
        }

        with patch.object(self.dm.intent_router, "route", return_value=mock_route):
            with patch.object(self.dm.extractor, "extract_updates", return_value=mock_extract):
                self.dm.process("水深改成500米", request_id="req_modify_500")

        self.assertGreater(self.dm.slot_store.version, v_before)
        self.assertEqual(self.dm.slot_store.get_task_state().get("water_depth"), 500.0)
        self.assertNotEqual(self.dm.phase, "done")
        self.assertIsNone(self.dm.final_result)

    def test_emergency_control_routing(self):
        """测试紧急控制指令路由与状态记录。"""
        mock_route = MagicMock()
        mock_route.dialogue_mode = "emergency_intervention"
        mock_route.emergency_action = "stop"
        mock_route.source = "rule"
        mock_route.confidence = 1.0
        mock_route.reason = "stop keyword"

        self.dm.phase = "done"
        with patch.object(self.dm.intent_router, "route", return_value=mock_route):
            reply = self.dm.process("立即停止当前任务", request_id="req_em_stop")

        self.assertEqual(self.dm.control_state, "stop_requested")
        self.assertIn("已识别针对已发布任务的控制指令【停止】", reply)

    def test_negative_control_request(self):
        """测试否定式控制请求："不要停止当前任务" 不得触发控制动作。"""
        reply_neg = self.dm.process("不要停止当前任务", request_id="req_neg_stop")
        self.assertEqual(self.dm.control_state, "idle")

    def test_query_control_distinction(self):
        """测试控制询问与硬控制动作的分流区别 (GC-31, GC-33)。"""
        reply_query = self.dm.process("如果停止当前任务会怎样？", request_id="req_gc31")
        self.assertEqual(self.dm.control_state, "idle")

    def test_kd02_kb_miss_currently_has_no_general_reasoning(self):
        """KD-02 Characterization Test: 当 KB found=False 时直接返回预设拒绝文案，未调用 General Reasoning 兜底。"""
        with patch.object(self.dm.kb, "execute_typed_query", return_value={"found": False, "reason": "no_match"}):
            reply = self.dm.process("什么是未知的概念？", request_id="req_kd02")
        self.assertIn("当前知识库未提供该信息", reply)

    def test_kd03_llm_template_currently_disables_thinking(self):
        """KD-03 Characterization Test: 验证 LLMClient.generate_text 模板渲染硬编码 enable_thinking=False。"""
        with patch("src.llm_client.SamplingParams", MagicMock()):
            mock_tok = MagicMock()
            mock_tok.apply_chat_template.return_value = "prompt"
            mock_llm = MagicMock()
            mock_output = MagicMock()
            mock_output.outputs = [MagicMock(text="response")]
            mock_llm.generate.return_value = [mock_output]

            llm = LLMClient(mock_llm, mock_tok)
            llm.generate_text([{"role": "user", "content": "hi"}])
            mock_tok.apply_chat_template.assert_called_once()
            _, kwargs = mock_tok.apply_chat_template.call_args
            self.assertIn("enable_thinking", kwargs)
            self.assertFalse(kwargs["enable_thinking"])

    def test_governance_corpus_integrity(self):
        """验证 Golden Corpus 50 条测试案例的数据结构完整性与元数据覆盖度。"""
        self.assertEqual(len(GOVERNANCE_GOLDEN_CORPUS), 50)
        categories = {c["category"] for c in GOVERNANCE_GOLDEN_CORPUS}
        natures = {c["nature"] for c in GOVERNANCE_GOLDEN_CORPUS}

        self.assertIn("general_chat", categories)
        self.assertIn("persistence", categories)
        self.assertIn("emergency_control", categories)

        self.assertIn("invariant", natures)
        self.assertIn("expected_behavior", natures)
        self.assertIn("known_defect", natures)

    def test_governance_corpus_executable_invariants(self):
        """真实参数化执行 EXECUTABLE_GOVERNANCE_CASE_IDS 中的关键 Invariant 案例，断言系统不变量。"""
        executable_case_ids = {
            "GC-01",
            "GC-04",
            "GC-11",
            "GC-12",
            "GC-13",
            "GC-26",
            "GC-28",
            "GC-31",
            "GC-33",
            "GC-37",
            "GC-39",
        }
        cases_by_id = {c["id"]: c for c in GOVERNANCE_GOLDEN_CORPUS if c["id"] in executable_case_ids}
        self.assertEqual(len(cases_by_id), len(executable_case_ids))

        for case_id in sorted(executable_case_ids):
            case = cases_by_id[case_id]
            user_input = case["input"]
            v_before = self.dm.slot_store.version
            self.dm.control_state = "idle"
            self.dm.phase = "collecting"

            if case_id == "GC-28":
                self.dm.phase = "done"

            def stub_llm(messages, max_tokens=None):
                return {"slot_candidates": []}

            with patch.object(self.dm.llm, "extract_json", side_effect=stub_llm):
                reply = self.dm.process(user_input, request_id=f"req_exec_{case_id}")

            self.assertIsNotNone(reply)
            if case["nature"] == "invariant" and not case["should_publish"]:
                if case["category"] in ("general_chat", "general_knowledge", "project_fact"):
                    self.assertEqual(self.dm.slot_store.version, v_before)

            if case_id == "GC-01":
                self.assertEqual(self.dm.slot_store.version, v_before)
            elif case_id == "GC-28":
                self.assertEqual(self.dm.control_state, "stop_requested")
            elif case_id in ("GC-31", "GC-33"):
                self.assertEqual(self.dm.control_state, "idle")
