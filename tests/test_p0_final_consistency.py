"""
tests/test_p0_final_consistency.py - P0 状态一致性与发布回滚安全收口测试
"""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.dialogue_manager import DialogueManager
from src.knowledge_retriever import KnowledgeBase
from src.slot_store import SlotVersionConflict
from tests.interaction_plan_support import (
    ScriptedLLM,
    extraction_result,
    make_plan,
    slot_candidate,
)
from tests.test_slot_consistency import seed_complete_valid_pipeline_task


class P0FinalConsistencyDefectTest(unittest.TestCase):
    def setUp(self):
        self.kb = KnowledgeBase()
        self.llm = ScriptedLLM()
        self.dm = DialogueManager(llm=self.llm, kb=self.kb)

    def _queue_write(self, *candidates):
        self.llm.queue_plan(make_plan("WRITE"))
        self.llm.queue_extraction(extraction_result(*candidates))

    # ── 问题一：否定取消测试 ──

    def test_p1_negation_cancel_does_not_reject_task(self):
        """否定取消并修改水深时，应执行 WRITE 且不取消草稿。"""
        seed_complete_valid_pipeline_task(self.dm, self.kb)
        version_before = self.dm.slot_store.version
        self._queue_write(
            slot_candidate(
                "water_depth",
                500.0,
                raw_key="水深",
                raw_value="500米",
                confidence=1.0,
            )
        )

        self.dm.process("不要取消任务，水深改成500米")

        self.assertEqual(len(self.llm.classify_calls), 1)
        self.assertEqual(len(self.llm.extract_calls), 1)
        self.assertGreater(self.dm.slot_store.version, version_before)
        self.assertNotEqual(self.dm.phase, "rejected")
        self.assertEqual(self.dm.slot_store.slots["water_depth"].value, 500.0)
        self.assertIsNone(self.dm.final_result)

    def test_p1_dont_cancel_prefix_update(self):
        """别取消并设置水深时，只提交水深 WRITE。"""
        seed_complete_valid_pipeline_task(self.dm, self.kb)
        version_before = self.dm.slot_store.version
        self._queue_write(
            slot_candidate(
                "water_depth",
                600.0,
                raw_key="水深",
                raw_value="600米",
                confidence=1.0,
            )
        )

        self.dm.process("别取消，把水深设置为600米")

        self.assertEqual(len(self.llm.classify_calls), 1)
        self.assertEqual(len(self.llm.extract_calls), 1)
        self.assertGreater(self.dm.slot_store.version, version_before)
        self.assertNotEqual(self.dm.phase, "rejected")
        self.assertEqual(self.dm.slot_store.slots["water_depth"].value, 600.0)

    def test_p1_cancel_task_pure_control(self):
        """CONTROL cancel 不调用 extractor，并清空未发布草稿。"""
        seed_complete_valid_pipeline_task(self.dm, self.kb)
        self.llm.queue_plan(make_plan("CONTROL", emergency_action="cancel"))

        self.dm.process("取消当前任务")

        self.assertEqual(len(self.llm.classify_calls), 1)
        self.assertEqual(self.llm.extract_calls, [])
        self.assertEqual(self.dm.phase, "rejected")
        self.assertEqual(self.dm.task_state, {})
        self.assertEqual(self.dm._last_built_json, {})
        self.assertEqual(self.dm._last_missing, [])
        self.assertIsNone(self.dm.final_result)
        self.assertEqual(self.dm.control_state, "idle")
        self.assertIsNone(self.dm.last_control_request)

    # ── 问题二：done 状态修改测试 ──

    def test_p2_done_state_modification_recreates_draft_and_new_intent_id(self):
        """发布后修改创建新草稿和 intent_id，且不覆盖原发布文件。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir) / "task"
            tmp_path.mkdir(parents=True, exist_ok=True)

            seed_complete_valid_pipeline_task(self.dm, self.kb)
            all_v = self.dm.validator.validate(self.dm.task_state)
            for violation in all_v:
                if violation.severity == "soft":
                    for field in violation.related_fields:
                        value = self.dm.task_state.get(field)
                        if value is not None:
                            self.dm._soft_whitelist.add(
                                (field, str(value), violation.constraint_id)
                            )

            with (
                patch("src.task_intent_builder.get_task_dir", return_value=tmp_path),
                patch("src.id_sequence.get_result_dir", return_value=Path(tmp_dir)),
            ):
                self.dm.process("确认发布")
                self.assertEqual(self.dm.phase, "done")
                self.assertEqual(self.llm.classify_calls, [])
                self.assertEqual(self.llm.extract_calls, [])
                orig_final_result = copy.deepcopy(self.dm.final_result)
                orig_intent_id = orig_final_result["intent_id"]

                final_files_1 = list(tmp_path.glob("task_intent_*.json"))
                self.assertEqual(len(final_files_1), 1)
                self._queue_write(
                    slot_candidate(
                        "water_depth",
                        500.0,
                        raw_key="水深",
                        raw_value="500米",
                        confidence=1.0,
                    )
                )

                reply = self.dm.process("水深改成500米")

                self.assertEqual(self.dm.phase, "done")
                self.assertIn("已正式确认发布", reply)
                self.assertIn("无法就地修改参数", reply)
                self.assertEqual(self.dm.final_result, orig_final_result)
                self.assertEqual(self.dm.slot_store.slots["water_depth"].value, 300.0)

                with open(final_files_1[0], "r", encoding="utf-8") as file_obj:
                    orig_file_data = json.load(file_obj)
                self.assertEqual(orig_file_data["intent_id"], orig_intent_id)
                self.assertEqual(orig_file_data["location"]["water_depth_m"], 300.0)

                final_files_2 = list(tmp_path.glob("task_intent_*.json"))
                self.assertEqual(len(final_files_2), 1)

    def test_done_repeat_confirmation_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            task_dir = Path(tmp_dir) / "task"
            task_dir.mkdir(parents=True, exist_ok=True)
            seed_complete_valid_pipeline_task(self.dm, self.kb)

            with (
                patch("src.task_intent_builder.get_task_dir", return_value=task_dir),
                patch("src.id_sequence.get_result_dir", return_value=Path(tmp_dir)),
            ):
                self.dm.process("确认发布")
                self.assertEqual(self.dm.phase, "done")
                self.assertEqual(self.llm.classify_calls, [])
                self.assertEqual(self.llm.extract_calls, [])
                final_before = copy.deepcopy(self.dm.final_result)
                snapshot_before = copy.deepcopy(self.dm.slot_store.export_snapshot())
                task_files_before = {
                    path.name: path.read_bytes()
                    for path in task_dir.glob("task_intent_*.json")
                }

                reply = self.dm.process("确认发布")

                self.assertEqual(self.llm.classify_calls, [])
                self.assertEqual(self.llm.extract_calls, [])
                self.assertIn("无需重复发布", reply)
                self.assertIn(final_before["intent_id"], reply)
                self.assertEqual(self.dm.phase, "done")
                self.assertEqual(self.dm.final_result, final_before)
                self.assertEqual(self.dm.slot_store.export_snapshot(), snapshot_before)
                self.assertEqual(
                    {
                        path.name: path.read_bytes()
                        for path in task_dir.glob("task_intent_*.json")
                    },
                    task_files_before,
                )

    def test_p2_done_modification_commit_failure_rollback(self):
        """done 状态就地修改被安全拦截，完整保留已发布状态与 SlotStore。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir) / "task"
            tmp_path.mkdir(parents=True, exist_ok=True)

            seed_complete_valid_pipeline_task(self.dm, self.kb)
            all_v = self.dm.validator.validate(self.dm.task_state)
            for violation in all_v:
                if violation.severity == "soft":
                    for field in violation.related_fields:
                        value = self.dm.task_state.get(field)
                        if value is not None:
                            self.dm._soft_whitelist.add(
                                (field, str(value), violation.constraint_id)
                            )

            with (
                patch("src.task_intent_builder.get_task_dir", return_value=tmp_path),
                patch("src.id_sequence.get_result_dir", return_value=Path(tmp_dir)),
            ):
                self.dm.process("确认发布")
                self.assertEqual(self.dm.phase, "done")
                self.assertEqual(self.llm.classify_calls, [])
                self.assertEqual(self.llm.extract_calls, [])
                orig_final_result = copy.deepcopy(self.dm.final_result)
                orig_snapshot = copy.deepcopy(self.dm.slot_store.export_snapshot())
                self._queue_write(
                    slot_candidate(
                        "water_depth",
                        500.0,
                        raw_key="水深",
                        raw_value="500米",
                        confidence=1.0,
                    )
                )

                reply = self.dm.process("水深改成500米")

                self.assertEqual(self.dm.phase, "done")
                self.assertIn("已正式确认发布", reply)
                self.assertIn("无法就地修改参数", reply)
                self.assertEqual(self.dm.final_result, orig_final_result)
                self.assertEqual(self.dm.slot_store.export_snapshot(), orig_snapshot)


    # ── 问题三：槽位冲突解决测试 ──

    def test_p3_negation_confirm_does_not_accept_unrelated_conflict(self):
        """不确认发布并修改水深时，只提交水深，不能顺带接受支持船冲突。"""
        seed_complete_valid_pipeline_task(self.dm, self.kb)
        self.dm.slot_store.slots["support_vessel"].status = "conflict"
        self.dm.slot_store.slots["support_vessel"].candidate_value = "海洋石油681"
        version_before = self.dm.slot_store.version
        self._queue_write(
            slot_candidate(
                "water_depth",
                500.0,
                raw_key="水深",
                raw_value="500米",
                confidence=1.0,
            )
        )

        self.dm.process("不确认发布，水深改成500米")

        self.assertEqual(len(self.llm.classify_calls), 1)
        self.assertEqual(len(self.llm.extract_calls), 1)
        self.assertGreater(self.dm.slot_store.version, version_before)
        self.assertEqual(self.dm.slot_store.slots["water_depth"].value, 500.0)
        self.assertEqual(self.dm.slot_store.slots["support_vessel"].status, "conflict")
        self.assertEqual(self.dm.slot_store.slots["support_vessel"].candidate_value, "海洋石油681")

    def test_p3_targeted_confirm_support_vessel_conflict(self):
        """显式确认支持船候选时，只解决 support_vessel 冲突。"""
        seed_complete_valid_pipeline_task(self.dm, self.kb)
        self.dm.slot_store.slots["support_vessel"].status = "conflict"
        self.dm.slot_store.slots["support_vessel"].candidate_value = "海洋石油681"
        version_before = self.dm.slot_store.version
        self._queue_write(
            slot_candidate(
                "support_vessel",
                "海洋石油681",
                raw_key="支持船",
                raw_value="海洋石油681",
                confidence=1.0,
            )
        )

        self.dm.process("确认将支持船修改为海洋石油681")

        self.assertEqual(len(self.llm.classify_calls), 1)
        self.assertEqual(len(self.llm.extract_calls), 1)
        self.assertGreater(self.dm.slot_store.version, version_before)
        self.assertEqual(self.dm.slot_store.slots["support_vessel"].status, "valid")
        self.assertEqual(self.dm.slot_store.slots["support_vessel"].value, "海洋石油681")
        self.assertIsNone(self.dm.slot_store.slots["support_vessel"].candidate_value)

    def test_p3_targeted_cancel_support_vessel_conflict(self):
        """取消支持船候选修改时，保留原值并清除候选。"""
        seed_complete_valid_pipeline_task(self.dm, self.kb)
        orig_vessel = self.dm.slot_store.slots["support_vessel"].value
        self.dm.slot_store.slots["support_vessel"].status = "conflict"
        self.dm.slot_store.slots["support_vessel"].candidate_value = "海洋石油681"
        version_before = self.dm.slot_store.version
        self._queue_write()

        self.dm.process("取消支持船修改")

        self.assertEqual(len(self.llm.classify_calls), 1)
        self.assertEqual(len(self.llm.extract_calls), 1)
        self.assertGreater(self.dm.slot_store.version, version_before)
        self.assertEqual(self.dm.slot_store.slots["support_vessel"].status, "valid")
        self.assertEqual(self.dm.slot_store.slots["support_vessel"].value, orig_vessel)
        self.assertIsNone(self.dm.slot_store.slots["support_vessel"].candidate_value)

    # ── 历史快照恢复测试 ──

    def test_legacy_snapshot_restore_confirming_and_done(self):
        """测试兼容快照恢复：confirming 自动补全 intent_id，done 快照若无 intent_id 则降级为 confirming"""
        self.dm.reset()
        # legacy confirming 缺失 intent_id
        legacy_confirming = {
            "phase": "confirming",
            "task_state": {"task_type_key": "pipeline_inspection", "water_depth": 300.0},
            "built_json": {"task_type_key": "pipeline_inspection", "water_depth": 300.0},
            "slot_store": {
                "slots": {
                    "task_type_key": {"slot_name": "task_type_key", "value": "pipeline_inspection", "status": "valid"},
                    "water_depth": {"slot_name": "water_depth", "value": 300.0, "status": "valid"}
                }
            }
        }
        self.dm.load_snapshot(legacy_confirming)
        self.assertEqual(self.dm.phase, "confirming")
        self.assertIn("intent_id", self.dm.slot_store.slots)
        self.assertEqual(self.dm.slot_store.slots["intent_id"].status, "valid")

        # legacy done 缺失 intent_id 且无文件关联 -> 降级为 confirming
        legacy_done_missing_id = {
            "phase": "done",
            "task_state": {"task_type_key": "pipeline_inspection", "water_depth": 300.0},
            "built_json": {"task_type_key": "pipeline_inspection", "water_depth": 300.0},
            "slot_store": {
                "slots": {
                    "task_type_key": {"slot_name": "task_type_key", "value": "pipeline_inspection", "status": "valid"},
                    "water_depth": {"slot_name": "water_depth", "value": 300.0, "status": "valid"}
                }
            }
        }
        self.dm.load_snapshot(legacy_done_missing_id)
        self.assertNotEqual(self.dm.phase, "done")
        self.assertIn(self.dm.phase, ["confirming", "collecting"])


if __name__ == "__main__":
    unittest.main()
