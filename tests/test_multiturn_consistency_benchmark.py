"""显式 InteractionPlan 驱动的多轮 WRITE 与 SSOT 一致性基准。"""

from __future__ import annotations

import copy
import unittest

from src.dialogue_manager import DialogueManager
from src.knowledge_retriever import KnowledgeBase
from tests.interaction_plan_support import (
    ScriptedLLM,
    extraction_result,
    make_plan,
    slot_candidate,
)


def _payload_addition(*items: str) -> dict:
    return {
        "field": "payload",
        "operation": "add",
        "items": list(items),
        "target_items": [],
        "raw_text": "携带高清水下摄像机和成像声呐",
        "confidence": 0.99,
        "source": "user_input",
    }


class MultiTurnConsistencyBenchmarkTest(unittest.TestCase):
    def setUp(self):
        self.llm = ScriptedLLM(
            plans=[make_plan("WRITE") for _ in range(4)],
            extractions=[
                extraction_result(
                    slot_candidate(
                        "task_type_key",
                        "pipeline_inspection",
                        raw_key="作业类型标识",
                        raw_value="管缆巡检",
                    ),
                    slot_candidate(
                        "task_type",
                        "管缆巡检",
                        raw_key="作业类型",
                        raw_value="管缆巡检",
                    ),
                ),
                extraction_result(
                    slot_candidate(
                        "oilfield_name",
                        "流花11-1油田",
                        raw_key="目标油田",
                        raw_value="流花11-1油田",
                    ),
                ),
                extraction_result(
                    slot_candidate(
                        "equipment_type",
                        "轻型工作级深海机器人 150HP",
                        raw_key="设备型号",
                        raw_value="天鹰座一号机",
                    ),
                    slot_candidate(
                        "equipment_unit_id",
                        "LROV-150-001",
                        raw_key="具体机器人",
                        raw_value="天鹰座一号机",
                    ),
                ),
                extraction_result(
                    list_mutations=[
                        _payload_addition("高清水下摄像机", "成像声呐")
                    ],
                ),
                extraction_result(
                    slot_candidate(
                        "water_depth",
                        300.0,
                        raw_key="作业水深",
                        raw_value="300米",
                    ),
                ),
            ],
            default_reply="参数已接收。",
        )
        self.dm = DialogueManager(self.llm, KnowledgeBase())

    def _process_write_and_assert_ssot(self, message: str) -> tuple[int, dict]:
        version_before = self.dm.slot_store.version
        snapshot_before = copy.deepcopy(self.dm.slot_store.export_snapshot())

        self.dm.process(message)

        version_after = self.dm.slot_store.version
        self.assertGreater(
            version_after,
            version_before,
            "每个 WRITE 轮必须真实提交并递增版本",
        )
        self.assertNotEqual(
            self.dm.slot_store.export_snapshot(),
            snapshot_before,
            "WRITE 轮不得以空更新假通过",
        )
        slot_state = self.dm.slot_store.get_task_state()
        self.assertTrue(slot_state, "SSOT 比较不得建立在两个空状态之上")
        self.assertEqual(self.dm.task_state, slot_state)
        return version_after, copy.deepcopy(slot_state)

    def test_four_turn_write_accumulation_and_ssot_consistency(self):
        _, state_1 = self._process_write_and_assert_ssot(
            "执行流花11-1油田管缆巡检"
        )
        self.assertEqual(state_1["task_type_key"], "pipeline_inspection")
        self.assertEqual(state_1["task_type"], "管缆巡检")
        self.assertEqual(state_1["raw_oilfield_name"], "流花11-1油田")

        _, state_2 = self._process_write_and_assert_ssot("使用天鹰座一号机")
        self.assertEqual(state_2["equipment_type"], "轻型工作级深海机器人 150HP")
        self.assertEqual(state_2["equipment_unit_id"], "LROV-150-001")
        self.assertEqual(state_2["raw_oilfield_name"], "流花11-1油田")

        _, state_3 = self._process_write_and_assert_ssot(
            "携带高清摄像机和成像声呐"
        )
        self.assertEqual(
            state_3["payload"],
            ["高清水下摄像机", "成像声呐"],
        )
        self.assertEqual(state_3["equipment_unit_id"], "LROV-150-001")
        self.assertEqual(state_3["raw_oilfield_name"], "流花11-1油田")

        _, final_state = self._process_write_and_assert_ssot("把水深改成300米")
        expected_fields = {
            "task_type_key": "pipeline_inspection",
            "task_type": "管缆巡检",
            "raw_oilfield_name": "流花11-1油田",
            "equipment_type": "轻型工作级深海机器人 150HP",
            "equipment_unit_id": "LROV-150-001",
            "payload": ["高清水下摄像机", "成像声呐"],
            "water_depth": 300.0,
        }
        for key, expected_value in expected_fields.items():
            self.assertIn(key, final_state)
            self.assertEqual(final_state[key], expected_value)

        self.assertGreaterEqual(len(final_state), len(expected_fields))
        self.assertEqual(final_state, self.dm.slot_store.get_task_state())
        self.assertEqual(self.dm.task_state, final_state)
        self.assertEqual(len(self.llm.classify_calls), 4)
        self.assertEqual(len(self.llm.extract_calls), 5)
        self.assertFalse(self.llm.plans)
        self.assertFalse(self.llm.extractions)


if __name__ == "__main__":
    unittest.main()
