"""故障隔离、硬约束恢复与发布事务回滚基准。

测试替身只返回预先排队的 InteractionPlan 和抽取结果，不读取用户原句。完整任务
前置状态由确定性测试 seed 构建；每次真实用户修改仍必须经过 WRITE Plan、Extractor、
SlotStore 提交与约束检查。
"""

from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.dialogue_manager import DialogueManager
from src.knowledge_retriever import KnowledgeBase
from src.task_intent_builder import TaskIntentBuilder, TaskPersistenceError
from tests.interaction_plan_support import (
    ScriptedLLM,
    extraction_result,
    make_plan,
    slot_candidate,
)
from tests.test_slot_consistency import seed_complete_valid_pipeline_task


class FailureRecoveryBenchmarkTest(unittest.TestCase):
    def setUp(self):
        self.kb = KnowledgeBase()
        self.llm = ScriptedLLM(default_reply="测试回复。")
        self.dm = DialogueManager(self.llm, self.kb)

    def _setup_full_confirming_task(self) -> None:
        seed_complete_valid_pipeline_task(self.dm, self.kb)
        self.assertEqual(self.dm.phase, "confirming")
        self.assertTrue(self.dm.task_state)
        self.assertEqual(self.dm.task_state, self.dm.slot_store.get_task_state())

    def _queue_write(self, *candidates: dict) -> None:
        self.llm.queue_plan(make_plan("WRITE"))
        self.llm.queue_extraction(extraction_result(*candidates))

    def test_llm_classification_exception_does_not_pollute_slot_store(self):
        self._setup_full_confirming_task()
        version_before = self.dm.slot_store.version
        snapshot_before = copy.deepcopy(self.dm.slot_store.export_snapshot())
        state_before = copy.deepcopy(self.dm.task_state)
        phase_before = self.dm.phase
        extraction_calls_before = len(self.llm.extract_calls)

        with patch.object(
            self.llm,
            "classify_interaction",
            side_effect=RuntimeError(
                "Simulated LLM network/classification timeout error"
            ),
        ):
            reply = self.dm.process("任意分类故障输入")

        self.assertTrue(reply)
        self.assertEqual(self.dm.slot_store.version, version_before)
        self.assertEqual(self.dm.slot_store.export_snapshot(), snapshot_before)
        self.assertEqual(self.dm.task_state, state_before)
        self.assertEqual(self.dm.phase, phase_before)
        self.assertEqual(len(self.llm.extract_calls), extraction_calls_before)
        self.assertEqual(self.dm.task_state, self.dm.slot_store.get_task_state())

    def test_hard_constraint_violation_and_recovery(self):
        self._setup_full_confirming_task()
        version_before_violation = self.dm.slot_store.version
        self._queue_write(
            slot_candidate(
                "water_depth",
                9999.0,
                raw_key="作业水深",
                raw_value="9999米",
            )
        )

        violation_reply = self.dm.process("作业水深为9999米")

        self.assertTrue(violation_reply)
        self.assertGreater(
            self.dm.slot_store.version,
            version_before_violation,
            "9999m 必须先真实提交，随后由硬约束阻断",
        )
        self.assertEqual(self.dm.phase, "blocked_hard")
        self.assertEqual(self.dm.task_state.get("water_depth"), 9999.0)
        self.assertTrue(self.dm._blocking_violations)
        self.assertTrue(
            any(v.severity == "hard" for v in self.dm._blocking_violations)
        )
        self.assertEqual(self.dm.task_state, self.dm.slot_store.get_task_state())

        version_before_recovery = self.dm.slot_store.version
        self._queue_write(
            slot_candidate(
                "water_depth",
                300.0,
                raw_key="作业水深",
                raw_value="300米",
            )
        )

        recovery_reply = self.dm.process("把水深改成300米")

        self.assertTrue(recovery_reply)
        self.assertGreater(
            self.dm.slot_store.version,
            version_before_recovery,
            "修复值必须通过新的 WRITE 事务提交",
        )
        self.assertNotEqual(self.dm.phase, "blocked_hard")
        self.assertIn(self.dm.phase, ("collecting", "confirming", "blocked_soft"))
        self.assertEqual(self.dm.task_state.get("water_depth"), 300.0)
        remaining = self.dm.validator.validate(self.dm.task_state)
        self.assertFalse(self.dm.validator.has_hard_violations(remaining))
        self.assertEqual(self.dm.task_state, self.dm.slot_store.get_task_state())
        self.assertEqual(len(self.llm.classify_calls), 2)
        self.assertEqual(len(self.llm.extract_calls), 2)
        self.assertFalse(self.llm.plans)
        self.assertFalse(self.llm.extractions)

    def test_publish_failure_reaches_publish_staging_and_rolls_back(self):
        self._setup_full_confirming_task()
        version_before = self.dm.slot_store.version
        snapshot_before = copy.deepcopy(self.dm.slot_store.export_snapshot())
        state_before = copy.deepcopy(self.dm.task_state)
        built_before = copy.deepcopy(self.dm._last_built_json)
        phase_before = self.dm.phase
        final_before = copy.deepcopy(self.dm.final_result)

        with tempfile.TemporaryDirectory() as tmp_dir:
            result_dir = Path(tmp_dir)
            task_dir = result_dir / "task"
            task_dir.mkdir(parents=True, exist_ok=True)
            persistence_error = TaskPersistenceError(
                "Simulated filesystem atomic publish lock failure"
            )

            with patch(
                "src.task_intent_builder.get_task_dir",
                return_value=task_dir,
            ), patch(
                "src.id_sequence.get_result_dir",
                return_value=result_dir,
            ), patch.object(
                TaskIntentBuilder,
                "publish_staging",
                autospec=True,
                side_effect=persistence_error,
            ) as mock_publish:
                with self.assertRaisesRegex(
                    TaskPersistenceError,
                    "Simulated filesystem atomic publish lock failure",
                ):
                    self.dm.process(
                        "确认发布",
                        request_id="publish_failure_rollback_test",
                    )

                mock_publish.assert_called_once()

        self.assertEqual(self.dm.phase, phase_before)
        self.assertEqual(self.dm.slot_store.version, version_before)
        self.assertEqual(self.dm.slot_store.export_snapshot(), snapshot_before)
        self.assertEqual(self.dm.task_state, state_before)
        self.assertEqual(self.dm._last_built_json, built_before)
        self.assertEqual(self.dm.final_result, final_before)
        self.assertEqual(self.dm.task_state, self.dm.slot_store.get_task_state())
        self.assertEqual(self.dm._last_built_json, self.dm.slot_store.get_built_json())
        self.assertFalse(self.llm.classify_calls)
        self.assertFalse(self.llm.extract_calls)


if __name__ == "__main__":
    unittest.main()
