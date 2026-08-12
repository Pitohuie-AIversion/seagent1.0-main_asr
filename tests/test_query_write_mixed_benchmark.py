"""显式 InteractionPlan 驱动的 Query/Write 交错一致性基准。"""

from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from src.dialogue_manager import DialogueManager
from src.knowledge_retriever import KnowledgeBase
from tests.interaction_plan_support import (
    ScriptedLLM,
    extraction_result,
    make_plan,
    slot_candidate,
)


def _task_creation_extractions():
    return (
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
    )


class QueryWriteMixedBenchmarkTest(unittest.TestCase):
    def setUp(self):
        self.kb = KnowledgeBase()
        self._state_temp_dir = tempfile.TemporaryDirectory()
        self.kb.state_info.state_file = (
            Path(self._state_temp_dir.name) / "state.yaml"
        )

        create_stage1, create_stage2 = _task_creation_extractions()
        self.llm = ScriptedLLM(
            plans=[
                make_plan("WRITE"),
                make_plan(
                    "READ",
                    query_intent="DEVICE_CAPABILITY",
                    subject_type="device",
                    relation="capabilities",
                    source_policy="project_kb",
                ),
                make_plan("WRITE"),
                make_plan(
                    "READ",
                    query_intent="TASK_STATUS",
                    subject_type="task",
                    relation="missing_fields",
                    source_policy="session_state",
                ),
                make_plan("WRITE"),
                make_plan(
                    "READ",
                    query_intent="DEVICE_STATUS",
                    subject_type="realtime_state",
                    relation="status",
                    source_policy="realtime_state",
                ),
            ],
            extractions=[
                create_stage1,
                create_stage2,
                extraction_result(
                    slot_candidate(
                        "equipment_type",
                        "水下无人自主航行器 324CC",
                        raw_key="使用设备",
                        raw_value="水下无人自主航行器一号机",
                    ),
                    slot_candidate(
                        "equipment_unit_id",
                        "AUV-324cc-001",
                        raw_key="具体机器人",
                        raw_value="水下无人自主航行器一号机",
                    ),
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
            default_reply="设备当前深度为 350m。",
        )
        self.dm = DialogueManager(self.llm, self.kb)

    def tearDown(self):
        self._state_temp_dir.cleanup()

    def _assert_read_turn_is_invariant(self, message: str) -> str:
        version_before = self.dm.slot_store.version
        snapshot_before = copy.deepcopy(self.dm.slot_store.export_snapshot())
        task_state_before = copy.deepcopy(self.dm.task_state)
        extraction_calls_before = len(self.llm.extract_calls)

        reply = self.dm.process(message)

        self.assertEqual(
            self.dm.slot_store.version,
            version_before,
            "READ 轮不得改变 SlotStore.version",
        )
        self.assertEqual(
            self.dm.slot_store.export_snapshot(),
            snapshot_before,
            "READ 轮不得改变 SlotStore 完整快照",
        )
        self.assertEqual(
            self.dm.task_state,
            task_state_before,
            "READ 轮不得改变 task_state",
        )
        self.assertEqual(
            len(self.llm.extract_calls),
            extraction_calls_before,
            "READ 轮不得调用字段抽取模型",
        )
        self.assertEqual(self.dm.task_state, self.dm.slot_store.get_task_state())
        return reply

    def _process_write_and_assert_commit(self, message: str) -> dict:
        version_before = self.dm.slot_store.version
        snapshot_before = copy.deepcopy(self.dm.slot_store.export_snapshot())

        self.dm.process(message)

        self.assertGreater(
            self.dm.slot_store.version,
            version_before,
            "WRITE 轮必须真实提交并递增 SlotStore.version",
        )
        self.assertNotEqual(
            self.dm.slot_store.export_snapshot(),
            snapshot_before,
            "WRITE 轮不得以空更新假通过",
        )
        slot_state = self.dm.slot_store.get_task_state()
        self.assertTrue(slot_state, "WRITE 后 SlotStore 必须包含非空有效状态")
        self.assertEqual(self.dm.task_state, slot_state)
        return copy.deepcopy(slot_state)

    def test_interleaved_query_write_invariance(self):
        state_1 = self._process_write_and_assert_commit(
            "执行流花11-1油田管缆巡检"
        )
        self.assertEqual(state_1["task_type_key"], "pipeline_inspection")
        self.assertEqual(state_1["raw_oilfield_name"], "流花11-1油田")

        self._assert_read_turn_is_invariant(
            "水下无人自主航行器一号机最大水深是多少？"
        )

        state_2 = self._process_write_and_assert_commit(
            "使用水下无人自主航行器一号机"
        )
        self.assertEqual(state_2["equipment_type"], "水下无人自主航行器 324CC")
        self.assertEqual(state_2["equipment_unit_id"], "AUV-324cc-001")
        self.assertEqual(state_2["raw_oilfield_name"], "流花11-1油田")

        self._assert_read_turn_is_invariant("当前任务缺少什么？")

        state_3 = self._process_write_and_assert_commit("把水深改成300米")
        self.assertEqual(state_3["water_depth"], 300.0)
        self.assertEqual(state_3["equipment_unit_id"], "AUV-324cc-001")
        self.assertEqual(state_3["raw_oilfield_name"], "流花11-1油田")

        self.kb.state_info.set_status("AUV-324cc-001", {"depth": 350})
        reply = self._assert_read_turn_is_invariant("当前设备实时深度？")
        self.assertIn("350", reply)

        final_slot_state = self.dm.slot_store.get_task_state()
        self.assertTrue(final_slot_state)
        self.assertEqual(self.dm.task_state, final_slot_state)
        self.assertEqual(len(self.llm.classify_calls), 6)
        self.assertEqual(len(self.llm.extract_calls), 4)
        self.assertFalse(self.llm.plans)
        self.assertFalse(self.llm.extractions)


if __name__ == "__main__":
    unittest.main()
