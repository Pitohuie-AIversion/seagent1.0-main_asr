"""test_knowledge_grounded_enhancements.py — Knowledge-Grounded Intelligence Enhancement Tests

验证系统在约束违规提示中仅从 KnowledgeBase 中检索并推荐真实的替代设备，严禁编造非 KB 型号；
同时验证自然控制意图与硬阻断兜底规则安全协同。
"""

import unittest

from src.dialogue_manager import DialogueManager
from src.knowledge_retriever import KnowledgeBase
from src.prompts import build_responder_messages
from tests.interaction_plan_support import ScriptedLLM, make_plan


class TestKnowledgeGroundedEnhancements(unittest.TestCase):
    def setUp(self):
        self.kb = KnowledgeBase()

    def test_kb_alternatives_retrieval_returns_real_kb_robots(self):
        """测试：当触发水深或设备不匹配约束时，_get_kb_alternatives_for_violations 仅返回 KB 真实的合规替代型号。"""
        llm = ScriptedLLM()
        dm = DialogueManager(llm=llm, kb=self.kb)
        schema = dm.builder.get_schema("pipeline_inspection", "normal")
        dm.slot_store.init_task_slots(schema)
        slots = dm.slot_store.clone_slots()
        slots["task_type_key"].value = "pipeline_inspection"
        slots["task_type_key"].status = "valid"
        slots["task_type"].value = "管缆巡检"
        slots["task_type"].status = "valid"
        slots["water_depth"].value = 350.0  # 超出 Observation ROV 75HP (300m)
        slots["water_depth"].status = "valid"
        slots["equipment_type"].value = "观察级深海机器人 75HP"
        slots["equipment_type"].status = "valid"
        dm.slot_store.commit_transaction(slots, [], request_id="seed_kb_alt_test")
        dm._rebuild_cache()

        alts = dm._get_kb_alternatives_for_violations([])
        self.assertTrue(len(alts) > 0)
        alt_names = [alt["name"] for alt in alts]
        # 所有返回的替代型号必须能在 KB 中查到且最大水深 >= 350m
        for alt in alts:
            robot = dm.kb.get_rov(alt["name"])
            self.assertIsNotNone(robot)
            self.assertGreaterEqual(robot["max_depth_m"], 350.0)

    def test_kb_alternatives_formatted_in_prompt_without_hallucination(self):
        """测试：知识库合规替代设备被正确注入到 Prompt 中，并包含严格不编造指令。"""
        constraint_ctx = {
            "type": "hard",
            "violations": [],
            "kb_alternatives": [
                {
                    "name": "轻型工作级深海机器人 150HP",
                    "max_depth_m": 600,
                    "capabilities": ["inspection"],
                }
            ],
        }

        messages = build_responder_messages(
            task_state={"task_type": "管缆巡检"},
            built_json={},
            missing_fields=[],
            mode="normal",
            phase="blocked_hard",
            knowledge_context="",
            constraint_context=constraint_ctx,
            conversation_history=[],
            latest_user_message="水深350米",
            ROV2type={},
            support_task=[],
        )

        sys_content = messages[0]["content"]
        self.assertIn("知识库查找出的真实合规替代设备", sys_content)
        self.assertIn("轻型工作级深海机器人 150HP", sys_content)
        self.assertIn("严禁编造非知识库型号", sys_content)

    def test_natural_publish_confirmation_is_recognized(self):
        """测试：自然口语表达式能够正确被识别为确认/发布意图。"""
        dm = DialogueManager(llm=ScriptedLLM(), kb=self.kb)
        self.assertTrue(dm._is_final_publish_confirmation("确认发布"))
        self.assertTrue(dm._is_confirmation_only("没问题"))
        self.assertTrue(dm._is_ignore_warning("忽略警告"))
        self.assertTrue(dm._user_cancelled("取消任务"))


if __name__ == "__main__":
    unittest.main()
