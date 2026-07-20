"""
tests/test_intent_routing.py - 独立意图路由、控制指令隔离、深度查询、状态不变性及错误结构回归测试
"""

import copy
import json
import unittest
from unittest.mock import MagicMock, patch, PropertyMock

from src.dialogue_manager import DialogueManager, SOFT_IGNORE_KEYWORDS
from src.intent_router import IntentRouter, IntentRouteResult
from src.knowledge_retriever import KnowledgeBase
from src.llm_client import LLMClient
from src.validator import Violation
from src.slot_store import Slot
import web_backend


class DummyLLM(LLMClient):
    def __init__(self, default_reply: str = "默认LLM测试回复"):
        self.llm = None
        self.default_reply = default_reply
        self.called_chats = []

    def chat(self, messages: list[dict], temperature: float = 0.7, max_tokens: int = 800) -> str:
        self.called_chats.append(messages)
        res = super().chat(messages, temperature, max_tokens)
        if res and res != "收到您的信息，请继续补充任务描述。":
            return res
        return self.default_reply

    def generate(self, messages: list[dict], temperature: float = 0.7, max_tokens: int = 800) -> str:
        return self.chat(messages, temperature, max_tokens)

    def filter_reply(self, text: str) -> str:
        return text


def _seed_blocked_soft_dm(dm, kb):
    """设置 dm 进入 blocked_soft 状态且 water_depth=500.0"""
    dm.phase = "blocked_soft"
    dm.slot_store.slots["task_type_key"] = Slot("task_type_key", value="pipeline_inspection", status="valid")
    dm.slot_store.slots["task_type"] = Slot("task_type", value="管缆巡检", status="valid")
    dm.slot_store.slots["water_depth"] = Slot("water_depth", value=500.0, status="valid")
    dm._rebuild_cache()
    v = Violation("depth_vs_rov_limit", "水深较大预警", "水深较大预警", "soft", related_fields=["water_depth"])
    dm._blocking_violations = [v]


