"""
tests/test_intent_routing.py - 独立意图路由、问句与动作门控、状态不变性及错误结构回归测试
"""

import json
import unittest
from unittest.mock import MagicMock, patch

from src.dialogue_manager import DialogueManager
from src.intent_router import IntentRouter, IntentRouteResult
from src.knowledge_retriever import KnowledgeBase
from src.llm_client import LLMClient
from src.validator import Violation
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


class TestIntentRoutingAndInvariance(unittest.TestCase):
    def setUp(self):
        self.kb = KnowledgeBase()
        self.llm = DummyLLM()
        self.router = IntentRouter(self.llm)
        self.dm = DialogueManager(self.llm, self.kb)

    # 1. “这个任务适合使用什么工具？” → TOOL_QUERY
    def test_r01_tool_query_routing(self):
        res = self.router.route("这个任务适合使用什么工具？", [], {})
        self.assertEqual(res.intent, "TOOL_QUERY")
        self.assertFalse(res.should_update_slots)

    # 2. 已有任务时同一句仍为 TOOL_QUERY
    def test_r02_active_task_tool_query_routing(self):
        task_state = {"task_type": "管缆巡检", "task_type_key": "pipeline_inspection"}
        res = self.router.route("这个任务适合使用什么工具？", [], task_state)
        self.assertEqual(res.intent, "TOOL_QUERY")
        self.assertFalse(res.should_update_slots)

    # 3. 已有任务时“谢谢” → GENERAL_CHAT
    def test_r03_active_task_thanks_routing(self):
        task_state = {"task_type": "管缆巡检", "task_type_key": "pipeline_inspection"}
        res = self.router.route("谢谢", [], task_state)
        self.assertEqual(res.intent, "GENERAL_CHAT")
        self.assertFalse(res.should_update_slots)

    # 4. 已有任务时无关输入 → CLARIFICATION 或 UNKNOWN
    def test_r04_active_task_irrelevant_input_routing(self):
        task_state = {"task_type": "管缆巡检", "task_type_key": "pipeline_inspection"}
        res = self.router.route("今天天气不错啊", [], task_state)
        self.assertIn(res.intent, ("CLARIFICATION", "UNKNOWN"))
        self.assertFalse(res.should_update_slots)

    # 5. “不好，水深改成500米”不得确认发布
    def test_r05_negation_confirm_does_not_confirm(self):
        self.dm.phase = "confirming"
        self.dm.task_state = {"task_type": "管缆巡检", "task_type_key": "pipeline_inspection"}
        res = self.router.route("不好，水深改成500米", [], self.dm.task_state, phase=self.dm.phase)
        self.assertNotEqual(res.intent, "TASK_CONFIRM")
        self.assertIn(res.intent, ("TASK_CREATE", "TASK_UPDATE"))

    # 6. “不确认”不得确认发布
    def test_r06_unconfirm_does_not_confirm(self):
        self.dm.phase = "confirming"
        res = self.router.route("不确认", [], self.dm.task_state, phase=self.dm.phase)
        self.assertNotEqual(res.intent, "TASK_CONFIRM")

    # 7. “不要发布”不得确认发布
    def test_r07_dont_publish_does_not_confirm(self):
        self.dm.phase = "confirming"
        res = self.router.route("不要发布", [], self.dm.task_state, phase=self.dm.phase)
        self.assertNotEqual(res.intent, "TASK_CONFIRM")

    # 8. “不要取消任务”不得取消任务
    def test_r08_dont_cancel_does_not_cancel(self):
        res = self.router.route("不要取消任务", [], self.dm.task_state, phase="collecting")
        self.assertNotEqual(res.intent, "TASK_CANCEL")

    # 9. “确认发布”在 confirming 阶段正常发布
    def test_r09_confirm_publish_in_confirming_phase(self):
        res = self.router.route("确认发布", [], self.dm.task_state, phase="confirming")
        self.assertEqual(res.intent, "TASK_CONFIRM")

    # 10. “取消当前任务”正常取消
    def test_r10_cancel_current_task(self):
        res = self.router.route("取消当前任务", [], self.dm.task_state)
        self.assertEqual(res.intent, "TASK_CANCEL")

    # 11. 知识问题包含500、1000等数字不得写入 water_depth
    def test_r11_knowledge_query_numbers_no_slot_mutation(self):
        v_before = self.dm.slot_store.version
        reply = self.dm.process("500米级机器人有哪些？")
        v_after = self.dm.slot_store.version
        self.assertEqual(v_before, v_after)
        wd = self.dm.slot_store.slots.get("water_depth")
        self.assertTrue(wd is None or wd.value is None)

    # 12. 每一种非任务路由均不调用 extractor 和 commit_transaction
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

    # 13. 非任务路由前后完整状态快照一致
    def test_r13_non_task_route_snapshot_invariance(self):
        v_before = self.dm.slot_store.version
        snap_before = self.dm.slot_store.export_snapshot()
        self.dm.process("机器人可以使用哪些工具？")
        v_after = self.dm.slot_store.version
        snap_after = self.dm.slot_store.export_snapshot()
        self.assertEqual(v_before, v_after)
        self.assertEqual(snap_before, snap_after)

    # 14. 500米级设备查询必须真正过滤结果
    def test_r14_device_capability_500m_filtering(self):
        res = self.kb.execute_typed_query("DEVICE_CAPABILITY", "500米级机器人有哪些？")
        self.assertTrue(res["found"])
        for r in res["results"]:
            self.assertEqual(r.get("max_depth_m"), 500)

    # 15. KB found=false 时回复不得编造事实
    def test_r15_kb_not_found_no_hallucination(self):
        res = self.kb.execute_typed_query("DEVICE_CAPABILITY", "超光速神潜器9000")
        self.assertFalse(res["found"])
        self.assertEqual(len(res["results"]), 0)

    # ── 恢复上一版被删除的基础路由测试 ──
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

    # ── 新增 20 项核心控制与逻辑验证测试 ──

    # 1. “为什么使用ROV？”不得TASK_UPDATE
    def test_n01_why_use_rov_no_task_update(self):
        res = self.router.route("为什么使用ROV？", [], {})
        self.assertNotEqual(res.intent, "TASK_UPDATE")
        self.assertFalse(res.should_update_slots)

    # 2. 已有任务时同一句仍不得写槽位
    def test_n02_active_task_why_use_rov_no_slot_update(self):
        task_state = {"task_type": "管缆巡检", "task_type_key": "pipeline_inspection"}
        res = self.router.route("为什么使用ROV？", [], task_state)
        self.assertFalse(res.should_update_slots)

    # 3. “如何选择机器人？”不得TASK_UPDATE
    def test_n03_how_to_choose_robot_no_task_update(self):
        res = self.router.route("如何选择机器人？", [], {})
        self.assertNotEqual(res.intent, "TASK_UPDATE")
        self.assertFalse(res.should_update_slots)

    # 4. “水深多少？”不得TASK_CREATE
    def test_n04_what_is_water_depth_no_task_create(self):
        res = self.router.route("水深多少？", [], {})
        self.assertNotEqual(res.intent, "TASK_CREATE")
        self.assertFalse(res.should_update_slots)

    # 5. 已有任务时“1+1等于几？”不得TASK_UPDATE
    def test_n05_active_task_math_question_no_task_update(self):
        task_state = {"task_type": "管缆巡检", "task_type_key": "pipeline_inspection"}
        res = self.router.route("1+1等于几？", [], task_state)
        self.assertNotEqual(res.intent, "TASK_UPDATE")
        self.assertFalse(res.should_update_slots)

    # 6. “当前任务有哪些参数？”→ TASK_STATUS
    def test_n06_current_task_params_task_status(self):
        res = self.router.route("当前任务有哪些参数？", [], {})
        self.assertEqual(res.intent, "TASK_STATUS")

    # 7. “管缆巡检需要哪些参数？”→ KNOWLEDGE_QA
    def test_n07_pipeline_inspection_params_knowledge_qa(self):
        res = self.router.route("管缆巡检需要哪些参数？", [], {})
        self.assertEqual(res.intent, "KNOWLEDGE_QA")

    # 8. “有哪些机器人？”→ DEVICE_CAPABILITY且KB found=True
    def test_n08_what_robots_available_device_capability_found(self):
        res = self.router.route("有哪些机器人？", [], {})
        self.assertEqual(res.intent, "DEVICE_CAPABILITY")
        kb_res = self.kb.execute_typed_query("DEVICE_CAPABILITY", "有哪些机器人？")
        self.assertTrue(kb_res["found"])
        self.assertGreater(len(kb_res["results"]), 0)

    # 9. “支持500米水深的机器人有哪些？”→ DEVICE_CAPABILITY
    def test_n09_support_500m_robots_device_capability(self):
        res = self.router.route("支持500米水深的机器人有哪些？", [], {})
        self.assertEqual(res.intent, "DEVICE_CAPABILITY")
        kb_res = self.kb.execute_typed_query("DEVICE_CAPABILITY", "支持500米水深的机器人有哪些？")
        self.assertTrue(kb_res["found"])

    # 10. “能够在500米作业的机器人有哪些？”→ DEVICE_CAPABILITY
    def test_n10_able_to_work_500m_robots_device_capability(self):
        res = self.router.route("能够在500米作业的机器人有哪些？", [], {})
        self.assertEqual(res.intent, "DEVICE_CAPABILITY")
        kb_res = self.kb.execute_typed_query("DEVICE_CAPABILITY", "能够在500米作业的机器人有哪些？")
        self.assertTrue(kb_res["found"])

    # 11. 上述设备查询不得写water_depth
    def test_n11_device_queries_do_not_mutate_water_depth(self):
        queries = ["有哪些机器人？", "支持500米水深的机器人有哪些？", "能够在500米作业的机器人有哪些？"]
        for q in queries:
            v_before = self.dm.slot_store.version
            self.dm.process(q)
            v_after = self.dm.slot_store.version
            self.assertEqual(v_before, v_after)
            wd = self.dm.slot_store.slots.get("water_depth")
            self.assertTrue(wd is None or wd.value is None)

    # 12. 缺少confidence不得修改槽位
    def test_n12_missing_confidence_no_slot_update(self):
        with patch.object(self.llm, 'extract_json', return_value={"intent": "TASK_CREATE", "reason": "test"}) as mock_ext:
            res = self.router.route("模糊问句", [], {})
            mock_ext.assert_called_once()
            self.assertEqual(res.intent, "CLARIFICATION")
            self.assertFalse(res.should_update_slots)

    # 13. 所有非法confidence均进入CLARIFICATION
    def test_n13_all_invalid_confidences_fall_to_clarification(self):
        invalid_confidences = [None, "high", True, False, -0.1, 1.1, float('nan'), float('inf')]
        for c in invalid_confidences:
            with patch.object(self.llm, 'extract_json', return_value={"intent": "TASK_CREATE", "confidence": c, "reason": "test"}):
                res = self.router.route("模糊问句测试", [], {})
                self.assertEqual(res.intent, "CLARIFICATION")
                self.assertFalse(res.should_update_slots)

    # 14. blocked_soft + “确认继续”正常走软警告流程
    def test_n14_blocked_soft_confirm_continue_flow(self):
        from src.slot_store import Slot
        self.dm.phase = "blocked_soft"
        self.dm.slot_store.slots["task_type_key"] = Slot("task_type_key", value="pipeline_inspection", status="valid")
        self.dm.slot_store.slots["task_type"] = Slot("task_type", value="管缆巡检", status="valid")
        self.dm.slot_store.slots["water_depth"] = Slot("water_depth", value=500.0, status="valid")
        self.dm._rebuild_cache()

        v = Violation("depth_vs_rov_limit", "水深较大预警", "水深较大预警", "soft", related_fields=["water_depth"])
        self.dm._blocking_violations = [v]

        reply = self.dm.process("确认继续")
        self.assertNotEqual(self.dm.phase, "blocked_soft")

    # 15. blocked_soft + “忽略警告”正常走软警告流程
    def test_n15_blocked_soft_ignore_warning_flow(self):
        from src.slot_store import Slot
        self.dm.phase = "blocked_soft"
        self.dm.slot_store.slots["task_type_key"] = Slot("task_type_key", value="pipeline_inspection", status="valid")
        self.dm.slot_store.slots["task_type"] = Slot("task_type", value="管缆巡检", status="valid")
        self.dm.slot_store.slots["water_depth"] = Slot("water_depth", value=500.0, status="valid")
        self.dm._rebuild_cache()

        v = Violation("depth_vs_rov_limit", "水深较大预警", "水深较大预警", "soft", related_fields=["water_depth"])
        self.dm._blocking_violations = [v]

        reply = self.dm.process("忽略警告")
        self.assertNotEqual(self.dm.phase, "blocked_soft")

    # 16. confirming + “确认发布”正常走发布流程
    def test_n16_confirming_confirm_publish_flow(self):
        from pathlib import Path
        import tempfile
        from tests.test_slot_consistency import seed_complete_valid_pipeline_task
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir) / "task"
            tmp_path.mkdir(parents=True, exist_ok=True)
            seed_complete_valid_pipeline_task(self.dm, self.kb)
            self.dm.phase = "confirming"

            with patch("src.task_intent_builder.get_task_dir", return_value=tmp_path), \
                 patch("src.id_sequence.get_result_dir", return_value=Path(tmp_dir)):
                reply = self.dm.process("确认发布")
                self.assertEqual(self.dm.phase, "done")

    # 17. 非确认阶段“确认发布”不得发布
    def test_n17_non_confirming_confirm_publish_no_publish(self):
        self.dm.phase = "collecting"
        self.dm.task_state = {}
        self.dm._rebuild_cache()

        reply = self.dm.process("确认发布")
        self.assertNotEqual(self.dm.phase, "done")

    # 18. 当前任务工具查询前后slot_store完整快照一致
    def test_n18_tool_query_with_active_task_snapshot_invariance(self):
        self.dm.task_state = {"task_type": "管缆巡检", "task_type_key": "pipeline_inspection", "water_depth": 300.0}
        self.dm._rebuild_cache()

        v_before = self.dm.slot_store.version
        snap_before = self.dm.slot_store.export_snapshot()

        reply = self.dm.process("这个任务适合使用什么工具？")

        v_after = self.dm.slot_store.version
        snap_after = self.dm.slot_store.export_snapshot()

        self.assertEqual(v_before, v_after)
        self.assertEqual(snap_before, snap_after)

    # 19. 直接回答expected_slot仍能正常补槽
    def test_n19_answer_expected_slot_normal_filling(self):
        from src.slot_store import Slot
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

    # 20. 未回答expected_slot的无关数字不得补槽
    def test_n20_irrelevant_math_question_with_number_no_slot_filling(self):
        self.dm.task_state = {"task_type": "管缆巡检", "task_type_key": "pipeline_inspection"}
        self.dm._last_missing = [{"key": "water_depth", "label": "水深（米）"}]

        v_before = self.dm.slot_store.version
        reply = self.dm.process("1+1等于几？")
        v_after = self.dm.slot_store.version

        self.assertEqual(v_before, v_after)
        wd = self.dm.slot_store.slots.get("water_depth")
        self.assertTrue(wd is None or wd.value is None)


if __name__ == "__main__":
    unittest.main()
