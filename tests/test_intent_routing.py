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
        self.assertTrue(res.is_query)
        self.assertFalse(res.should_update_slots)

    def test_r02_active_task_tool_query_routing(self):
        task_state = {"task_type": "管缆巡检", "task_type_key": "pipeline_inspection"}
        res = self.router.route("这个任务适合使用什么工具？", [], task_state)
        self.assertTrue(res.is_query)
        self.assertFalse(res.should_update_slots)

    def test_r03_active_task_thanks_routing(self):
        task_state = {"task_type": "管缆巡检", "task_type_key": "pipeline_inspection"}
        res = self.router.route("谢谢", [], task_state)
        self.assertTrue(res.is_query or not res.should_update_slots)
        self.assertFalse(res.should_update_slots)

    def test_r04_active_task_irrelevant_input_routing(self):
        task_state = {"task_type": "管缆巡检", "task_type_key": "pipeline_inspection"}
        res = self.router.route("今天天气不错啊", [], task_state)
        self.assertFalse(res.should_update_slots)

    def test_r05_negation_confirm_does_not_confirm(self):
        self.dm.phase = "confirming"
        self.dm.task_state = {"task_type": "管缆巡检", "task_type_key": "pipeline_inspection"}
        res = self.router.route("不好，水深改成500米", [], self.dm.task_state, phase=self.dm.phase)
        self.assertEqual(res.interaction_type, "WRITE")
        self.assertTrue(res.should_update_slots)

    def test_r06_unconfirm_does_not_confirm(self):
        self.dm.phase = "confirming"
        res = self.router.route("不确认", [], self.dm.task_state, phase=self.dm.phase)
        self.assertFalse(res.should_update_slots)

    def test_r07_dont_publish_does_not_confirm(self):
        self.dm.phase = "confirming"
        res = self.router.route("不要发布", [], self.dm.task_state, phase=self.dm.phase)
        self.assertFalse(res.should_update_slots)

    def test_r08_dont_cancel_does_not_cancel(self):
        res = self.router.route("不要取消任务", [], self.dm.task_state, phase="collecting")
        self.assertFalse(res.should_update_slots)

    def test_r09_confirm_publish_in_confirming_phase(self):
        res = self.router.route("确认发布", [], self.dm.task_state, phase="confirming")
        self.assertEqual(res.interaction_type, "WRITE")

    def test_r10_cancel_current_task(self):
        res = self.router.route("取消当前任务", [], self.dm.task_state)
        self.assertTrue(res.is_query or res.interaction_type == "WRITE")

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
        self.assertTrue(res.is_query)
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
            "interaction_type": "QUERY",
            "slot_candidates": [],
            "reason": "test"
            # 缺少 confidence
        }):
            res = self.router.route("模糊问句", [], {})
            self.assertTrue(res.is_query or not res.should_update_slots)
            self.assertFalse(res.should_update_slots)

    def test_slot_candidates_nan_confidence_rejected(self):
        """slot_candidates + NaN confidence → CLARIFICATION"""
        with patch.object(self.llm, 'extract_json', return_value={
            "interaction_type": "QUERY",
            "slot_candidates": [],
            "confidence": float('nan'),
            "reason": "test"
        }):
            res = self.router.route("模糊问句", [], {})
            self.assertTrue(res.is_query or not res.should_update_slots)
            self.assertFalse(res.should_update_slots)

    def test_missing_reason_falls_to_clarification(self):
        """缺少 reason → CLARIFICATION / QUERY"""
        with patch.object(self.llm, 'extract_json', return_value={"interaction_type": "QUERY", "confidence": 0.9}):
            res = self.router.route("模糊问句", [], {})
            self.assertTrue(res.is_query or not res.should_update_slots)
            self.assertFalse(res.should_update_slots)

    def test_empty_reason_falls_to_clarification(self):
        """空 reason → CLARIFICATION / QUERY"""
        with patch.object(self.llm, 'extract_json', return_value={"interaction_type": "QUERY", "confidence": 0.9, "reason": "  "}):
            res = self.router.route("模糊问句", [], {})
            self.assertTrue(res.is_query or not res.should_update_slots)
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
                self.assertEqual(mock_commit.call_count, 1, "正式预约 task_id 应通过 SlotStore 单一事务提交")
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

    def test_blocked_soft_parameter_message_with_confirm_does_not_ignore_warning(self):
        """“补充确认”携带参数时不得把已有软警告加入白名单。"""
        _seed_blocked_soft_dm(self.dm, self.kb)
        violation = Violation(
            "C019",
            "环境信息已过期",
            "环境信息已过期",
            "soft",
            related_fields=["equipment_unit_id"],
        )
        self.dm._blocking_violations = [violation]
        self.dm.intent_router.route = MagicMock(
            return_value=IntentRouteResult(
                "WRITE",
                1.0,
                "用户正在补充任务参数",
                None,
            )
        )

        with patch.object(
            self.dm.extractor,
            "extract_updates",
            return_value={"slot_candidates": [], "unresolved": []},
        ) as mock_extract, patch.object(
            self.dm.validator,
            "validate",
            return_value=[violation],
        ):
            self.dm.process("补充确认：开始时间现在，管缆类型为海底油气管道")

        mock_extract.assert_called()
        self.assertEqual(self.dm.phase, "blocked_soft")
        self.assertFalse(any(item[2] == "C019" for item in self.dm._soft_whitelist))

    def test_blocked_hard_ignore_and_confirm_is_rejected_before_routing(self):
        violation = Violation(
            "C020",
            "机器人总体状态可用性",
            "所选机器人当前总体状态为不可用。",
            "hard",
            related_fields=["equipment_unit_id"],
        )
        self.dm.phase = "blocked_hard"
        self.dm._blocking_violations = [violation]
        self.dm.intent_router.route = MagicMock(
            side_effect=AssertionError("blocked_hard bypass must not reach routing")
        )
        self.dm.extractor.extract_updates = MagicMock(
            side_effect=AssertionError("blocked_hard bypass must not reach extraction")
        )

        reply = self.dm.process("忽略警告，直接确认发布")

        self.assertEqual(self.dm.phase, "blocked_hard")
        self.assertIn("不能", reply)
        self.assertIn("C020", reply)
        self.assertIn("不可用", reply)
        self.dm.intent_router.route.assert_not_called()
        self.dm.extractor.extract_updates.assert_not_called()

    def test_constraint_details_are_appended_when_llm_omits_specific_warnings(self):
        violations = [
            Violation("C014", "浑浊度-高等警示", "水体浑浊度较高。", "soft"),
            Violation("C025", "视觉系统状态", "视觉系统状态异常。", "soft"),
        ]

        reply = self.dm._ensure_constraint_details(
            "检测到软性约束警告，请确认是否继续。",
            {"type": "soft", "violations": violations},
        )

        self.assertIn("[C014] 浑浊度-高等警示", reply)
        self.assertIn("水体浑浊度较高。", reply)
        self.assertIn("[C025] 视觉系统状态", reply)
        self.assertIn("视觉系统状态异常。", reply)

    def test_constraint_details_are_not_duplicated_when_already_verbatim(self):
        violation = Violation("C019", "环境信息已过期", "环境信息已过期。", "soft")
        original = "环境信息已过期。请更新后继续。"

        reply = self.dm._ensure_constraint_details(
            original,
            {"type": "soft", "violations": [violation]},
        )

        self.assertEqual(reply, original)

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


