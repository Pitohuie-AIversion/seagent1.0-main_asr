"""IntentRouter 与 DialogueManager 的显式 InteractionPlan 契约测试。

自然语言语义正确性由真实模型评测负责。这里的 ScriptedLLM 只返回预先给定的
计划，单元测试验证 Router 不会根据原句或任务上下文二次改写模型计划，并验证
READ/CLARIFY 不会产生任务状态副作用。
"""

from __future__ import annotations

import copy
import unittest
from unittest.mock import patch

from src.dialogue_manager import DialogueManager
from src.intent_router import IntentRouter
from src.knowledge_retriever import KnowledgeBase
from src.slot_store import Slot
from tests.interaction_plan_support import ScriptedLLM, make_plan


class TestInteractionPlanRoutingContract(unittest.TestCase):
    def test_read_plan_is_not_overridden_by_active_task_or_expected_slots(self):
        explicit_plan = make_plan(
            "READ",
            query_intent="TASK_STATUS",
            subject_type="task",
            relation="missing_fields",
            source_policy="session_state",
        )
        llm = ScriptedLLM(plans=[explicit_plan])
        router = IntentRouter(llm)

        result = router.route(
            "这是一条不携带固定路由话术的输入",
            [{"role": "assistant", "content": "上一轮正在收集任务"}],
            {"task_type_key": "pipeline_inspection", "water_depth": 500.0},
            expected_slots=["equipment_unit_id"],
        )

        self.assertEqual(result.interaction_plan.operation, "READ")
        self.assertEqual(result.dialogue_mode, "knowledge_qa")
        self.assertEqual(result.query_intent, "TASK_STATUS")
        self.assertFalse(result.should_update_slots)
        self.assertEqual(len(llm.classify_calls), 1)

    def test_write_plan_is_not_vetoed_when_input_has_no_write_keywords(self):
        llm = ScriptedLLM(plans=[make_plan("WRITE")])
        router = IntentRouter(llm)

        result = router.route(
            "那就照你刚才说的做",
            [{"role": "assistant", "content": "建议选用第二台机器人"}],
            {"task_type_key": "pipeline_inspection"},
        )

        self.assertEqual(result.interaction_plan.operation, "WRITE")
        self.assertEqual(result.dialogue_mode, "task_collection")
        self.assertEqual(result.interaction_type, "WRITE")
        self.assertTrue(result.should_update_slots)
        self.assertEqual(len(llm.classify_calls), 1)

    def test_control_plan_preserves_valid_emergency_action(self):
        llm = ScriptedLLM(
            plans=[make_plan("CONTROL", emergency_action="stop")]
        )
        router = IntentRouter(llm)

        result = router.route("任意控制表达", [], {"task_type_key": "pipeline_inspection"})

        self.assertEqual(result.interaction_plan.operation, "CONTROL")
        self.assertEqual(result.dialogue_mode, "emergency_intervention")
        self.assertEqual(result.emergency_action, "stop")
        # 兼容旧 DialogueManager 接口：CONTROL 通过 QUERY 外壳进入专用控制路径。
        self.assertEqual(result.interaction_type, "QUERY")
        self.assertFalse(result.should_update_slots)

    def test_clarify_plan_is_no_side_effect_query(self):
        llm = ScriptedLLM(plans=[make_plan("CLARIFY")])
        router = IntentRouter(llm)

        result = router.route("任意含糊表达", [], {})

        self.assertEqual(result.interaction_plan.operation, "CLARIFY")
        self.assertTrue(result.interaction_plan.needs_clarification)
        self.assertEqual(result.dialogue_mode, "knowledge_qa")
        self.assertEqual(result.query_intent, "CLARIFICATION")
        self.assertFalse(result.should_update_slots)

    def test_redundant_dialogue_mode_is_derived_from_operation(self):
        contradictory_plan = make_plan("WRITE")
        contradictory_plan["dialogue_mode"] = "knowledge_qa"
        llm = ScriptedLLM(plans=[contradictory_plan])
        router = IntentRouter(llm)

        result = router.route("任意输入", [], {})

        self.assertEqual(result.interaction_plan.operation, "WRITE")
        self.assertEqual(result.dialogue_mode, "task_collection")
        self.assertTrue(result.should_update_slots)

    def test_bare_expected_slot_alias_corrects_clarify_to_write(self):
        mistaken_plan = make_plan(
            "CLARIFY",
            subject_type="device_class",
            subject_text="观察级ROV",
            clarification_reason=(
                "用户输入'观察级ROV'属于设备类别，但当前任务待填字段为"
                "equipment_family 和 equipment_type。"
            ),
        )
        llm = ScriptedLLM(plans=[mistaken_plan])
        router = IntentRouter(llm)

        result = router.route(
            "观察级ROV",
            [],
            {"task_type_key": "pipeline_inspection"},
            phase="collecting",
            expected_slots=["equipment_family", "equipment_type"],
            expected_slot_options=[
                {
                    "key": "equipment_family",
                    "allowed_values": ["观察级深海机器人"],
                    "alias_mappings": {"观察级ROV": "观察级深海机器人"},
                },
                {
                    "key": "equipment_type",
                    "allowed_values": ["观察级深海机器人 75HP"],
                    "alias_mappings": {},
                },
            ],
        )

        self.assertEqual(result.interaction_plan.operation, "WRITE")
        self.assertEqual(result.dialogue_mode, "task_collection")
        self.assertTrue(result.should_update_slots)

    def test_expected_slot_alias_question_is_not_corrected_to_write(self):
        read_plan = make_plan(
            "READ",
            query_intent="DEVICE_CAPABILITY",
            subject_type="device_class",
            subject_text="观察级ROV",
            relation="compare",
            source_policy="project_kb",
        )
        llm = ScriptedLLM(plans=[read_plan])
        router = IntentRouter(llm)

        result = router.route(
            "观察级ROV和AUV哪个更合适？",
            [],
            {"task_type_key": "pipeline_inspection"},
            phase="collecting",
            expected_slots=["equipment_family", "equipment_type"],
            expected_slot_options=[
                {
                    "key": "equipment_family",
                    "allowed_values": ["观察级深海机器人", "水下无人自主航行器"],
                    "alias_mappings": {
                        "观察级ROV": "观察级深海机器人",
                        "AUV": "水下无人自主航行器",
                    },
                },
            ],
        )

        self.assertEqual(result.interaction_plan.operation, "READ")
        self.assertEqual(result.dialogue_mode, "knowledge_qa")
        self.assertFalse(result.should_update_slots)