class TestIntentRoutingAndInvariance(unittest.TestCase):
    def setUp(self):
        self.kb = KnowledgeBase()
        self.llm = DummyLLM()
        self.router = IntentRouter(self.llm)
        self.dm = DialogueManager(self.llm, self.kb)

    # ══════════════════════════════════════════════════════════════════════
    # 一、基础路由测试（保留原有测试）
    # ══════════════════════════════════════════════════════════════════════

    def test_r01_tool_query_routing(self):
        res = self.router.route("这个任务适合使用什么工具？", [], {})
        self.assertEqual(res.intent, "TOOL_QUERY")
        self.assertFalse(res.should_update_slots)

    def test_r02_active_task_tool_query_routing(self):
        task_state = {"task_type": "管缆巡检", "task_type_key": "pipeline_inspection"}
        res = self.router.route("这个任务适合使用什么工具？", [], task_state)
        self.assertEqual(res.intent, "TOOL_QUERY")
        self.assertFalse(res.should_update_slots)

    def test_r03_active_task_thanks_routing(self):
        task_state = {"task_type": "管缆巡检", "task_type_key": "pipeline_inspection"}
        res = self.router.route("谢谢", [], task_state)
        self.assertEqual(res.intent, "GENERAL_CHAT")
        self.assertFalse(res.should_update_slots)

    def test_r04_active_task_irrelevant_input_routing(self):
        task_state = {"task_type": "管缆巡检", "task_type_key": "pipeline_inspection"}
        res = self.router.route("今天天气不错啊", [], task_state)
        self.assertIn(res.intent, ("CLARIFICATION", "UNKNOWN"))
        self.assertFalse(res.should_update_slots)

    def test_r05_negation_confirm_does_not_confirm(self):
        self.dm.phase = "confirming"
        self.dm.task_state = {"task_type": "管缆巡检", "task_type_key": "pipeline_inspection"}
        res = self.router.route("不好，水深改成500米", [], self.dm.task_state, phase=self.dm.phase)
        self.assertNotEqual(res.intent, "TASK_CONFIRM")
        self.assertIn(res.intent, ("TASK_CREATE", "TASK_UPDATE"))

    def test_r06_unconfirm_does_not_confirm(self):
        self.dm.phase = "confirming"
        res = self.router.route("不确认", [], self.dm.task_state, phase=self.dm.phase)
        self.assertNotEqual(res.intent, "TASK_CONFIRM")

    def test_r07_dont_publish_does_not_confirm(self):
        self.dm.phase = "confirming"
        res = self.router.route("不要发布", [], self.dm.task_state, phase=self.dm.phase)
        self.assertNotEqual(res.intent, "TASK_CONFIRM")

    def test_r08_dont_cancel_does_not_cancel(self):
        res = self.router.route("不要取消任务", [], self.dm.task_state, phase="collecting")
        self.assertNotEqual(res.intent, "TASK_CANCEL")

    def test_r09_confirm_publish_in_confirming_phase(self):
        res = self.router.route("确认发布", [], self.dm.task_state, phase="confirming")
        self.assertEqual(res.intent, "TASK_CONFIRM")

    def test_r10_cancel_current_task(self):
        res = self.router.route("取消当前任务", [], self.dm.task_state)
        self.assertEqual(res.intent, "TASK_CANCEL")

    def test_r11_knowledge_query_numbers_no_slot_mutation(self):
        v_before = self.dm.slot_store.version
        reply = self.dm.process("500米级机器人有哪些？")
        v_after = self.dm.slot_store.version
        self.assertEqual(v_before, v_after)
        wd = self.dm.slot_store.slots.get("water_depth")
        self.assertTrue(wd is None or wd.value is None)

    def test_r12_non_task_routes_no_extractor_or_commit(self):
        non_task_queries = [
            "你好",
            "机器人可以使用哪些工具？",
            "500米级机器人有哪些？",
            "当前任务进行到哪一步？",
            "谢谢",
        ]
        for q in non_task_queries:
            with patch.object(self.dm.extractor, 'extract_updates') as mock_ext, \
                 patch.object(self.dm.slot_store, 'commit_transaction') as mock_commit:
                self.dm.process(q)
                mock_ext.assert_not_called()
                mock_commit.assert_not_called()

    def test_r13_non_task_route_snapshot_invariance(self):
        v_before = self.dm.slot_store.version
        snap_before = self.dm.slot_store.export_snapshot()
        self.dm.process("机器人可以使用哪些工具？")
        v_after = self.dm.slot_store.version
        snap_after = self.dm.slot_store.export_snapshot()
        self.assertEqual(v_before, v_after)
        self.assertEqual(snap_before, snap_after)

    def test_r14_device_capability_500m_filtering(self):
        res = self.kb.execute_typed_query("DEVICE_CAPABILITY", "500米级机器人有哪些？")
        self.assertTrue(res["found"])
        for r in res["results"]:
            self.assertEqual(r.get("max_depth_m"), 500)

    def test_r15_kb_not_found_no_hallucination(self):
        res = self.kb.execute_typed_query("DEVICE_CAPABILITY", "超光速神潜器9000能在1000米作业吗？")
        self.assertFalse(res["found"])
        self.assertEqual(len(res["results"]), 0)

    def test_restored_device_status_routing(self):
        res = self.router.route("当前机器人状态怎么样？", [], {})
        self.assertEqual(res.intent, "DEVICE_STATUS")
        self.assertFalse(res.should_update_slots)

    def test_restored_environment_query_routing(self):
        res = self.router.route("这里的海况怎么样？", [], {})
        self.assertEqual(res.intent, "ENVIRONMENT_QUERY")
        self.assertFalse(res.should_update_slots)

    def test_restored_frontend_error_parsing_elements(self):
        with open("index.html", encoding="utf-8") as f:
            content = f.read()
        self.assertIn("res.ok", content)
        self.assertIn("data.request_id", content)
        self.assertIn("data.msg", content)

    def test_restored_full_task_creation_flow(self):
        with patch.object(self.dm.extractor, 'extract_updates', return_value={
            "intent": "TASK_CREATE",
            "slot_candidates": [
                {"raw_key": "任务类型", "canonical_key": "task_type", "raw_value": "巡检", "normalized_value": "管缆巡检", "confidence": 0.95},
                {"raw_key": "任务标识", "canonical_key": "task_type_key", "raw_value": "巡检", "normalized_value": "pipeline_inspection", "confidence": 0.95},
                {"raw_key": "水深", "canonical_key": "water_depth", "raw_value": "300米", "normalized_value": "300", "confidence": 0.95},
            ]
        }):
            reply = self.dm.process("创建一个水下巡检任务，水深300米")
            self.assertIn("300", str(self.dm._last_built_json))
            self.assertEqual(self.dm.task_state.get("task_type_key"), "pipeline_inspection")

    # ══════════════════════════════════════════════════════════════════════
    # 二、问句与动作门控测试
    # ══════════════════════════════════════════════════════════════════════

    def test_n01_why_use_rov_no_task_update(self):
        res = self.router.route("为什么使用ROV？", [], {})
        self.assertNotEqual(res.intent, "TASK_UPDATE")
        self.assertFalse(res.should_update_slots)

    def test_n02_active_task_why_use_rov_no_slot_update(self):
        task_state = {"task_type": "管缆巡检", "task_type_key": "pipeline_inspection"}
        res = self.router.route("为什么使用ROV？", [], task_state)
        self.assertFalse(res.should_update_slots)

    def test_n03_how_to_choose_robot_no_task_update(self):
        res = self.router.route("如何选择机器人？", [], {})
        self.assertNotEqual(res.intent, "TASK_UPDATE")
        self.assertFalse(res.should_update_slots)

    def test_n04_what_is_water_depth_no_task_create(self):
        res = self.router.route("水深多少？", [], {})
        self.assertNotEqual(res.intent, "TASK_CREATE")
        self.assertFalse(res.should_update_slots)

    def test_n05_active_task_math_question_no_task_update(self):
        task_state = {"task_type": "管缆巡检", "task_type_key": "pipeline_inspection"}
        res = self.router.route("1+1等于几？", [], task_state)
        self.assertNotEqual(res.intent, "TASK_UPDATE")
        self.assertFalse(res.should_update_slots)

    def test_n06_current_task_params_task_status(self):
        res = self.router.route("当前任务有哪些参数？", [], {})
        self.assertEqual(res.intent, "TASK_STATUS")

    def test_n07_pipeline_inspection_params_knowledge_qa(self):
        res = self.router.route("管缆巡检需要哪些参数？", [], {})
        self.assertEqual(res.intent, "KNOWLEDGE_QA")

    def test_n08_what_robots_available_device_capability_found(self):
        res = self.router.route("有哪些机器人？", [], {})
        self.assertEqual(res.intent, "DEVICE_CAPABILITY")
        kb_res = self.kb.execute_typed_query("DEVICE_CAPABILITY", "有哪些机器人？")
        self.assertTrue(kb_res["found"])
        self.assertGreater(len(kb_res["results"]), 0)

    def test_n09_support_500m_robots_device_capability(self):
        res = self.router.route("支持500米水深的机器人有哪些？", [], {})
        self.assertEqual(res.intent, "DEVICE_CAPABILITY")
        kb_res = self.kb.execute_typed_query("DEVICE_CAPABILITY", "支持500米水深的机器人有哪些？")
        self.assertTrue(kb_res["found"])

    def test_n10_able_to_work_500m_robots_device_capability(self):
        res = self.router.route("能够在500米作业的机器人有哪些？", [], {})
        self.assertEqual(res.intent, "DEVICE_CAPABILITY")
        kb_res = self.kb.execute_typed_query("DEVICE_CAPABILITY", "能够在500米作业的机器人有哪些？")
        self.assertTrue(kb_res["found"])

    def test_n11_device_queries_do_not_mutate_water_depth(self):
        queries = ["有哪些机器人？", "支持500米水深的机器人有哪些？", "能够在500米作业的机器人有哪些？"]
        for q in queries:
            v_before = self.dm.slot_store.version
            self.dm.process(q)
            v_after = self.dm.slot_store.version
            self.assertEqual(v_before, v_after)
            wd = self.dm.slot_store.slots.get("water_depth")
            self.assertTrue(wd is None or wd.value is None)

    # ══════════════════════════════════════════════════════════════════════
    # 三、LLM 路由严格校验（无 mock 绕过）
    # ══════════════════════════════════════════════════════════════════════

    def test_n12_missing_confidence_no_slot_update(self):
        with patch.object(self.llm, 'extract_json', return_value={"intent": "TASK_CREATE", "reason": "test"}) as mock_ext:
            res = self.router.route("模糊问句", [], {})
            mock_ext.assert_called_once()
            self.assertEqual(res.intent, "CLARIFICATION")
            self.assertFalse(res.should_update_slots)

    def test_n13_all_invalid_confidences_fall_to_clarification(self):
        invalid_confidences = [None, "high", True, False, -0.1, 1.1, float('nan'), float('inf')]
        for c in invalid_confidences:
            with patch.object(self.llm, 'extract_json', return_value={"intent": "TASK_CREATE", "confidence": c, "reason": "test"}):
                res = self.router.route("模糊问句测试", [], {})
                self.assertEqual(res.intent, "CLARIFICATION", f"confidence={c} should fall to CLARIFICATION")
                self.assertFalse(res.should_update_slots, f"confidence={c} should not update slots")

    def test_llm_invalid_json_falls_to_clarification(self):
        """LLM 返回非法 JSON → CLARIFICATION"""
        with patch.object(self.llm, 'extract_json', return_value="not a dict"):
            res = self.router.route("模糊问句", [], {})
            self.assertEqual(res.intent, "CLARIFICATION")
            self.assertFalse(res.should_update_slots)

    def test_llm_exception_falls_to_clarification(self):
        """LLM 调用异常 → CLARIFICATION (fallback)"""
        with patch.object(self.llm, 'extract_json', side_effect=RuntimeError("LLM down")):
            res = self.router.route("模糊问句", [], {})
            self.assertEqual(res.intent, "CLARIFICATION")
            self.assertFalse(res.should_update_slots)

    def test_llm_invalid_intent_falls_to_clarification(self):
        """非法 intent → CLARIFICATION"""
        with patch.object(self.llm, 'extract_json', return_value={"intent": "BOGUS_INTENT", "confidence": 0.9, "reason": "test"}):
            res = self.router.route("模糊问句", [], {})
            self.assertEqual(res.intent, "CLARIFICATION")
            self.assertFalse(res.should_update_slots)

    def test_llm_low_confidence_falls_to_clarification(self):
        """低置信度 → CLARIFICATION"""
        with patch.object(self.llm, 'extract_json', return_value={"intent": "TASK_CREATE", "confidence": 0.3, "reason": "不确定"}):
            res = self.router.route("模糊问句", [], {})
            self.assertEqual(res.intent, "CLARIFICATION")
            self.assertFalse(res.should_update_slots)

    def test_slot_candidates_no_longer_bypass_validation(self):
        """slot_candidates 不再绕过 confidence 校验"""
        with patch.object(self.llm, 'extract_json', return_value={
            "intent": "TASK_CREATE",
            "slot_candidates": [],
            "reason": "test"
            # 缺少 confidence
        }):
            res = self.router.route("模糊问句", [], {})
            self.assertEqual(res.intent, "CLARIFICATION")
            self.assertFalse(res.should_update_slots)

    def test_slot_candidates_nan_confidence_rejected(self):
        """slot_candidates + NaN confidence → CLARIFICATION"""
        with patch.object(self.llm, 'extract_json', return_value={
            "intent": "TASK_CREATE",
            "slot_candidates": [],
            "confidence": float('nan'),
            "reason": "test"
        }):
            res = self.router.route("模糊问句", [], {})
            self.assertEqual(res.intent, "CLARIFICATION")
            self.assertFalse(res.should_update_slots)

    def test_missing_reason_falls_to_clarification(self):
        """缺少 reason → CLARIFICATION"""
        with patch.object(self.llm, 'extract_json', return_value={"intent": "TASK_CREATE", "confidence": 0.9}):
            res = self.router.route("模糊问句", [], {})
            self.assertEqual(res.intent, "CLARIFICATION")
            self.assertFalse(res.should_update_slots)

    def test_empty_reason_falls_to_clarification(self):
        """空 reason → CLARIFICATION"""
        with patch.object(self.llm, 'extract_json', return_value={"intent": "TASK_CREATE", "confidence": 0.9, "reason": "  "}):
            res = self.router.route("模糊问句", [], {})
            self.assertEqual(res.intent, "CLARIFICATION")
            self.assertFalse(res.should_update_slots)

    # ══════════════════════════════════════════════════════════════════════
    # 四、TASK_CONFIRM 控制指令隔离测试
    # ══════════════════════════════════════════════════════════════════════

    def test_n14_blocked_soft_confirm_continue_flow(self):
        """blocked_soft + '确认继续': 不调用 extractor, slot 不变, phase 离开 blocked_soft"""
        _seed_blocked_soft_dm(self.dm, self.kb)
        v_before = self.dm.slot_store.version
        snap_before = copy.deepcopy(self.dm.slot_store.export_snapshot())
        wd_before = self.dm.slot_store.slots["water_depth"].value

        with patch.object(self.dm.extractor, 'extract_updates') as mock_ext, \
             patch.object(self.dm.slot_store, 'commit_transaction') as mock_commit:
            reply = self.dm.process("确认继续")
            mock_ext.assert_not_called()
            mock_commit.assert_not_called()

        self.assertNotEqual(self.dm.phase, "blocked_soft")
        self.assertEqual(self.dm.slot_store.version, v_before)
        self.assertEqual(self.dm.slot_store.export_snapshot(), snap_before)
        self.assertEqual(self.dm.slot_store.slots["water_depth"].value, wd_before)
        # 验证白名单包含正确条目
        self.assertTrue(any(item[2] == "depth_vs_rov_limit" for item in self.dm._soft_whitelist))

    def test_n15_blocked_soft_ignore_warning_flow(self):
        """blocked_soft + '忽略警告': 不调用 extractor, slot 不变, phase 离开 blocked_soft"""
        _seed_blocked_soft_dm(self.dm, self.kb)
        v_before = self.dm.slot_store.version
        snap_before = copy.deepcopy(self.dm.slot_store.export_snapshot())
        wd_before = self.dm.slot_store.slots["water_depth"].value

        with patch.object(self.dm.extractor, 'extract_updates') as mock_ext, \
             patch.object(self.dm.slot_store, 'commit_transaction') as mock_commit:
            reply = self.dm.process("忽略警告")
            mock_ext.assert_not_called()
            mock_commit.assert_not_called()

        self.assertNotEqual(self.dm.phase, "blocked_soft")
        self.assertEqual(self.dm.slot_store.version, v_before)
        self.assertEqual(self.dm.slot_store.export_snapshot(), snap_before)
        self.assertEqual(self.dm.slot_store.slots["water_depth"].value, wd_before)
        self.assertTrue(any(item[2] == "depth_vs_rov_limit" for item in self.dm._soft_whitelist))

    def test_blocked_soft_continue_keyword_flow(self):
        """blocked_soft + '继续': 控制状态正确"""
        _seed_blocked_soft_dm(self.dm, self.kb)
        v_before = self.dm.slot_store.version
        with patch.object(self.dm.extractor, 'extract_updates') as mock_ext:
            self.dm.process("继续")
            mock_ext.assert_not_called()
        self.assertNotEqual(self.dm.phase, "blocked_soft")
        self.assertEqual(self.dm.slot_store.version, v_before)

    def test_blocked_soft_ignore_keyword_flow(self):
        """blocked_soft + '忽略': 控制状态正确"""
        _seed_blocked_soft_dm(self.dm, self.kb)
        v_before = self.dm.slot_store.version
        with patch.object(self.dm.extractor, 'extract_updates') as mock_ext:
            self.dm.process("忽略")
            mock_ext.assert_not_called()
        self.assertNotEqual(self.dm.phase, "blocked_soft")
        self.assertEqual(self.dm.slot_store.version, v_before)

    def test_n16_confirming_confirm_publish_flow(self):
        """confirming + '确认发布': 不调用 extractor, 正常发布"""
        from pathlib import Path
        import tempfile
        from tests.test_slot_consistency import seed_complete_valid_pipeline_task
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir) / "task"
            tmp_path.mkdir(parents=True, exist_ok=True)
            seed_complete_valid_pipeline_task(self.dm, self.kb)
            self.dm.phase = "confirming"

            all_v = self.dm.validator.validate(self.dm.task_state)
            for v in all_v:
                if v.severity == "soft":
                    for f in v.related_fields:
                        val = self.dm.task_state.get(f)
                        if val is not None:
                            self.dm._soft_whitelist.add((f, str(val), v.constraint_id))

            with patch("src.task_intent_builder.get_task_dir", return_value=tmp_path), \
                 patch("src.id_sequence.get_result_dir", return_value=Path(tmp_dir)), \
                 patch.object(self.dm.extractor, 'extract_updates') as mock_ext, \
                 patch.object(self.dm.slot_store, 'commit_transaction') as mock_commit:
                reply = self.dm.process("确认发布")
                mock_ext.assert_not_called()
                mock_commit.assert_not_called()
                self.assertEqual(self.dm.phase, "done")

    def test_n17_non_confirming_confirm_publish_no_publish(self):
        """非确认阶段的'确认发布'不得发布"""
        self.dm.phase = "collecting"
        self.dm.task_state = {}
        self.dm._rebuild_cache()
        reply = self.dm.process("确认发布")
        self.assertNotEqual(self.dm.phase, "done")

    # ── 恶意抽取器对抗测试 ──

    def test_malicious_extractor_blocked_soft_ignore(self):
        """恶意 extractor 返回 water_depth=999，'忽略警告'时 extractor 根本不被调用"""
        _seed_blocked_soft_dm(self.dm, self.kb)
        wd_before = self.dm.slot_store.slots["water_depth"].value
        self.assertEqual(wd_before, 500.0)

        malicious_return = {
            "intent": "TASK_UPDATE",
            "slot_candidates": [
                {"canonical_key": "water_depth", "normalized_value": 999, "raw_value": "999", "confidence": 1.0}
            ]
        }
        with patch.object(self.dm.extractor, 'extract_updates', return_value=malicious_return) as mock_ext:
            self.dm.process("忽略警告")
            mock_ext.assert_not_called()

        self.assertEqual(self.dm.slot_store.slots["water_depth"].value, 500.0)

    def test_malicious_extractor_blocked_soft_confirm_continue(self):
        """恶意 extractor 返回 water_depth=999，'确认继续'时 extractor 根本不被调用"""
        _seed_blocked_soft_dm(self.dm, self.kb)

        malicious_return = {
            "intent": "TASK_UPDATE",
            "slot_candidates": [
                {"canonical_key": "water_depth", "normalized_value": 999, "raw_value": "999", "confidence": 1.0}
            ]
        }
        with patch.object(self.dm.extractor, 'extract_updates', return_value=malicious_return) as mock_ext:
            self.dm.process("确认继续")
            mock_ext.assert_not_called()

        self.assertEqual(self.dm.slot_store.slots["water_depth"].value, 500.0)

    def test_malicious_extractor_confirming_publish(self):
        """恶意 extractor 返回 water_depth=999，'确认发布'时 extractor 根本不被调用"""
        from pathlib import Path
        import tempfile
        from tests.test_slot_consistency import seed_complete_valid_pipeline_task
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir) / "task"
            tmp_path.mkdir(parents=True, exist_ok=True)
            seed_complete_valid_pipeline_task(self.dm, self.kb)
            self.dm.phase = "confirming"
            wd_before = self.dm.slot_store.slots["water_depth"].value

            malicious_return = {
                "intent": "TASK_UPDATE",
                "slot_candidates": [
                    {"canonical_key": "water_depth", "normalized_value": 999, "raw_value": "999", "confidence": 1.0}
                ]
            }
            with patch("src.task_intent_builder.get_task_dir", return_value=tmp_path), \
                 patch("src.id_sequence.get_result_dir", return_value=Path(tmp_dir)), \
                 patch.object(self.dm.extractor, 'extract_updates', return_value=malicious_return) as mock_ext:
                self.dm.process("确认发布")
                mock_ext.assert_not_called()

            self.assertEqual(self.dm.slot_store.slots["water_depth"].value, wd_before)

    # ══════════════════════════════════════════════════════════════════════
    # 五、活动任务工具查询测试
    # ══════════════════════════════════════════════════════════════════════

    def test_n18_tool_query_with_active_task_snapshot_invariance(self):
        """通过 slot_store 设置任务状态后查询工具，验证快照不变"""
        self.dm.slot_store.slots["task_type_key"] = Slot("task_type_key", value="pipeline_inspection", status="valid")
        self.dm.slot_store.slots["task_type"] = Slot("task_type", value="管缆巡检", status="valid")
        self.dm.slot_store.slots["water_depth"] = Slot("water_depth", value=300.0, status="valid")
        self.dm._rebuild_cache()

        v_before = self.dm.slot_store.version
        snap_before = self.dm.slot_store.export_snapshot()

        reply = self.dm.process("这个任务适合使用什么工具？")

        v_after = self.dm.slot_store.version
        snap_after = self.dm.slot_store.export_snapshot()

        self.assertEqual(v_before, v_after)
        self.assertEqual(snap_before, snap_after)

    def test_tool_query_with_active_task_receives_context(self):
        """工具查询 execute_typed_query 收到正确的 task_type_key"""
        self.dm.slot_store.slots["task_type_key"] = Slot("task_type_key", value="pipeline_inspection", status="valid")
        self.dm.slot_store.slots["task_type"] = Slot("task_type", value="管缆巡检", status="valid")
        self.dm._rebuild_cache()

        with patch.object(self.kb, 'execute_typed_query', wraps=self.kb.execute_typed_query) as mock_kb:
            self.dm.process("这个任务适合使用什么工具？")
            self.assertTrue(mock_kb.called)
            call_args = mock_kb.call_args
            context = call_args.kwargs.get("context") or (call_args.args[2] if len(call_args.args) > 2 else None)
            if context:
                self.assertEqual(context.get("task_type_key"), "pipeline_inspection")

    # ══════════════════════════════════════════════════════════════════════
    # 六、深度查询测试
    # ══════════════════════════════════════════════════════════════════════

    def test_depth_gte_1000m_work(self):
        """能够在1000米作业 → 每个结果 max_depth_m >= 1000"""
        res = self.kb.execute_typed_query("DEVICE_CAPABILITY", "能够在1000米作业的机器人有哪些？")
        for r in res["results"]:
            self.assertGreaterEqual(r["max_depth_m"], 1000)

    def test_depth_gte_1000m_dive(self):
        """可下潜到1000米 → 每个结果 max_depth_m >= 1000"""
        res = self.kb.execute_typed_query("DEVICE_CAPABILITY", "可下潜到1000米的机器人有哪些？")
        for r in res["results"]:
            self.assertGreaterEqual(r["max_depth_m"], 1000)

    def test_depth_gte_1000m_support(self):
        """支持1000米水深 → 每个结果 max_depth_m >= 1000"""
        res = self.kb.execute_typed_query("DEVICE_CAPABILITY", "支持1000米水深的机器人有哪些？")
        for r in res["results"]:
            self.assertGreaterEqual(r["max_depth_m"], 1000)

    def test_depth_lte_1000m(self):
        """不超过1000米 → 每个结果 max_depth_m <= 1000"""
        res = self.kb.execute_typed_query("DEVICE_CAPABILITY", "不超过1000米的机器人有哪些？")
        self.assertTrue(res["found"])
        for r in res["results"]:
            self.assertLessEqual(r["max_depth_m"], 1000)

    def test_depth_lt_1000m(self):
        """低于1000米 → 每个结果 max_depth_m < 1000"""
        res = self.kb.execute_typed_query("DEVICE_CAPABILITY", "低于1000米的机器人有哪些？")
        for r in res["results"]:
            self.assertLess(r["max_depth_m"], 1000)

    def test_depth_eq_1000m(self):
        """1000米级 → 每个结果 max_depth_m == 1000"""
        res = self.kb.execute_typed_query("DEVICE_CAPABILITY", "1000米级机器人有哪些？")
        for r in res["results"]:
            self.assertEqual(r["max_depth_m"], 1000)

    def test_depth_parse_failure_no_return_all(self):
        """深度数字解析失败时不得返回全部设备（如含有未知语义的 '987米 xxx'）"""
        res = self.kb.execute_typed_query("DEVICE_CAPABILITY", "987米的某种奇怪的东西")
        # 如果987不匹配任何设备，不应返回全部
        if res["found"]:
            for r in res["results"]:
                self.assertIn(r["max_depth_m"], [987])  # 只有精确匹配才行

    def test_depth_generic_all_robots(self):
        """'有哪些机器人？'返回全部设备"""
        res = self.kb.execute_typed_query("DEVICE_CAPABILITY", "有哪些机器人？")
        self.assertTrue(res["found"])
        self.assertGreater(len(res["results"]), 0)

    def test_depth_gte_500m(self):
        """支持500米水深 → 每个结果 max_depth_m >= 500"""
        res = self.kb.execute_typed_query("DEVICE_CAPABILITY", "支持500米水深的机器人有哪些？")
        self.assertTrue(res["found"])
        for r in res["results"]:
            self.assertGreaterEqual(r["max_depth_m"], 500)

    def test_depth_not_less_than_1000m(self):
        """不少于1000米 → 每个结果 max_depth_m >= 1000"""
        res = self.kb.execute_typed_query("DEVICE_CAPABILITY", "不少于1000米的机器人有哪些？")
        for r in res["results"]:
            self.assertGreaterEqual(r["max_depth_m"], 1000)

    def test_depth_gt_1000m(self):
        """超过1000米 → 每个结果 max_depth_m > 1000"""
        res = self.kb.execute_typed_query("DEVICE_CAPABILITY", "超过1000米的机器人有哪些？")
        for r in res["results"]:
            self.assertGreater(r["max_depth_m"], 1000)

    def test_depth_at_most_1000m(self):
        """至多1000米 → 每个结果 max_depth_m <= 1000"""
        res = self.kb.execute_typed_query("DEVICE_CAPABILITY", "至多1000米的机器人有哪些？")
        self.assertTrue(res["found"])
        for r in res["results"]:
            self.assertLessEqual(r["max_depth_m"], 1000)

    def test_depth_lt_small_1000m(self):
        """小于1000米 → 每个结果 max_depth_m < 1000"""
        res = self.kb.execute_typed_query("DEVICE_CAPABILITY", "小于1000米的机器人有哪些？")
        for r in res["results"]:
            self.assertLess(r["max_depth_m"], 1000)

    # ══════════════════════════════════════════════════════════════════════
    # 七、其他保留测试
    # ══════════════════════════════════════════════════════════════════════

    def test_n19_answer_expected_slot_normal_filling(self):
        self.dm.phase = "collecting"
        self.dm.slot_store.slots["task_type_key"] = Slot("task_type_key", value="pipeline_inspection", status="valid")
        self.dm.slot_store.slots["task_type"] = Slot("task_type", value="管缆巡检", status="valid")
        self.dm._rebuild_cache()
        self.dm._last_missing = [{"key": "water_depth", "label": "水深（米）"}]

        with patch.object(self.dm.extractor, 'extract_updates', return_value={
            "intent": "TASK_UPDATE",
            "slot_candidates": [
                {"raw_key": "水深", "canonical_key": "water_depth", "raw_value": "500米", "normalized_value": 500.0, "confidence": 0.95}
            ]
        }):
            reply = self.dm.process("500米")
            self.assertEqual(self.dm.slot_store.slots.get("water_depth").value, 500.0)

    def test_n20_irrelevant_math_question_with_number_no_slot_filling(self):
        self.dm.task_state = {"task_type": "管缆巡检", "task_type_key": "pipeline_inspection"}
        self.dm._last_missing = [{"key": "water_depth", "label": "水深（米）"}]

        v_before = self.dm.slot_store.version
        reply = self.dm.process("1+1等于几？")
        v_after = self.dm.slot_store.version

        self.assertEqual(v_before, v_after)
        wd = self.dm.slot_store.slots.get("water_depth")
        self.assertTrue(wd is None or wd.value is None)

    # ══════════════════════════════════════════════════════════════════════
    # 八、KB found=false 回复测试
    # ══════════════════════════════════════════════════════════════════════

    def test_kb_not_found_reply_indicates_no_result(self):
        """KB found=false 时最终回复必须明确表示无结果"""
        reply = self.dm.process("超光速神潜器9000有什么能力？")
        # 不应该胡编乱造
        self.assertNotIn("超光速神潜器9000可以", reply)


if __name__ == "__main__":
    unittest.main()
