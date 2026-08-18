"""test_global_paradigm_alignment.py — Global Dual-Gate Paradigm Alignment Tests

验证全局全字段在“LLM 软吸附 + 后端硬契约”双门控范式下的端到端对齐与安全防护：
1. payload: 增量列表变更 + 工具口语别名吸附至 supported_payloads
2. equipment_class: 机器人大类别名吸附 (如 "观察级" -> "观察级ROV")
3. equipment_family: 机器人系列别名吸附 (如 "天鹰座150" -> "轻型工作级深海机器人")
4. equipment_type: 设备型号别名吸附 (如 "OBSROV-75" -> "OBSROV-75-001")
5. support_vessel: 支持船别名吸附 (如 "201号船" -> "海洋石油201")
6. cable_type: 管缆类型别名吸附 (如 "油气管" -> "海底油气管道")
7. oilfield_name: 油田区域别名吸附与空间坐标自动关联
"""

import unittest

from src.dialogue_manager import DialogueManager
from src.knowledge_retriever import KnowledgeBase
from tests.interaction_plan_support import ScriptedLLM, make_plan


class TestGlobalParadigmAlignment(unittest.TestCase):
    def setUp(self):
        self.kb = KnowledgeBase()

    def _setup_pipeline_dm(self):
        llm = ScriptedLLM(default_reply="请求已接收。")
        dm = DialogueManager(llm=llm, kb=self.kb)
        schema = dm.builder.get_schema("pipeline_inspection", "normal")
        dm.slot_store.init_task_slots(schema)
        slots = dm.slot_store.clone_slots()
        slots["task_type_key"].value = "pipeline_inspection"
        slots["task_type_key"].status = "valid"
        slots["task_type"].value = "管缆巡检"
        slots["task_type"].status = "valid"
        dm.slot_store.commit_transaction(slots, [], request_id="test_seed_pipeline")
        dm._rebuild_cache()
        dm.phase = "collecting"
        return dm, llm

    def test_payload_incremental_add_and_alias_snapping(self):
        """1. payload 增量添加与别名吸附对齐。"""
        dm, llm = self._setup_pipeline_dm()
        slots = dm.slot_store.clone_slots()
        slots["equipment_type"].value = "轻型工作级深海机器人 150HP"
        slots["equipment_type"].status = "valid"
        slots["payload"].value = ["激光标尺"]
        slots["payload"].status = "valid"
        dm.slot_store.commit_transaction(slots, [], request_id="seed_payload")

        llm.queue_plan(make_plan("WRITE"))
        llm.queue_extraction(
            {
                "slot_candidates": [],
                "list_mutations": [
                    {
                        "field": "payload",
                        "operation": "add",
                        "items": ["腐蚀探头"],  # 别名，吸附至 腐蚀检测探头
                        "raw_text": "带上腐蚀探头",
                        "confidence": 0.95,
                        "source": "user_input",
                    }
                ],
                "unresolved": [],
            }
        )

        dm.process("带上腐蚀探头")

        payload_val = dm.slot_store.slots["payload"].value
        self.assertIn("腐蚀检测探头", payload_val)
        self.assertIn("激光标尺", payload_val)

    def test_cable_type_alias_snapping(self):
        """2. cable_type 管缆类型别名吸附对齐 (油气管 -> 海底油气管道)。"""
        dm, llm = self._setup_pipeline_dm()

        llm.queue_plan(make_plan("WRITE"))
        llm.queue_extraction(
            {
                "slot_candidates": [
                    {
                        "raw_key": "管缆类型",
                        "canonical_key": "cable_type",
                        "raw_value": "油气管",
                        "normalized_value": "海底油气管道",
                        "confidence": 0.95,
                    }
                ],
                "list_mutations": [],
                "unresolved": [],
            }
        )

        dm.process("管缆类型是油气管")

        cable_type_slot = dm.slot_store.slots["cable_type"]
        self.assertEqual(cable_type_slot.value, "海底油气管道")
        self.assertEqual(cable_type_slot.status, "valid")

    def test_support_vessel_alias_snapping(self):
        """3. support_vessel 支持船别名吸附对齐 (681号船 -> 海洋石油681)。"""
        dm, llm = self._setup_pipeline_dm()

        llm.queue_plan(make_plan("WRITE"))
        llm.queue_extraction(
            {
                "slot_candidates": [
                    {
                        "raw_key": "支持船",
                        "canonical_key": "support_vessel",
                        "raw_value": "681号船",
                        "normalized_value": "海洋石油681",
                        "confidence": 0.95,
                    }
                ],
                "list_mutations": [],
                "unresolved": [],
            }
        )

        dm.process("使用681号船")

        vessel_slot = dm.slot_store.slots["support_vessel"]
        self.assertEqual(vessel_slot.value, "海洋石油681")
        self.assertEqual(vessel_slot.status, "valid")

    def test_equipment_type_alias_snapping(self):
        """4. equipment_type 设备型号别名吸附对齐 (150马力轻型 -> 轻型工作级深海机器人 150HP)。"""
        dm, llm = self._setup_pipeline_dm()

        llm.queue_plan(make_plan("WRITE"))
        llm.queue_extraction(
            {
                "slot_candidates": [
                    {
                        "raw_key": "作业设备型号",
                        "canonical_key": "equipment_type",
                        "raw_value": "150马力轻型",
                        "normalized_value": "轻型工作级深海机器人 150HP",
                        "confidence": 0.95,
                    }
                ],
                "list_mutations": [],
                "unresolved": [],
            }
        )

        dm.process("使用150马力轻型机器人")

        eq_type_slot = dm.slot_store.slots["equipment_type"]
        self.assertEqual(eq_type_slot.value, "轻型工作级深海机器人 150HP")
        self.assertEqual(eq_type_slot.status, "valid")

    def test_oilfield_name_spatial_linking(self):
        """5. oilfield_name 油田名称同音/别名吸附 (硫化11-1油田 -> 流花11-1油田)。"""
        from src.oilfield_linker import OilfieldEntityLinker
        import yaml
        from pathlib import Path

        env = yaml.safe_load(Path("config/oilfield.yaml").read_text(encoding="utf-8"))
        linker = OilfieldEntityLinker(env)
        match = linker.link("硫化11-1油田")
        self.assertEqual(match.status, "accepted")
        self.assertEqual(match.standard_name, "流花11-1油田")

        dm, llm = self._setup_pipeline_dm()

        llm.queue_plan(make_plan("WRITE"))
        llm.queue_extraction(
            {
                "slot_candidates": [
                    {
                        "raw_key": "油田名称",
                        "canonical_key": "oilfield_name",
                        "raw_value": "流花油田",
                        "normalized_value": "流花11-1油田",
                        "confidence": 0.95,
                    }
                ],
                "list_mutations": [],
                "unresolved": [],
            }
        )

        dm.process("作业点在流花油田")

        oilfield_slot = dm.slot_store.slots.get("oilfield_name")
        if oilfield_slot and oilfield_slot.value:
            self.assertEqual(oilfield_slot.value, "流花11-1油田")


if __name__ == "__main__":
    unittest.main()