class TestReadAndClarifyStateInvariance(unittest.TestCase):
    def setUp(self):
        self.llm = ScriptedLLM(
            plans=[
                make_plan("READ", query_intent="GENERAL_CHAT"),
                make_plan("CLARIFY"),
            ],
            default_reply="测试只读回复",
        )
        self.dm = DialogueManager(self.llm, KnowledgeBase())
        self.dm.slot_store.slots["task_type_key"] = Slot(
            "task_type_key",
            value="pipeline_inspection",
            status="valid",
        )
        self.dm.slot_store.slots["task_type"] = Slot(
            "task_type",
            value="管缆巡检",
            status="valid",
        )
        self.dm.slot_store.slots["water_depth"] = Slot(
            "water_depth",
            value=500.0,
            status="valid",
        )
        self.dm._rebuild_cache()

    def test_read_and_clarify_do_not_extract_or_commit(self):
        version_before = self.dm.slot_store.version
        snapshot_before = copy.deepcopy(self.dm.slot_store.export_snapshot())
        task_state_before = copy.deepcopy(self.dm.task_state)
        phase_before = self.dm.phase

        with patch.object(
            self.dm.extractor,
            "extract_updates",
        ) as mock_extract, patch.object(
            self.dm.slot_store,
            "commit_transaction",
        ) as mock_commit:
            read_reply = self.dm.process("第一条由模型判定为读取的输入")
            clarify_reply = self.dm.process("第二条由模型判定为澄清的输入")

        self.assertTrue(read_reply)
        self.assertTrue(clarify_reply)
        mock_extract.assert_not_called()
        mock_commit.assert_not_called()
        self.assertEqual(self.dm.slot_store.version, version_before)
        self.assertEqual(self.dm.slot_store.export_snapshot()["slots"], snapshot_before["slots"])
        self.assertEqual(self.dm.task_state, task_state_before)
        self.assertEqual(self.dm.phase, phase_before)
        self.assertEqual(len(self.llm.classify_calls), 2)


if __name__ == "__main__":
    unittest.main()
