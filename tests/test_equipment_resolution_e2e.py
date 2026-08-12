"""
tests/test_equipment_resolution_e2e.py — Equipment Resolution End-to-End Test

验证要求：
从自然语言设备口语别名（例如 "观察级一号机" 或 "天鹰座001"）出发，
经过系统级 Extractor -> alias resolution -> unit_id -> variant -> family -> validation，
验证 SlotStore 与 task_state 正确解析并填充各个字段，无任何手工 Slot 篡改行为。
"""

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.dialogue_manager import DialogueManager
from src.knowledge_retriever import KnowledgeBase
from tests.interaction_plan_support import (
    ScriptedLLM,
    extraction_result,
    make_plan,
    slot_candidate,
)


class EquipmentResolutionE2ETest(unittest.TestCase):
    def setUp(self):
        self.kb = KnowledgeBase()

    def _run_scripted_write(
        self,
        *,
        task_type_key,
        raw_task_type,
        equipment_alias,
        user_message,
    ):
        llm = ScriptedLLM(
            plans=[make_plan("WRITE")],
            extractions=[
                extraction_result(
                    slot_candidate(
                        "task_type_key",
                        task_type_key,
                        raw_key="任务类型",
                        raw_value=raw_task_type,
                    )
                ),
                extraction_result(
                    slot_candidate(
                        "equipment_name",
                        equipment_alias,
                        raw_key="设备名称",
                    ),
                    slot_candidate(
                        "water_depth",
                        300.0,
                        raw_key="作业水深",
                        raw_value="300米",
                    ),
                ),
            ],
            default_reply="设备解析成功。",
        )
        dm = DialogueManager(llm, self.kb)

        dm.process(user_message)

        self.assertEqual(len(llm.classify_calls), 1)
        self.assertEqual(len(llm.extract_calls), 2)
        self.assertFalse(llm.plans)
        self.assertFalse(llm.extractions)
        return dm

    def _assert_equipment_matches_ssot(
        self,
        dm,
        *,
        equipment_alias,
        task_type_key,
        expected_unit_id,
    ):
        expected_unit = self.kb.resolve_robot_unit(equipment_alias, task_type_key)
        self.assertIsNotNone(expected_unit)
        self.assertEqual(expected_unit["unit_id"], expected_unit_id)

        expected_slots = {
            "equipment_unit_id": expected_unit["unit_id"],
            "equipment_name": expected_unit["display_name"],
            "equipment_type": expected_unit["robot"]["full_name"],
            "equipment_family": expected_unit["robot"]["family_full_name"],
        }
        for slot_name, expected_value in expected_slots.items():
            with self.subTest(slot=slot_name):
                slot = dm.slot_store.slots.get(slot_name)
                self.assertIsNotNone(slot)
                self.assertEqual(slot.status, "valid")
                self.assertEqual(slot.value, expected_value)
                self.assertEqual(dm.task_state.get(slot_name), expected_value)

        self.assertEqual(dm.task_state, dm.slot_store.get_task_state())

    def test_alias_to_unit_variant_family_e2e_flow(self):
        """口语别名通过两阶段抽取写入 unit、variant 与 family。"""
        dm = self._run_scripted_write(
            task_type_key="tree_valve_operation",
            raw_task_type="采油树控制面板插入",
            equipment_alias="通用工作级一号机",
            user_message="执行采油树控制面板插入，配备通用工作级一号机，水深300米",
        )

        self._assert_equipment_matches_ssot(
            dm,
            equipment_alias="通用工作级一号机",
            task_type_key="tree_valve_operation",
            expected_unit_id="WROV-250-001",
        )
        self.assertEqual(dm.task_state.get("water_depth"), 300.0)

    def test_alias_jinniu_to_unit_variant_family_e2e_flow(self):
        """金牛座实体别名必须收敛到配置中的同一设备级联。"""
        dm = self._run_scripted_write(
            task_type_key="pipeline_burial",
            raw_task_type="管缆埋设",
            equipment_alias="金牛座001",
            user_message="执行管缆埋设作业，配备金牛座001，水深300米",
        )

        self._assert_equipment_matches_ssot(
            dm,
            equipment_alias="金牛座001",
            task_type_key="pipeline_burial",
            expected_unit_id="CRAWLER-1600-001",
        )
        self.assertEqual(dm.task_state.get("water_depth"), 300.0)


if __name__ == "__main__":
    unittest.main()