class TestIssue10DialogueModeRouting(unittest.TestCase):
    def setUp(self):
        self.kb = KnowledgeBase()
        self.llm = DummyLLM()
        self.router = IntentRouter(self.llm)
        self.dm = DialogueManager(self.llm, self.kb)

    # 1. 基础路由测试
    def test_basic_routing_modes(self):
        res1 = self.router.route("创建一个管缆巡检任务", [], {})
        self.assertEqual(res1.dialogue_mode, "task_collection")
        self.assertEqual(res1.interaction_type, "WRITE")

        res2 = self.router.route("ROV 最大作业水深是多少？", [], {})
        self.assertEqual(res2.dialogue_mode, "knowledge_qa")
        self.assertEqual(res2.query_intent, "DEVICE_CAPABILITY")

        self.dm.slot_store.slots["task_type_key"] = Slot(
            "task_type_key", value="pipeline_inspection", status="valid"
        )
        self.dm._rebuild_cache()
        res3 = self.router.route(
            "立即停止当前任务", [], self.dm.task_state, phase=self.dm.phase
        )
        self.assertEqual(res3.dialogue_mode, "emergency_intervention")
        self.assertEqual(res3.emergency_action, "stop")

        res4 = self.router.route("停止", [], {})
        self.assertEqual(res4.dialogue_mode, "knowledge_qa")
        self.assertEqual(res4.query_intent, "CLARIFICATION")

    # 2. 否定加参数更新
    def test_negation_plus_parameter_update(self):
        self.dm.slot_store.slots["task_type_key"] = Slot(
            "task_type_key", value="pipeline_inspection", status="valid"
        )
        self.dm._rebuild_cache()

        with patch.object(self.llm, "extract_json") as mock_llm_ej, patch.object(
            self.llm, "classify_interaction"
        ) as mock_llm_ci:
            with patch.object(
                self.dm.extractor,
                "extract_updates",
                return_value={
                    "intent": "TASK_UPDATE",
                    "slot_candidates": [
                        {
                            "canonical_key": "water_depth",
                            "normalized_value": 500.0,
                            "raw_value": "500米",
                            "confidence": 0.95,
                        }
                    ],
                },
            ):
                reply = self.dm.process("不要停止任务，水深改成500米")
                self.assertEqual(
                    self.dm.slot_store.slots["water_depth"].value, 500.0
                )
                self.assertNotEqual(self.dm.phase, "rejected")
                mock_llm_ej.assert_not_called()
                mock_llm_ci.assert_not_called()

    # 3. 无标点疑问句
    def test_unpunctuated_questions_no_control(self):
        questions = [
            "取消任务是否需要确认",
            "取消任务可以恢复吗",
            "停止任务是否已经生效",
            "暂停任务有什么影响",
            "终止任务是什么意思",
            "停止任务需不需要授权",
        ]
        for q in questions:
            res = self.router.route(q, [], {})
            self.assertNotEqual(
                res.dialogue_mode,
                "emergency_intervention",
                f"Question {q} must not route to emergency",
            )
            v_before = self.dm.slot_store.version
            phase_before = self.dm.phase
            snap_before = self.dm.slot_store.export_snapshot()

            self.dm.process(q)

            self.assertEqual(self.dm.slot_store.version, v_before)
            self.assertEqual(self.dm.phase, phase_before)
            self.assertEqual(self.dm.slot_store.export_snapshot(), snap_before)

    # 4. 条件句
    def test_conditional_sentences_no_control(self):
        conditionals = [
            "如果停止任务会怎样",
            "要是取消任务还能恢复吗",
            "假如暂停任务有什么影响",
        ]
        for c in conditionals:
            res = self.router.route(c, [], {})
            self.assertNotEqual(res.dialogue_mode, "emergency_intervention")
            v_before = self.dm.slot_store.version
            phase_before = self.dm.phase
            self.dm.process(c)
            self.assertEqual(self.dm.slot_store.version, v_before)
            self.assertEqual(self.dm.phase, phase_before)

    # 5. 非任务对象
    def test_non_task_objects_no_emergency(self):
        non_task_inputs = [
            "停止回答",
            "停止生成",
            "取消告警",
            "暂停功能",
            "取消载荷修改",
            "终止说明输出",
        ]
        for item in non_task_inputs:
            res = self.router.route(item, [], {})
            self.assertNotEqual(res.dialogue_mode, "emergency_intervention")
            v_before = self.dm.slot_store.version
            self.dm.process(item)
            self.assertEqual(self.dm.slot_store.version, v_before)

    # 6. LLM 错误升级拦截 (Double validation)
    def test_llm_emergency_escalation_double_validation(self):
        with patch.object(
            self.llm,
            "extract_json",
            return_value={
                "dialogue_mode": "emergency_intervention",
                "emergency_action": "stop",
                "confidence": 0.99,
                "reason": "用户提及停止",
            },
        ):
            res = self.router._call_llm_router(
                "停止任务有什么影响？", [], {}, "collecting", []
            )
            self.assertEqual(res.dialogue_mode, "knowledge_qa")
            self.assertEqual(res.query_intent, "CLARIFICATION")
            self.assertIsNone(res.emergency_action)

    # 7. 草稿 cancel
    def test_draft_cancel_in_all_phases(self):
        from tests.test_slot_consistency import seed_complete_valid_pipeline_task

        phases_to_test = ["collecting", "confirming", "blocked_soft", "blocked_hard"]
        for p in phases_to_test:
            self.dm.reset()
            seed_complete_valid_pipeline_task(self.dm, self.kb)
            self.dm.phase = p
            if p == "blocked_hard":
                v = Violation("h1", "hard_error", "hard_error", "hard")
                self.dm._blocking_violations = [v]

            with patch.object(self.dm.extractor, "extract_updates") as mock_ext:
                reply = self.dm.process("取消当前任务")
                mock_ext.assert_not_called()

            self.assertEqual(self.dm.phase, "rejected", f"Phase {p} cancel failed")
            self.assertEqual(self.dm.task_state, {})
            self.assertEqual(self.dm._last_built_json, {})
            self.assertEqual(self.dm._last_missing, [])
            self.assertIsNone(self.dm.final_result)

    # 8. done 阶段控制
    def test_done_phase_control_actions(self):
        from pathlib import Path
        import tempfile
        from tests.test_slot_consistency import seed_complete_valid_pipeline_task

        for action_phrase in ["暂停当前任务", "停止当前任务", "终止当前任务", "取消当前任务"]:
            with tempfile.TemporaryDirectory() as tmp_dir:
                self.dm.reset()
                seed_complete_valid_pipeline_task(self.dm, self.kb)
                all_v = self.dm.validator.validate(self.dm.task_state)
                for v in all_v:
                    if v.severity == "soft":
                        for f in v.related_fields:
                            val = self.dm.task_state.get(f)
                            if val is not None:
                                self.dm._soft_whitelist.add(
                                    (f, str(val), v.constraint_id)
                                )

                task_dir = Path(tmp_dir) / "task"
                task_dir.mkdir(parents=True, exist_ok=True)
                with patch(
                    "src.task_intent_builder.get_task_dir",
                    return_value=task_dir,
                ), patch(
                    "src.id_sequence.get_result_dir", return_value=Path(tmp_dir)
                ):
                    self.dm.process("确认发布")
                    self.assertEqual(self.dm.phase, "done")
                    state_before = copy.deepcopy(self.dm.task_state)
                    final_before = copy.deepcopy(self.dm.final_result)

                    with patch.object(
                        self.dm.extractor, "extract_updates"
                    ) as mock_ext:
                        reply = self.dm.process(action_phrase)
                        mock_ext.assert_not_called()

                    self.assertEqual(self.dm.phase, "done")
                    self.assertEqual(self.dm.task_state, state_before)
                    self.assertEqual(self.dm.final_result, final_before)
                    self.assertIn("已识别", reply)
                    self.assertIn("控制请求已记录", reply)

    # 9. 知识问答只读
    def test_knowledge_qa_read_only(self):
        self.dm.reset()
        self.dm.slot_store.slots["task_type_key"] = Slot(
            "task_type_key", value="pipeline_inspection", status="valid"
        )
        self.dm.slot_store.slots["water_depth"] = Slot(
            "water_depth", value=300.0, status="valid"
        )
        self.dm._rebuild_cache()

        v_before = self.dm.slot_store.version
        snap_before = copy.deepcopy(self.dm.slot_store.export_snapshot())
        state_before = copy.deepcopy(self.dm.task_state)
        phase_before = self.dm.phase

        self.dm.process("500米水深机器人有哪些？")

        self.assertEqual(self.dm.slot_store.version, v_before)
        self.assertEqual(self.dm.slot_store.export_snapshot(), snap_before)
        self.assertEqual(self.dm.task_state, state_before)
        self.assertEqual(self.dm.phase, phase_before)

    # 10. 多轮交错
    def test_multi_turn_interleaved_dialogue(self):
        with patch.object(
            self.dm.extractor,
            "extract_updates",
            return_value={
                "intent": "TASK_CREATE",
                "slot_candidates": [
                    {
                        "canonical_key": "task_type_key",
                        "normalized_value": "pipeline_inspection",
                        "confidence": 0.95,
                    },
                    {
                        "canonical_key": "task_type",
                        "normalized_value": "管缆巡检",
                        "confidence": 0.95,
                    },
                ],
            },
        ):
            reply1 = self.dm.process("创建一个巡检任务")
            self.assertEqual(
                self.dm.task_state.get("task_type_key"), "pipeline_inspection"
            )

        snap1 = copy.deepcopy(self.dm.slot_store.export_snapshot())

        reply2 = self.dm.process("ROV 最大水深是多少？")
        snap2 = copy.deepcopy(self.dm.slot_store.export_snapshot())
        self.assertEqual(snap1, snap2)

        with patch.object(
            self.dm.extractor,
            "extract_updates",
            return_value={
                "intent": "TASK_UPDATE",
                "slot_candidates": [
                    {
                        "canonical_key": "water_depth",
                        "normalized_value": 300.0,
                        "raw_value": "300米",
                        "confidence": 0.95,
                    }
                ],
            },
        ):
            reply3 = self.dm.process("水深设置为300米")
            self.assertEqual(
                self.dm.slot_store.slots["water_depth"].value, 300.0
            )
            self.assertEqual(
                self.dm.task_state.get("task_type_key"), "pipeline_inspection"
            )

    # 11. 草稿阶段暂停/停止/终止保留草稿测试
    def test_draft_phase_pause_stop_abort_preserves_draft(self):
        phases_to_test = ["collecting", "confirming", "blocked_soft", "blocked_hard"]
        commands_to_test = ["暂停当前任务", "停止当前任务", "终止当前任务"]

        for phase_name in phases_to_test:
            for cmd in commands_to_test:
                with self.subTest(phase=phase_name, cmd=cmd):
                    self.dm.reset()
                    self.dm.slot_store.slots["task_type_key"] = Slot("task_type_key", value="pipeline_inspection", status="valid")
                    self.dm.slot_store.slots["task_type"] = Slot("task_type", value="管缆巡检", status="valid")
                    self.dm.slot_store.slots["water_depth"] = Slot("water_depth", value=500.0, status="valid")
                    self.dm.task_state = self.dm.slot_store.get_task_state()
                    self.dm.phase = phase_name

                    snap_before = copy.deepcopy(self.dm.slot_store.export_snapshot())
                    state_before = dict(self.dm.task_state)

                    reply = self.dm.process(cmd)

                    self.assertEqual(self.dm.slot_store.export_snapshot(), snap_before)
                    self.assertEqual(self.dm.task_state, state_before)
                    self.assertEqual(self.dm.phase, phase_name)
                    self.assertIn("保留", reply)

    # 12. 草稿阶段取消操作清除草稿
    def test_draft_phase_cancel_clears_draft(self):
        for phase_name in ["collecting", "confirming", "blocked_soft", "blocked_hard"]:
            with self.subTest(phase=phase_name):
                self.dm.reset()
                self.dm.slot_store.slots["task_type_key"] = Slot("task_type_key", value="pipeline_inspection", status="valid")
                self.dm.task_state = self.dm.slot_store.get_task_state()
                self.dm.phase = phase_name

                reply = self.dm.process("取消当前任务")

                self.assertEqual(self.dm.phase, "rejected")
                self.assertIn("取消", reply)

    # 13. Done 阶段控制请求内存状态记录
    def test_done_phase_records_memory_control_request(self):
        actions = [
            ("暂停当前任务", "pause"),
            ("停止当前任务", "stop"),
            ("终止当前任务", "abort"),
            ("取消当前任务", "cancel"),
        ]
        for cmd, expected_action in actions:
            with self.subTest(cmd=cmd):
                self.dm.reset()
                self.dm.phase = "done"
                self.dm.task_state["task_type_key"] = "pipeline_inspection"

                reply = self.dm.process(cmd)

                self.assertEqual(self.dm.control_state, f"{expected_action}_requested")
                self.assertIsNotNone(self.dm.last_control_request)
                self.assertEqual(self.dm.last_control_request["action"], expected_action)
                self.assertEqual(self.dm.last_control_request["status"], "requested")
                self.assertIn("已识别", reply)

    # 14. 缺失或非法 emergency_action 降级为 knowledge_qa / CLARIFICATION
    def test_invalid_emergency_action_demotes_to_clarification(self):
        invalid_actions = [None, "", "shutdown", "invalid_cmd", "destroy"]
        for act in invalid_actions:
            with self.subTest(act=act):
                res = IntentRouteResult(
                    interaction_type="QUERY",
                    confidence=0.9,
                    reason="测试非法动作",
                    dialogue_mode="emergency_intervention",
                    emergency_action=act,
                )
                self.assertEqual(res.dialogue_mode, "knowledge_qa")
                self.assertEqual(res.intent, "CLARIFICATION")
                self.assertIsNone(res.emergency_action)

    # 15. 复合紧急指令测试（肯定控制 + 否定参数修饰）
    def test_compound_emergency_sentence_with_negated_parameter(self):
        msg = "马上停止当前任务，不要再下潜500米"
        route = self.dm.intent_router.route(
            user_message=msg,
            conversation_history=self.dm.conversation_history,
            task_state=self.dm.task_state,
            phase=self.dm.phase,
            expected_slots=[],
        )
        self.assertEqual(route.dialogue_mode, "emergency_intervention")
        self.assertEqual(route.emergency_action, "stop")

        with patch.object(self.dm.extractor, "extract_updates") as mock_ext:
            reply = self.dm.process(msg)
            mock_ext.assert_not_called()

        self.assertNotIn("water_depth", self.dm.task_state)

    # 16. 空会话控制指令不声称保留草稿且不修改状态
    def test_empty_session_control_commands(self):
        commands = ["停止当前任务", "暂停当前任务", "终止当前任务", "取消当前任务"]
        for cmd in commands:
            with self.subTest(cmd=cmd):
                self.dm.reset()
                reply = self.dm.process(cmd)
                self.assertEqual(self.dm.phase, "collecting")
                self.assertEqual(self.dm.control_state, "idle")
                self.assertIn("当前没有活动任务或可取消", reply)

    # 17. 非任务控制对象不误触发全局紧急控制
    def test_non_task_objects_do_not_trigger_emergency(self):
        non_task_cmds = [
            "停止任务打印",
            "暂停任务播报",
            "停止任务说明展示",
            "取消任务页面刷新",
            "终止任务日志输出",
        ]
        for cmd in non_task_cmds:
            with self.subTest(cmd=cmd):
                route = self.dm.intent_router.route(
                    user_message=cmd,
                    conversation_history=[],
                    task_state={},
                    phase="collecting",
                    expected_slots=[],
                )
                self.assertNotEqual(route.dialogue_mode, "emergency_intervention")
                self.assertIsNone(route.emergency_action)

    # 18. dialogue_mode 状态机切换与快照恢复测试
    def test_dialogue_mode_state_machine_and_snapshot_restoration(self):
        self.dm.reset()
        self.assertEqual(self.dm.dialogue_mode, "task_collection")

        # 触发知识问答路由
        self.dm.process("ROV 最大水深是多少？")
        self.assertEqual(self.dm.dialogue_mode, "knowledge_qa")
        self.assertIsNotNone(self.dm.last_mode_transition)
        self.assertEqual(self.dm.last_mode_transition["to"], "knowledge_qa")
        self.assertIn("source", self.dm.last_mode_transition)
        self.assertIn("changed_at", self.dm.last_mode_transition)
        self.assertTrue(len(self.dm.mode_transition_history) >= 1)

        # 导出快照并重置，验证恢复
        snap = self.dm.export_snapshot()
        self.dm.reset()
        self.assertEqual(self.dm.dialogue_mode, "task_collection")

        self.dm.load_snapshot(snap)
        self.assertEqual(self.dm.dialogue_mode, "knowledge_qa")
        self.assertEqual(self.dm.last_mode_transition["to"], "knowledge_qa")
        self.assertEqual(self.dm.get_status()["dialogue_mode"], "knowledge_qa")

    # 19. 离线 Mock 模式模糊输入降级为 knowledge_qa / CLARIFICATION
    def test_offline_mock_ambiguous_input_fallback_to_clarification(self):
        ambiguous_inputs = ["我想问一下机器人……", "随便说说", "我不确定"]
        for text in ambiguous_inputs:
            with self.subTest(text=text):
                res = self.dm.intent_router.llm.classify_interaction(
                    messages=[{"role": "user", "content": text}]
                )
                self.assertEqual(res["dialogue_mode"], "knowledge_qa")
                self.assertEqual(res["query_intent"], "CLARIFICATION")

    # 20. WRITE 与 emergency_action=None 矛盾协议判定降级为 knowledge_qa / CLARIFICATION
    def test_contradictory_write_and_missing_action_demotes_to_clarification(self):
        res = IntentRouteResult(
            interaction_type="WRITE",
            confidence=0.9,
            reason="测试矛盾协议",
            dialogue_mode="emergency_intervention",
            emergency_action=None,
        )
        self.assertEqual(res.dialogue_mode, "knowledge_qa")
        self.assertEqual(res.interaction_type, "QUERY")
        self.assertEqual(res.query_intent, "CLARIFICATION")

    # 21. 复合紧急指令中包含后续疑问/条件子句优先提取紧急动作
    def test_compound_emergency_followed_by_question_or_conditional(self):
        cases = [
            ("立即停止当前任务，为什么设备还在下潜？", "stop"),
            ("马上暂停当前任务，如果继续会怎样？", "pause"),
            ("立刻终止当前操作，之后需要做什么？", "abort"),
        ]
        for msg, expected_action in cases:
            with self.subTest(msg=msg):
                route = self.dm.intent_router.route(
                    user_message=msg,
                    conversation_history=[],
                    task_state=self.dm.task_state,
                    phase=self.dm.phase,
                    expected_slots=[],
                )
                self.assertEqual(route.dialogue_mode, "emergency_intervention")
                self.assertEqual(route.emergency_action, expected_action)

    # 22. 疑问或条件修饰控制动作本身的分支降级为知识问答
    def test_question_or_conditional_modifying_control_action(self):
        cases = [
            "为什么要停止当前任务？",
            "如果停止当前任务会怎样？",
        ]
        for msg in cases:
            with self.subTest(msg=msg):
                route = self.dm.intent_router.route(
                    user_message=msg,
                    conversation_history=[],
                    task_state={},
                    phase="collecting",
                    expected_slots=[],
                )
                self.assertEqual(route.dialogue_mode, "knowledge_qa")
                self.assertIsNone(route.emergency_action)

    # 23. 草稿取消（cancel）保留模式切换审计历史
    def test_draft_cancel_preserves_mode_transition_history(self):
        self.dm.reset()
        self.dm.slot_store.slots["task_type_key"] = Slot("task_type_key", value="pipeline_inspection", status="valid")
        self.dm.task_state = self.dm.slot_store.get_task_state()
        self.dm.phase = "collecting"

        reply = self.dm.process("取消当前任务")

        self.assertEqual(self.dm.phase, "rejected")
        self.assertEqual(self.dm.task_state, {})
        self.assertEqual(self.dm.dialogue_mode, "emergency_intervention")
        self.assertIsNotNone(self.dm.last_mode_transition)
        self.assertEqual(self.dm.last_mode_transition["to"], "emergency_intervention")
        self.assertTrue(len(self.dm.mode_transition_history) >= 1)

    # 24. 通用身份与系统时间等提前返回路径更新 dialogue_mode
    def test_fast_path_early_returns_update_dialogue_mode(self):
        self.dm.reset()
        self.dm.process("你是谁")
        self.assertEqual(self.dm.dialogue_mode, "knowledge_qa")

        self.dm.reset()
        self.dm.process("当前时间是多少？")
        self.assertEqual(self.dm.dialogue_mode, "knowledge_qa")

    # 25. 快照加载非法的 dialogue_mode / control_state / timestamp 抛出 ValueError 且状态不变
    def test_invalid_snapshot_schema_raises_value_error_without_state_mutation(self):
        self.dm.reset()
        self.dm.process("ROV 最大水深是多少？")
        state_before_mode = self.dm.dialogue_mode
        state_before_history = list(self.dm.mode_transition_history)

        bad_snapshots = [
            {"dialogue_mode": "destroy"},
            {"control_state": "invalid_state"},
            {"mode_transition_history": "not-a-list"},
            {"mode_transition_history": [{"from": "task_collection", "to": "invalid", "confidence": 1.0, "changed_at": "2026-08-02"}]},
            {"mode_transition_history": [{"from": "task_collection", "to": "knowledge_qa", "confidence": 2.0, "changed_at": "2026-08-02"}]},
            {"last_control_request": {"action": "invalid_action"}},
        ]

        for snap in bad_snapshots:
            with self.subTest(snap=snap):
                with self.assertRaises(ValueError):
                    self.dm.load_snapshot(snap)

                # 验证内存状态完全未被污染
                self.assertEqual(self.dm.dialogue_mode, state_before_mode)
                self.assertEqual(self.dm.mode_transition_history, state_before_history)

    # 26. 非空非法 emergency_action 不得自动升级为 emergency_intervention
    def test_unrecognized_emergency_action_demotes_to_clarification(self):
        res = IntentRouteResult(
            interaction_type="QUERY",
            confidence=0.9,
            reason="测试非法动作",
            dialogue_mode="knowledge_qa",
            emergency_action="shutdown",
        )
        self.assertEqual(res.dialogue_mode, "knowledge_qa")
        self.assertEqual(res.query_intent, "CLARIFICATION")
        self.assertIsNone(res.emergency_action)

    # 27. 同模式多次调用不产生重复记录
    def test_same_mode_transition_deduplication(self):
        self.dm.reset()
        self.dm._switch_dialogue_mode("knowledge_qa", reason="R1")
        count1 = len(self.dm.mode_transition_history)

        # 再次切换到相同的 mode
        self.dm._switch_dialogue_mode("knowledge_qa", reason="R2")
        count2 = len(self.dm.mode_transition_history)

        self.assertEqual(count1, count2)

    # 28. 真实 save_conversation -> load_history -> load_snapshot 端到端持久化测试
    def test_end_to_end_history_persistence_roundtrip(self):
        from src.history_manager import save_conversation, load_history
        from pathlib import Path
        import tempfile
        from unittest.mock import patch

        self.dm.reset()
        self.dm.process("ROV 最大水深是多少？")
        mode_before = self.dm.dialogue_mode
        history_before = copy.deepcopy(self.dm.mode_transition_history)

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("src.history_manager.get_history_dir", return_value=Path(tmpdir)):
                fname = save_conversation(
                    session_id="test_sess_001",
                    conversation_history=self.dm.conversation_history,
                    task_state=self.dm.task_state,
                    built_json=self.dm._last_built_json,
                    mode=self.dm.mode,
                    phase=self.dm.phase,
                    slot_store=self.dm.slot_store,
                    dialogue_mode=self.dm.dialogue_mode,
                    last_mode_transition=self.dm.last_mode_transition,
                    mode_transition_history=self.dm.mode_transition_history,
                    control_state=self.dm.control_state,
                    last_control_request=self.dm.last_control_request,
                )
                loaded_snap = load_history(fname)
                self.assertIsNotNone(loaded_snap)
                self.assertEqual(loaded_snap.get("dialogue_mode"), mode_before)

                new_dm = DialogueManager(self.llm, self.kb)
                new_dm.load_snapshot(loaded_snap)
                self.assertEqual(new_dm.dialogue_mode, mode_before)
                self.assertEqual(new_dm.mode_transition_history, history_before)

    # 29. 裸控制词（停止/暂停/取消/终止）进入 knowledge_qa/CLARIFICATION 且不清空任务草稿
    def test_bare_control_words_demote_to_clarification_without_clearing_draft(self):
        bare_words = ["停止", "暂停", "取消", "终止"]
        for word in bare_words:
            with self.subTest(word=word):
                self.dm.reset()
                self.dm.slot_store.slots["task_type_key"] = Slot("task_type_key", value="pipeline_inspection", status="valid")
                self.dm.task_state = self.dm.slot_store.get_task_state()

                route = self.dm.intent_router.route(
                    user_message=word,
                    conversation_history=[],
                    task_state=self.dm.task_state,
                    phase=self.dm.phase,
                    expected_slots=[],
                )
                self.assertEqual(route.dialogue_mode, "knowledge_qa")
                self.assertEqual(route.query_intent, "CLARIFICATION")
                self.assertIsNone(route.emergency_action)

                reply = self.dm.process(word)
                self.assertEqual(self.dm.dialogue_mode, "knowledge_qa")
                self.assertEqual(self.dm.task_state.get("task_type_key"), "pipeline_inspection")

    # 30. 无标点 ASR 复合命令识别测试
    def test_unpunctuated_asr_compound_commands(self):
        cases = [
            ("立即停止当前任务为什么设备还在下潜", "emergency_intervention", "stop"),
            ("马上暂停当前任务如果继续会怎样", "emergency_intervention", "pause"),
        ]
        for msg, expected_mode, expected_action in cases:
            with self.subTest(msg=msg):
                route = self.dm.intent_router.route(
                    user_message=msg,
                    conversation_history=[],
                    task_state={},
                    phase="collecting",
                    expected_slots=[],
                )
                self.assertEqual(route.dialogue_mode, expected_mode)
                self.assertEqual(route.emergency_action, expected_action)

    # 31. 快照 control_state 与 last_control_request 严格一致性校验测试
    def test_snapshot_control_state_and_request_consistency_validation(self):
        self.dm.reset()
        state_before_mode = self.dm.dialogue_mode

        inconsistent_snapshots = [
            {"control_state": "stop_requested", "last_control_request": None},
            {"control_state": "idle", "last_control_request": {"action": "stop", "status": "requested"}},
            {"control_state": "pause_requested", "last_control_request": {"action": "stop", "status": "requested"}},
            {"control_state": "stop_requested", "last_control_request": {"action": "stop", "status": "invalid_status"}},
        ]

        for snap in inconsistent_snapshots:
            with self.subTest(snap=snap):
                with self.assertRaises(ValueError):
                    self.dm.load_snapshot(snap)
                self.assertEqual(self.dm.dialogue_mode, state_before_mode)

    # 32. 否定控制短语安全边界与“暂停下潜”动作映射测试
    def test_negated_control_phrases_and_pause_dive_action(self):
        negated_phrases = [
            "不停止当前任务",
            "先不暂停当前任务",
            "暂不取消当前任务",
            "不需要终止当前操作",
            "不要立即停止当前任务",
        ]
        for msg in negated_phrases:
            with self.subTest(msg=msg):
                self.dm.reset()
                self.dm.slot_store.slots["task_type_key"] = Slot("task_type_key", value="pipeline_inspection", status="valid")
                self.dm.task_state = self.dm.slot_store.get_task_state()

                initial_phase = self.dm.phase
                initial_state = dict(self.dm.task_state)
                initial_control_state = self.dm.control_state
                slot_store_before = copy.deepcopy(self.dm.slot_store.export_snapshot())

                with patch.object(self.dm.extractor, "extract_updates") as mock_ext:
                    reply = self.dm.process(msg)
                    mock_ext.assert_not_called()

                self.assertNotEqual(self.dm.dialogue_mode, "emergency_intervention")
                self.assertEqual(self.dm.phase, initial_phase)
                self.assertEqual(self.dm.task_state, initial_state)
                self.assertEqual(self.dm.control_state, initial_control_state)
                self.assertEqual(self.dm.slot_store.export_snapshot(), slot_store_before)
                self.assertEqual(self.dm.task_state.get("task_type_key"), "pipeline_inspection")

        # 验证“暂停下潜”对应动作类型为 pause
        route = self.dm.intent_router.route(
            user_message="暂停下潜",
            conversation_history=[],
            task_state=self.dm.task_state,
            phase=self.dm.phase,
            expected_slots=[],
        )
        self.assertEqual(route.dialogue_mode, "emergency_intervention")
        self.assertEqual(route.emergency_action, "pause")

    # 33. 快照 confidence 布尔值/非有限浮点数/无时区 timestamp 严格校验测试
    def test_snapshot_strict_confidence_and_timestamp_validation(self):
        self.dm.reset()
        state_before_mode = self.dm.dialogue_mode

        invalid_snapshots = [
            {"mode_transition_history": [{"from": "task_collection", "to": "knowledge_qa", "confidence": True, "changed_at": "2026-08-02T12:00:00+00:00"}]},
            {"mode_transition_history": [{"from": "task_collection", "to": "knowledge_qa", "confidence": float("nan"), "changed_at": "2026-08-02T12:00:00+00:00"}]},
            {"mode_transition_history": [{"from": "task_collection", "to": "knowledge_qa", "confidence": float("inf"), "changed_at": "2026-08-02T12:00:00+00:00"}]},
            {"mode_transition_history": [{"from": "task_collection", "to": "knowledge_qa", "confidence": 0.9, "changed_at": "2026-08-02 12:00:00"}]},
        ]

        for snap in invalid_snapshots:
            with self.subTest(snap=snap):
                with self.assertRaises(ValueError):
                    self.dm.load_snapshot(snap)
                self.assertEqual(self.dm.dialogue_mode, state_before_mode)

    # 34. 包含“不”的状态描述句子不能误压制紧急命令测试
    def test_status_descriptions_with_bu_do_not_suppress_emergency_actions(self):
        cases = [
            ("设备状态不明立即停止当前任务", "emergency_intervention", "stop"),
            ("信号不稳马上暂停当前任务", "emergency_intervention", "pause"),
            ("定位不可靠立即终止当前操作", "emergency_intervention", "abort"),
        ]
        for msg, expected_mode, expected_action in cases:
            with self.subTest(msg=msg):
                self.dm.reset()
                self.dm.slot_store.slots["task_type_key"] = Slot("task_type_key", value="pipeline_inspection", status="valid")
                self.dm.task_state = self.dm.slot_store.get_task_state()

                route = self.dm.intent_router.route(
                    user_message=msg,
                    conversation_history=[],
                    task_state=self.dm.task_state,
                    phase=self.dm.phase,
                    expected_slots=[],
                )
                self.assertEqual(route.dialogue_mode, expected_mode)
                self.assertEqual(route.emergency_action, expected_action)

                with patch.object(self.dm.extractor, "extract_updates") as mock_ext:
                    reply = self.dm.process(msg)
                    mock_ext.assert_not_called()

                self.assertEqual(self.dm.dialogue_mode, expected_mode)
                self.assertEqual(self.dm.control_state, "idle")
                self.assertEqual(self.dm.task_state.get("task_type_key"), "pipeline_inspection")

    # 35. 复合句否定+肯定动作以及非任务控制对象隔离测试
    def test_compound_and_object_level_emergency_routing(self):
        emergency_cases = [
            ("不是要停止当前任务而是暂停当前任务", "emergency_intervention", "pause"),
            ("不要暂停当前任务而是立即停止当前任务", "emergency_intervention", "stop"),
            ("不是要取消当前任务而是终止当前任务", "emergency_intervention", "abort"),
            ("立即停止当前任务并输出状态", "emergency_intervention", "stop"),
            ("停止回答并立即停止当前任务", "emergency_intervention", "stop"),
            ("暂停回答并立即停止当前任务", "emergency_intervention", "stop"),
        ]

        for msg, expected_mode, expected_action in emergency_cases:
            with self.subTest(msg=msg):
                self.dm.reset()
                self.dm.slot_store.slots["task_type_key"] = Slot("task_type_key", value="pipeline_inspection", status="valid")
                self.dm.task_state = self.dm.slot_store.get_task_state()

                route = self.dm.intent_router.route(
                    user_message=msg,
                    conversation_history=[],
                    task_state=self.dm.task_state,
                    phase=self.dm.phase,
                    expected_slots=[],
                )
                self.assertEqual(route.dialogue_mode, expected_mode)
                self.assertEqual(route.emergency_action, expected_action)

                with patch.object(self.dm.extractor, "extract_updates") as mock_ext:
                    reply = self.dm.process(msg)
                    mock_ext.assert_not_called()

                self.assertEqual(self.dm.dialogue_mode, expected_mode)
                self.assertEqual(self.dm.control_state, "idle")
                self.assertEqual(self.dm.task_state.get("task_type_key"), "pipeline_inspection")

        non_emergency_cases = [
            "不要停止当前任务而是继续巡检",
            "立即停止回答",
        ]
        for msg in non_emergency_cases:
            with self.subTest(msg=msg):
                self.dm.reset()
                self.dm.slot_store.slots["task_type_key"] = Slot("task_type_key", value="pipeline_inspection", status="valid")
                self.dm.task_state = self.dm.slot_store.get_task_state()

                route = self.dm.intent_router.route(
                    user_message=msg,
                    conversation_history=[],
                    task_state=self.dm.task_state,
                    phase=self.dm.phase,
                    expected_slots=[],
                )
                self.assertNotEqual(route.dialogue_mode, "emergency_intervention")

                with patch.object(self.dm.extractor, "extract_updates", return_value={"intent": "ORDINARY_QA", "slot_candidates": []}):
                    reply = self.dm.process(msg)

                self.assertNotEqual(self.dm.dialogue_mode, "emergency_intervention")
                self.assertEqual(self.dm.task_state.get("task_type_key"), "pipeline_inspection")

    def test_non_task_pause_then_task_stop_selects_stop(self):
        msg = "暂停回答并立即停止当前任务"

        self.dm.reset()
        self.dm.slot_store.slots["task_type_key"] = Slot("task_type_key", value="pipeline_inspection", status="valid")
        self.dm.task_state = self.dm.slot_store.get_task_state()

        route = self.dm.intent_router.route(
            user_message=msg,
            conversation_history=[],
            task_state=self.dm.task_state,
            phase=self.dm.phase,
            expected_slots=[],
        )

        self.assertEqual(route.dialogue_mode, "emergency_intervention")
        self.assertEqual(route.emergency_action, "stop")

        with patch.object(self.dm.extractor, "extract_updates") as mock_ext:
            reply = self.dm.process(msg)
            mock_ext.assert_not_called()

        self.assertEqual(self.dm.dialogue_mode, "emergency_intervention")
        self.assertEqual(self.dm.control_state, "idle")


    def test_mode_definitions_exclude_uncertain(self):
        from src.intent_router import VALID_DIALOGUE_MODES
        self.assertNotIn("uncertain", VALID_DIALOGUE_MODES)

    def test_broad_device_queries_return_device_list(self):
        queries = [
            "查询设备",
            "我要查询设备",
            "查看设备",
            "查看设备列表",
            "列出设备",
            "列出可用设备",
            "查询机器人",
            "查看机器人",
        ]
        for q in queries:
            with self.subTest(query=q):
                route = self.dm.intent_router.route(q, [], {})
                self.assertEqual(route.dialogue_mode, "knowledge_qa")
                self.assertEqual(route.query_intent, "DEVICE_CAPABILITY")

                kb_res = self.kb.execute_typed_query("DEVICE_CAPABILITY", q)
                self.assertEqual(kb_res.get("query_mode"), "device_list")
                self.assertTrue(kb_res.get("found"))
                self.assertTrue(len(kb_res.get("results", [])) > 0)

                self.dm.reset()
                reply = self.dm.process(q)
                self.assertNotIn("当前知识库未提供该信息", reply)
                self.assertEqual(self.dm.task_state, {})

    def test_legacy_uncertain_snapshot_compatibility(self):
        self.dm.reset()
        self.dm.slot_store.slots["task_type_key"] = Slot("task_type_key", value="pipeline_inspection", status="valid")
        self.dm.task_state = self.dm.slot_store.get_task_state()

        snap = self.dm.export_snapshot()
        snap["dialogue_mode"] = "uncertain"

        self.dm.reset()
        self.dm.load_snapshot(snap)

        self.assertEqual(self.dm.dialogue_mode, "knowledge_qa")
        self.assertEqual(self.dm.task_state.get("task_type_key"), "pipeline_inspection")
        self.assertEqual(self.dm.phase, "collecting")

    def test_broad_device_query_uses_llm_evidence(self):
        with patch.object(self.llm, "chat", return_value="系统当前支持天鹰座与金牛座系列机器人作业。") as mock_chat:
            self.dm.reset()
            reply = self.dm.process("查询设备")
            mock_chat.assert_called_once()
            call_args = mock_chat.call_args[0][0]
            prompt_str = str(call_args)
            self.assertTrue("equipment_payload_mapping" in prompt_str or "model_variants" in prompt_str or "full_name" in prompt_str or "ROV" in prompt_str or "device_list" in prompt_str or "all_rovs" in prompt_str or "天鹰座" in prompt_str or "金牛座" in prompt_str or "OBSROV" in prompt_str)
            self.assertEqual(reply, "系统当前支持天鹰座与金牛座系列机器人作业。")
            self.assertNotIn("{", reply)

    def test_broad_device_query_empty_llm_uses_evidence_fallback(self):
        with patch.object(self.llm, "chat", return_value=""):
            self.dm.reset()
            reply = self.dm.process("查看可用设备列表")
            self.assertNotEqual(reply, "当前知识库未提供该信息。")
            self.assertIn("当前可查询的设备包括：", reply)
            self.assertTrue(any(name in reply for name in ("履带式", "机器人", "ROV", "AUV", "金牛座", "天鹰座")))
            self.assertNotIn("{", reply)

    def test_device_query_packaging_is_read_only(self):
        v_before = self.dm.slot_store.version
        state_before = dict(self.dm.task_state)
        phase_before = self.dm.phase
        snap_before = self.dm.slot_store.export_snapshot()

        with patch.object(self.dm.extractor, "extract_updates") as mock_ext, patch.object(self.dm.slot_store, "commit_transaction") as mock_commit:
            reply = self.dm.process("查看设备列表")
            mock_ext.assert_not_called()
            mock_commit.assert_not_called()

        self.assertEqual(self.dm.slot_store.version, v_before)
        self.assertEqual(self.dm.task_state, state_before)
        self.assertEqual(self.dm.phase, phase_before)
        self.assertEqual(self.dm.slot_store.export_snapshot(), snap_before)

    def test_issue24_payload_and_tool_query_regressions(self):
        cases = [
            ("payload", "TOOL_QUERY"),
            ("机器人的负载有哪些", "TOOL_QUERY"),
            ("机器人的 payload 有哪些", "TOOL_QUERY"),
            ("机器人支持的设备有哪些", ("TOOL_QUERY", "DEVICE_CAPABILITY")),
            ("机器人可以使用哪些工具？", "TOOL_QUERY"),
        ]
        for query, expected_intent in cases:
            with self.subTest(query=query):
                route = self.dm.intent_router.route(query, [], {})
                self.assertEqual(route.dialogue_mode, "knowledge_qa")
                if isinstance(expected_intent, tuple):
                    self.assertIn(route.query_intent, expected_intent)
                else:
                    self.assertEqual(route.query_intent, expected_intent)

                self.dm.reset()
                reply = self.dm.process(query)
                self.assertNotIn("可能是在提交任务信息", reply)
                self.assertNotIn("当前知识库未提供该信息", reply)
                self.assertNotIn("{", reply)
                self.assertTrue(len(reply) > 5)


