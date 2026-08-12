"""Phase 1.9 核心回归：READ 不变性、WRITE 提交和发布失败回滚。"""

import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.dialogue_manager import DialogueManager
from src.exceptions import TaskPersistenceError
from src.knowledge_retriever import KnowledgeBase
from tests.interaction_plan_support import (
    ScriptedLLM,
    extraction_result,
    make_plan,
    slot_candidate,
)
from tests.test_slot_consistency import seed_complete_valid_pipeline_task


class Phase19RegressionGuardTest(unittest.TestCase):
    def setUp(self):
        self.kb = KnowledgeBase()
        self.llm = ScriptedLLM(default_reply="测试回复")
        self.dm = DialogueManager(self.llm, self.kb)

    def test_read_preserves_complete_task_state(self):
        """READ 不得修改非空 SlotStore、派生缓存或任务阶段。"""
        seed_complete_valid_pipeline_task(self.dm, self.kb)
        self.llm.queue_plan(
            make_plan(
                "READ",
                query_intent="DEVICE_CAPABILITY",
                subject_type="device",
                subject_text="金牛座一号机",
                relation="capabilities",
                source_policy="project_kb",
            )
        )

        before_snapshot = copy.deepcopy(self.dm.slot_store.export_snapshot())
        before_task_state = copy.deepcopy(self.dm.task_state)
        before_built_json = copy.deepcopy(self.dm._last_built_json)
        before_missing = copy.deepcopy(self.dm._last_missing)
        before_phase = self.dm.phase
        before_final_result = copy.deepcopy(self.dm.final_result)

        reply = self.dm.process(
            "金牛座一号机最大水深是多少？",
            request_id="req_phase19_read",
        )

        self.assertTrue(reply)
        self.assertEqual(len(self.llm.classify_calls), 1)
        self.assertEqual(self.llm.extract_calls, [])
        self.assertEqual(self.dm.slot_store.export_snapshot(), before_snapshot)
        self.assertEqual(self.dm.task_state, before_task_state)
        self.assertEqual(self.dm._last_built_json, before_built_json)
        self.assertEqual(self.dm._last_missing, before_missing)
        self.assertEqual(self.dm.phase, before_phase)
        self.assertEqual(self.dm.final_result, before_final_result)

    def test_write_creates_task_and_commits_water_depth(self):
        """WRITE 必须经过两阶段抽取并真实提交任务类型和水深。"""
        self.llm.queue_plan(make_plan("WRITE"))
        self.llm.queue_extraction(
            extraction_result(
                slot_candidate(
                    "task_type",
                    "管缆巡检",
                    raw_value="管缆巡检",
                    raw_key="任务类型",
                ),
                slot_candidate(
                    "task_type_key",
                    "pipeline_inspection",
                    raw_value="管缆巡检",
                    raw_key="任务类型标识",
                ),
            )
        )
        self.llm.queue_extraction(
            extraction_result(
                slot_candidate(
                    "water_depth",
                    300.0,
                    raw_value="300米",
                    raw_key="作业水深",
                )
            )
        )
        initial_version = self.dm.slot_store.version

        self.dm.process(
            "执行管缆巡检，水深300米",
            request_id="req_phase19_write",
        )

        state = self.dm.slot_store.get_task_state()
        self.assertEqual(len(self.llm.classify_calls), 1)
        self.assertEqual(len(self.llm.extract_calls), 2)
        self.assertGreater(self.dm.slot_store.version, initial_version)
        self.assertEqual(state.get("task_type"), "管缆巡检")
        self.assertEqual(state.get("task_type_key"), "pipeline_inspection")
        self.assertEqual(state.get("water_depth"), 300.0)
        self.assertEqual(self.dm.task_state, state)

    def test_publish_failure_calls_publisher_and_restores_snapshot(self):
        """publish_staging 失败必须抛错并精确恢复发布前内存状态。"""
        seed_complete_valid_pipeline_task(self.dm, self.kb)
        self.assertEqual(self.dm.phase, "confirming")
        self.assertEqual(self.dm._last_missing, [])

        before_snapshot = copy.deepcopy(self.dm.slot_store.export_snapshot())
        before_task_state = copy.deepcopy(self.dm.task_state)
        before_built_json = copy.deepcopy(self.dm._last_built_json)
        before_missing = copy.deepcopy(self.dm._last_missing)

        with tempfile.TemporaryDirectory() as tmp_dir:
            task_dir = Path(tmp_dir) / "task"
            task_dir.mkdir(parents=True, exist_ok=True)

            with (
                patch(
                    "src.task_intent_builder.get_task_dir",
                    return_value=task_dir,
                ),
                patch(
                    "src.dialogue_manager.TaskIntentBuilder.publish_staging",
                    side_effect=TaskPersistenceError(
                        "Simulated atomic publish failure in Phase 1.9 guard"
                    ),
                ) as mock_publish,
            ):
                with self.assertRaises(TaskPersistenceError):
                    self.dm.process(
                        "确认发布",
                        request_id="req_phase19_publish_failure",
                    )

            mock_publish.assert_called_once()
            self.assertEqual(list(task_dir.glob("task_intent_*.json")), [])

        self.assertEqual(self.llm.classify_calls, [])
        self.assertEqual(self.dm.phase, "confirming")
        self.assertIsNone(self.dm.final_result)
        self.assertEqual(self.dm.slot_store.export_snapshot(), before_snapshot)
        self.assertEqual(self.dm.task_state, before_task_state)
        self.assertEqual(self.dm._last_built_json, before_built_json)
        self.assertEqual(self.dm._last_missing, before_missing)


if __name__ == "__main__":
    unittest.main()
