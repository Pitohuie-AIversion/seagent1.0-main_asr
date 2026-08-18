"""test_payload_mutation_restoration.py — Payload List Mutation Preservation Tests

验证在自然语言输入“添加/加装/带上/加上”某携带工具时，系统精准保留已有工具列表并进行增量添加（add），
严禁因提取为候选值而误触发整体覆盖（overwrite）。
"""

import copy
import unittest

from src.dialogue_manager import DialogueManager
from src.knowledge_retriever import KnowledgeBase
from tests.interaction_plan_support import ScriptedLLM, make_plan, extraction_result


class TestPayloadMutationRestoration(unittest.TestCase):
    def setUp(self):
        self.kb = KnowledgeBase()

    def _setup_dm_with_payload(self, initial_payload):
        llm = ScriptedLLM(default_reply="参数已接收。")
        dm = DialogueManager(llm=llm, kb=self.kb)
        schema = dm.builder.get_schema("pipeline_inspection", "normal")
        dm.slot_store.init_task_slots(schema)
        slots = dm.slot_store.clone_slots()
        slots["task_type_key"].value = "pipeline_inspection"
        slots["task_type_key"].status = "valid"
        slots["task_type"].value = "管缆巡检"
        slots["task_type"].status = "valid"
        slots["equipment_type"].value = "轻型工作级深海机器人 150HP"
        slots["equipment_type"].status = "valid"
        slots["payload"].value = list(initial_payload)
        slots["payload"].status = "valid"
        dm.slot_store.commit_transaction(slots, [], request_id="test_seed_payload")
        dm._rebuild_cache()
        dm.phase = "collecting"
        return dm, llm

    def test_candidate_payload_converted_to_add_mutation_when_adding(self):
        """测试：当 LLM 误将'加装激光标尺'提取为 slot_candidates 时，后级平滑转换为 add 增量修改。"""
        dm, llm = self._setup_dm_with_payload(["腐蚀检测探头"])

        llm.queue_plan(make_plan("WRITE"))
        llm.queue_extraction(
            {
                "slot_candidates": [
                    {
                        "raw_key": "携带工具",
                        "canonical_key": "payload",
                        "raw_value": "激光标尺",
                        "normalized_value": ["激光标尺"],
                        "confidence": 0.95,
                    }
                ],
                "list_mutations": [],
                "unresolved": [],
            }
        )

        dm.process("加装激光标尺")

        payload_val = dm.slot_store.slots["payload"].value
        self.assertIn("腐蚀检测探头", payload_val)
        self.assertIn("激光标尺", payload_val)
        self.assertEqual(len(payload_val), 2)

    def test_candidate_payload_converted_to_remove_mutation_when_deleting(self):
        """测试：当 LLM 误将'删除腐蚀检测探头'提取为 slot_candidates 时，后级平滑转换为 remove 增量修改。"""
        dm, llm = self._setup_dm_with_payload(["腐蚀检测探头", "激光标尺"])

        llm.queue_plan(make_plan("WRITE"))
        llm.queue_extraction(
            {
                "slot_candidates": [
                    {
                        "raw_key": "携带工具",
                        "canonical_key": "payload",
                        "raw_value": "腐蚀检测探头",
                        "normalized_value": ["腐蚀检测探头"],
                        "confidence": 0.95,
                    }
                ],
                "list_mutations": [],
                "unresolved": [],
            }
        )

        dm.process("删除腐蚀检测探头")

        payload_val = dm.slot_store.slots["payload"].value
        self.assertNotIn("腐蚀检测探头", payload_val)
        self.assertIn("激光标尺", payload_val)
        self.assertEqual(payload_val, ["激光标尺"])

    def test_direct_list_mutation_add_preserves_existing(self):
        """测试：标准的 list_mutations add 操作能够正确增量累加。"""
        dm, llm = self._setup_dm_with_payload(["腐蚀检测探头"])

        llm.queue_plan(make_plan("WRITE"))
        llm.queue_extraction(
            {
                "slot_candidates": [],
                "list_mutations": [
                    {
                        "field": "payload",
                        "operation": "add",
                        "items": ["激光标尺"],
                        "raw_text": "加装激光标尺",
                        "confidence": 0.95,
                        "source": "user_input",
                    }
                ],
                "unresolved": [],
            }
        )

        dm.process("加装激光标尺")

        payload_val = dm.slot_store.slots["payload"].value
        self.assertEqual(payload_val, ["腐蚀检测探头", "激光标尺"])

    def test_semantic_adsorption_snapping_to_supported_payloads(self):
        """测试：LLM 或自然语言口语别名在后级通过 SlotStore 动态对齐吸附到 supported_payloads。"""
        dm, llm = self._setup_dm_with_payload(["激光标尺"])

        llm.queue_plan(make_plan("WRITE"))
        llm.queue_extraction(
            {
                "slot_candidates": [],
                "list_mutations": [
                    {
                        "field": "payload",
                        "operation": "add",
                        "items": ["腐蚀探头"],  # 口语别名/简称，会被 slot_store 确定性吸附为 "腐蚀检测探头"
                        "raw_text": "带上个腐蚀探头",
                        "confidence": 0.95,
                        "source": "user_input",
                    }
                ],
                "unresolved": [],
            }
        )

        dm.process("带上个腐蚀探头")

        payload_val = dm.slot_store.slots["payload"].value
        self.assertIn("腐蚀检测探头", payload_val)
        self.assertIn("激光标尺", payload_val)


if __name__ == "__main__":
    unittest.main()