class TestReadOnlyPriorityFullMatrix(unittest.TestCase):
    def setUp(self):
        self.kb = KnowledgeBase()
        self.llm = DummyLLM()
        self.dm = DialogueManager(self.llm, self.kb)

    def test_read_only_routing_and_reply_matrix(self):
        test_matrix = [
            ("什么是软约束？", "KNOWLEDGE_QA", ["软约束", "警告"], ["可能是在提交任务信息"]),
            ("水下机器人为什么需要定位？", "KNOWLEDGE_QA", ["定位"], ["可能是在提交任务信息"]),
            ("侧扫声呐有什么作用？", "TOOL_QUERY", ["侧扫声呐", "成像", "地貌", "声学", "扫测"], ["可能是在提交任务信息", "当前知识库未提供该信息"]),
            ("介绍一下金牛座机器人", "DEVICE_CAPABILITY", ["金牛座", "机器人"], ["可能是在提交任务信息"]),
            ("金牛座能执行什么任务？", "DEVICE_CAPABILITY", ["金牛座", "任务", "巡检", "埋设", "采油树"], ["可能是在提交任务信息"]),
            ("AUV 和 ROV 有什么区别？", "KNOWLEDGE_QA", ["AUV", "ROV"], ["可能是在提交任务信息"]),
            ("有哪些机器人可以搭载机械臂？", "DEVICE_CAPABILITY", ["机械臂", "机器人"], ["可能是在提交任务信息"]),
            ("这个 payload 是干什么的？", "TOOL_QUERY", ["工具", "载荷", "能力", "说明"], ["可能是在提交任务信息"]),
            ("目前有哪些机器人？", "DEVICE_CAPABILITY", ["天鹰座", "金牛座", "机器人"], ["可能是在提交任务信息"]),
            ("金牛座属于哪个 class/family？", "DEVICE_CAPABILITY", ["金牛座", "class", "family", "类", "族"], ["可能是在提交任务信息"]),
            ("金牛座的载荷、能力和限制是什么？", "DEVICE_CAPABILITY", ["金牛座", "水深", "能力"], ["可能是在提交任务信息"]),
            ("当前任务还缺哪些信息？", "TASK_STATUS", ["任务", "阶段", "收集"], ["可能是在提交任务信息"]),
            ("刚才填写了哪些任务参数？", "TASK_STATUS", ["任务", "字段"], ["可能是在提交任务信息"]),
            ("怎么创建一个巡检任务？", "KNOWLEDGE_QA", ["巡检", "任务"], ["可能是在提交任务信息"]),
            ("为什么任务被硬约束阻断？", "KNOWLEDGE_QA", ["硬约束", "阻断"], ["可能是在提交任务信息"]),
            ("如何忽略软警告？", "KNOWLEDGE_QA", ["软警告", "确认", "忽略"], ["可能是在提交任务信息"]),
            ("任务发布后保存在哪里？", "KNOWLEDGE_QA", ["staging", "final", "发布", "保存"], ["可能是在提交任务信息"]),
        ]

        for msg, expected_intent, must_include, must_not_include in test_matrix:
            with self.subTest(msg=msg):
                route = self.dm.intent_router.route(msg, [], {})
                self.assertEqual(route.dialogue_mode, "knowledge_qa")
                self.assertEqual(route.query_intent, expected_intent)

                self.dm.reset()
                reply = self.dm.process(msg)
                for exc in must_not_include:
                    self.assertNotIn(exc, reply)
                self.assertTrue(len(reply) > 5)

    def test_boundary_and_action_routing_matrix(self):
        boundary_cases = [
            ("让机器人 A 去检查管道", "task_collection", "WRITE", None),
            ("把机器人换成 B", "task_collection", "WRITE", None),
            ("立即停止机器人", "emergency_intervention", "QUERY", "stop"),
            ("机器人 A 当前电量是多少", "knowledge_qa", "QUERY", "DEVICE_STATUS"),
            ("帮我看看机器人", "knowledge_qa", "QUERY", "CLARIFICATION"),
            ("payload", "knowledge_qa", "QUERY", "TOOL_QUERY"),
            ("机器人的负载有哪些", "knowledge_qa", "QUERY", "TOOL_QUERY"),
            ("机器人的 payload 有哪些", "knowledge_qa", "QUERY", "TOOL_QUERY"),
            ("机器人支持的设备有哪些", "knowledge_qa", "QUERY", "TOOL_QUERY"),
        ]

        for msg, expected_mode, expected_it, expected_sub in boundary_cases:
            with self.subTest(msg=msg):
                route = self.dm.intent_router.route(msg, [], {})
                self.assertEqual(route.dialogue_mode, expected_mode)
                self.assertEqual(route.interaction_type, expected_it)
                if expected_mode == "emergency_intervention":
                    self.assertEqual(route.emergency_action, expected_sub)
                elif expected_mode == "knowledge_qa":
                    self.assertEqual(route.query_intent, expected_sub)


if __name__ == "__main__":
    unittest.main()

