"""
tests/test_intent_routing_matrix.py - G7 交互计划与 Read-First 路由测试矩阵
"""

import copy
import unittest

from src.dialogue_manager import DialogueManager
from src.intent_router import IntentRouter
from src.knowledge_retriever import KnowledgeBase
from src.llm_client import LLMClient


class DummyLLM(LLMClient):
    def __init__(self):
        self.llm = None

    def chat(self, messages: list[dict], temperature: float = 0.7, max_tokens: int = 800) -> str:
        return "默认测试回复"


class TestIntentRoutingMatrixG7(unittest.TestCase):
    def setUp(self):
        self.kb = KnowledgeBase()
        self.llm = DummyLLM()
        self.router = IntentRouter(self.llm)
        self.dm = DialogueManager(self.llm, self.kb)

    # ══════════════════════════════════════════════════════════════════════
    # 1. READ 矩阵验证
    # ══════════════════════════════════════════════════════════════════════

    def test_read_01_soft_constraint_definition(self):
        res = self.router.route("什么是软约束", [], {})
        plan = res.interaction_plan
        self.assertIsNotNone(plan)
        self.assertEqual(plan.operation, "READ")
        self.assertEqual(plan.dialogue_mode, "knowledge_qa")
        self.assertEqual(plan.subject_type, "system_rule")
        self.assertFalse(plan.needs_clarification)

    def test_read_02_describe_taurus(self):
        res = self.router.route("介绍金牛座", [], {})
        plan = res.interaction_plan
        self.assertIsNotNone(plan)
        self.assertEqual(plan.operation, "READ")
        self.assertEqual(plan.dialogue_mode, "knowledge_qa")
        self.assertEqual(plan.subject_type, "device")
        self.assertEqual(plan.subject_text, "金牛座")
        self.assertEqual(plan.relation, "describe")
        self.assertEqual(plan.source_policy, "project_kb")

    def test_read_03_taurus_family_belongs_to(self):
        res = self.router.route("金牛座属于哪个 family", [], {})
        plan = res.interaction_plan
        self.assertIsNotNone(plan)
        self.assertEqual(plan.operation, "READ")
        self.assertEqual(plan.subject_type, "device")
        self.assertEqual(plan.subject_text, "金牛座")
        self.assertEqual(plan.relation, "belongs_to")
        self.assertEqual(plan.source_policy, "project_kb")

    def test_read_04_taurus_supported_payloads(self):
        res = self.router.route("金牛座支持哪些 payload", [], {})
        plan = res.interaction_plan
        self.assertIsNotNone(plan)
        self.assertEqual(plan.operation, "READ")
        self.assertEqual(plan.relation, "supports")
        self.assertEqual(plan.source_policy, "project_kb")

    def test_read_05_robots_supporting_manipulator(self):
        res = self.router.route("哪些机器人支持机械臂", [], {})
        plan = res.interaction_plan
        self.assertIsNotNone(plan)
        self.assertEqual(plan.operation, "READ")
        self.assertEqual(plan.relation, "supports")
        self.assertEqual(plan.source_policy, "project_kb")

    def test_read_06_auv_rov_compare(self):
        res = self.router.route("AUV 和 ROV 有什么区别", [], {})
        plan = res.interaction_plan
        self.assertIsNotNone(plan)
        self.assertEqual(plan.operation, "READ")
        self.assertEqual(plan.subject_type, "device_class")
        self.assertEqual(plan.relation, "compare")
        self.assertEqual(plan.source_policy, "hybrid")

    def test_read_07_missing_fields(self):
        res = self.router.route("当前任务还缺什么", [], {"task_type_key": "pipeline_inspection"})
        plan = res.interaction_plan
        self.assertIsNotNone(plan)
        self.assertEqual(plan.operation, "READ")
        self.assertEqual(plan.subject_type, "task")
        self.assertEqual(plan.relation, "missing_fields")
        self.assertEqual(plan.source_policy, "session_state")

    def test_read_08_filled_fields(self):
        res = self.router.route("刚才已经填写了什么", [], {"task_type_key": "pipeline_inspection", "water_depth": 500.0})
        plan = res.interaction_plan
        self.assertIsNotNone(plan)
        self.assertEqual(plan.operation, "READ")
        self.assertEqual(plan.subject_type, "task")
        self.assertEqual(plan.relation, "filled_fields")
        self.assertEqual(plan.source_policy, "session_state")

    def test_read_09_rov_depth_capability_question(self):
        res = self.router.route("ROV可以在500米工作吗？", [], {})
        plan = res.interaction_plan
        self.assertIsNotNone(plan)
        self.assertEqual(plan.operation, "READ")
        self.assertEqual(plan.dialogue_mode, "knowledge_qa")
        self.assertEqual(plan.relation, "capabilities")

    def test_read_10_stop_impact_question(self):
        res = self.router.route("停止任务会有什么影响", [], {})
        plan = res.interaction_plan
        self.assertIsNotNone(plan)
        self.assertEqual(plan.operation, "READ")
        self.assertEqual(plan.dialogue_mode, "knowledge_qa")
        self.assertIsNone(plan.emergency_action)

    def test_read_11_conditional_stop_question(self):
        res = self.router.route("如果停止任务会怎样", [], {})
        plan = res.interaction_plan
        self.assertIsNotNone(plan)
        self.assertEqual(plan.operation, "READ")
        self.assertEqual(plan.dialogue_mode, "knowledge_qa")
        self.assertIsNone(plan.emergency_action)

    # ══════════════════════════════════════════════════════════════════════
    # 2. WRITE 矩阵验证
    # ══════════════════════════════════════════════════════════════════════

    def test_write_01_create_pipeline_task(self):
        res = self.router.route("创建一个管缆巡检任务", [], {})
        plan = res.interaction_plan
        self.assertIsNotNone(plan)
        self.assertEqual(plan.operation, "WRITE")
        self.assertEqual(plan.dialogue_mode, "task_collection")

    def test_write_02_observation_rov(self):
        res = self.router.route("让观察级机器人执行巡检", [], {})
        plan = res.interaction_plan
        self.assertIsNotNone(plan)
        self.assertEqual(plan.operation, "WRITE")
        self.assertEqual(plan.dialogue_mode, "task_collection")

    def test_write_03_modify_water_depth(self):
        res = self.router.route("水深改成500米", [], {})
        plan = res.interaction_plan
        self.assertIsNotNone(plan)
        self.assertEqual(plan.operation, "WRITE")
        self.assertEqual(plan.dialogue_mode, "task_collection")

    def test_write_04_change_robot_aquila(self):
        res = self.router.route("把机器人换成天鹰座", [], {})
        plan = res.interaction_plan
        self.assertIsNotNone(plan)
        self.assertEqual(plan.operation, "WRITE")
        self.assertEqual(plan.dialogue_mode, "task_collection")

    def test_write_05_add_camera_payload(self):
        res = self.router.route("增加高清摄像机", [], {})
        plan = res.interaction_plan
        self.assertIsNotNone(plan)
        self.assertEqual(plan.operation, "WRITE")
        self.assertEqual(plan.dialogue_mode, "task_collection")

    def test_write_06_expected_slot_direct_answer(self):
        res = self.router.route("海底油气管道", [], {"task_type_key": "pipeline_inspection"}, expected_slots=["cable_type"])
        plan = res.interaction_plan
        self.assertIsNotNone(plan)
        self.assertEqual(plan.operation, "WRITE")
        self.assertEqual(plan.dialogue_mode, "task_collection")

    # ══════════════════════════════════════════════════════════════════════
    # 3. CONTROL 矩阵验证
    # ══════════════════════════════════════════════════════════════════════

    def test_control_01_stop_immediately(self):
        res = self.router.route("立即停止当前任务", [], {})
        plan = res.interaction_plan
        self.assertIsNotNone(plan)
        self.assertEqual(plan.operation, "CONTROL")
        self.assertEqual(plan.dialogue_mode, "emergency_intervention")
        self.assertEqual(plan.emergency_action, "stop")

    def test_control_02_pause_robot(self):
        res = self.router.route("马上暂停当前机器人", [], {})
        plan = res.interaction_plan
        self.assertIsNotNone(plan)
        self.assertEqual(plan.operation, "CONTROL")
        self.assertEqual(plan.dialogue_mode, "emergency_intervention")
        self.assertEqual(plan.emergency_action, "pause")

    def test_control_03_abort_job(self):
        res = self.router.route("终止当前作业", [], {})
        plan = res.interaction_plan
        self.assertIsNotNone(plan)
        self.assertEqual(plan.operation, "CONTROL")
        self.assertEqual(plan.dialogue_mode, "emergency_intervention")
        self.assertEqual(plan.emergency_action, "abort")

    # ══════════════════════════════════════════════════════════════════════
    # 4. CLARIFY 矩阵验证
    # ══════════════════════════════════════════════════════════════════════

    def test_clarify_01_vague_look(self):
        res = self.router.route("帮我看看机器人", [], {})
        plan = res.interaction_plan
        self.assertIsNotNone(plan)
        self.assertEqual(plan.operation, "CLARIFY")
        self.assertEqual(plan.dialogue_mode, "knowledge_qa")
        self.assertTrue(plan.needs_clarification)

    def test_clarify_02_vague_process(self):
        res = self.router.route("处理一下设备", [], {})
        plan = res.interaction_plan
        self.assertIsNotNone(plan)
        self.assertEqual(plan.operation, "CLARIFY")
        self.assertTrue(plan.needs_clarification)

    def test_clarify_03_bare_stop_word(self):
        res = self.router.route("停止", [], {})
        plan = res.interaction_plan
        self.assertIsNotNone(plan)
        self.assertEqual(plan.operation, "CLARIFY")
        self.assertTrue(plan.needs_clarification)

    # ══════════════════════════════════════════════════════════════════════
    # 5. 状态副作用与不变性断言
    # ══════════════════════════════════════════════════════════════════════

    def test_state_invariance_for_read_and_clarify(self):
        read_queries = [
            "什么是软约束",
            "介绍金牛座",
            "当前任务还缺什么",
            "帮我看看机器人",  # CLARIFY
            "停止",            # CLARIFY
        ]
        for q in read_queries:
            v_before = self.dm.slot_store.version
            snap_before = self.dm.slot_store.export_snapshot()
            phase_before = self.dm.phase
            task_state_before = copy.deepcopy(self.dm.task_state)

            reply = self.dm.process(q)
            self.assertTrue(isinstance(reply, str) and len(reply) > 0)

            self.assertEqual(self.dm.slot_store.version, v_before)
            self.assertEqual(self.dm.slot_store.export_snapshot(), snap_before)
            self.assertEqual(self.dm.phase, phase_before)
            self.assertEqual(self.dm.task_state, task_state_before)

    # ══════════════════════════════════════════════════════════════════════
    # 6. ASR 入口与文本入口语义一致性
    # ══════════════════════════════════════════════════════════════════════

    def test_asr_consistency(self):
        text_input = "水深改成500米"
        asr_transcribed_input = "水深改成500米"

        res_text = self.router.route(text_input, [], {})
        res_asr = self.router.route(asr_transcribed_input, [], {})

        p_text = res_text.interaction_plan
        p_asr = res_asr.interaction_plan

        self.assertEqual(p_text.operation, p_asr.operation)
        self.assertEqual(p_text.dialogue_mode, p_asr.dialogue_mode)
        self.assertEqual(p_text.subject_type, p_asr.subject_type)
        self.assertEqual(p_text.relation, p_asr.relation)


if __name__ == "__main__":
    unittest.main()
