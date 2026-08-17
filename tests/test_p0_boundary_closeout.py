"""
tests/test_p0_boundary_closeout.py - P0 最后一轮边界收口测试套件
"""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.dialogue_manager import DialogueManager
from src.knowledge_retriever import KnowledgeBase
from src.slot_store import Slot, SlotVersionConflict
from tests.interaction_plan_support import (
    ScriptedLLM,
    extraction_result,
    make_plan,
    slot_candidate,
)
from tests.test_slot_consistency import seed_complete_valid_pipeline_task


class P0BoundaryCloseoutTest(unittest.TestCase):
    def setUp(self):
        self.kb = KnowledgeBase()
        self.llm = ScriptedLLM(default_reply="默认LLM测试回复")
        self.dm = DialogueManager(self.llm, self.kb)

    def _queue_write(self, *candidates: dict) -> None:
        self.llm.queue_plan(make_plan("WRITE"))
        self.llm.queue_extraction(extraction_result(*candidates))

    def _install_pending_oilfield(self) -> None:
        slots = self.dm.slot_store.clone_slots()
        slots["oilfield_name"] = Slot(
            "oilfield_name",
            value="陵水17-2气田",
            status="valid",
            value_type="string",
        )
        slots["pending_oilfield_name"] = Slot(
            "pending_oilfield_name",
            value="流花11-1",
            status="valid",
            value_type="string",
        )
        slots["pending_oilfield_candidates"] = Slot(
            "pending_oilfield_candidates",
            value=[
                {
                    "name": "流花11-1油田",
                    "id": "liuhua_11_1",
                    "confidence": 0.95,
                    "evidence": ["alias"],
                }
            ],
            status="valid",
            value_type="list",
        )
        self.dm.slot_store.commit_transaction(slots, [], request_id="test_setup_pending")
        self.dm._rebuild_cache(commit_derived=False)

    def _install_conflicts(
        self,
        *,
        support_vessel: bool = False,
        water_depth: bool = False,
        payload: bool = False,
    ) -> None:
        slots = self.dm.slot_store.clone_slots()
        if support_vessel:
            slot = slots["support_vessel"]
            slot.value = "海洋石油681"
            slot.status = "conflict"
            slot.candidate_value = "海洋石油286"
            slot.validation_error = "候选支持船需要确认"
        if water_depth:
            slot = slots["water_depth"]
            slot.value = 300.0
            slot.status = "conflict"
            slot.candidate_value = 800.0
            slot.validation_error = "候选水深需要确认"
        if payload:
            slot = slots["payload"]
            slot.status = "conflict"
            slot.candidate_value = ["腐蚀检测探头", "泄漏检测传感器"]
            slot.validation_error = "候选载荷需要确认"
        self.dm.slot_store.commit_transaction(slots, [], request_id="test_setup_conflicts")
        self.dm._rebuild_cache(commit_derived=False)

    # ── 问题一：done 修订包含 invalid 值的 intent_id 关联 ──

    def test_p1_done_revision_with_invalid_value_changes_intent_id(self):
        """done 状态下输入无效值：必须立即生成新的草稿 intent_id，后续有效修改沿用该草稿 ID"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir) / "task"
            tmp_path.mkdir(parents=True, exist_ok=True)

            seed_complete_valid_pipeline_task(self.dm, self.kb)
            all_v = self.dm.validator.validate(self.dm.task_state)
            for v in all_v:
                if v.severity == "soft":
                    for f in v.related_fields:
                        val = self.dm.task_state.get(f)
                        if val is not None:
                            self.dm._soft_whitelist.add((f, str(val), v.constraint_id))

            with patch("src.task_intent_builder.get_task_dir", return_value=tmp_path), \
                 patch("src.id_sequence.get_result_dir", return_value=Path(tmp_dir)):
                self.dm.process("确认发布")
                self.assertEqual(self.dm.phase, "done")
                orig_intent_id = self.dm.final_result["intent_id"]
                orig_file = tmp_path / f"task_intent_{orig_intent_id}.json"
                self.assertTrue(orig_file.exists())
                orig_file_data = json.loads(orig_file.read_text(encoding="utf-8"))

                # Turn 1: 已发布任务在未明确开启新任务前禁止就地篡改参数
                self._queue_write(
                    slot_candidate(
                        "water_depth",
                        "abc",
                        raw_key="水深",
                        raw_value="abc",
                    )
                )
                invalid_reply = self.dm.process("水深改为abc")

                self.assertEqual(self.dm.phase, "done")
                self.assertIsNotNone(self.dm.final_result)
                self.assertIn("已正式确认发布", invalid_reply)
                self.assertIn("无法就地修改参数", invalid_reply)
                self.assertEqual(self.dm.slot_store.slots["water_depth"].value, 300.0)
                self.assertEqual(
                    json.loads(orig_file.read_text(encoding="utf-8")),
                    orig_file_data,
                )

                # Turn 2: 再次输入有效参数尝试修改，同样被安全拦截，原始状态与文件完全不变
                self._queue_write(
                    slot_candidate(
                        "water_depth",
                        500.0,
                        raw_key="水深",
                        raw_value="500米",
                    )
                )
                second_reply = self.dm.process("水深改成500米")
                self.assertEqual(self.dm.phase, "done")
                self.assertIn("已正式确认发布", second_reply)
                self.assertIn("无法就地修改参数", second_reply)
                self.assertEqual(self.dm.slot_store.slots["water_depth"].value, 300.0)
                self.assertEqual(
                    json.loads(orig_file.read_text(encoding="utf-8")),
                    orig_file_data,
                )

    def test_p1_done_revision_transaction_failure_rollback(self):
        """done 状态下尝试就地修改被安全拦截，任务内存与磁盘状态完全保持不变"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir) / "task"
            tmp_path.mkdir(parents=True, exist_ok=True)

            seed_complete_valid_pipeline_task(self.dm, self.kb)
            all_v = self.dm.validator.validate(self.dm.task_state)
            for v in all_v:
                if v.severity == "soft":
                    for f in v.related_fields:
                        val = self.dm.task_state.get(f)
                        if val is not None:
                            self.dm._soft_whitelist.add((f, str(val), v.constraint_id))

            with patch("src.task_intent_builder.get_task_dir", return_value=tmp_path), \
                 patch("src.id_sequence.get_result_dir", return_value=Path(tmp_dir)):
                self.dm.process("确认发布")
                self.assertEqual(self.dm.phase, "done")
                orig_result = copy.deepcopy(self.dm.final_result)
                orig_slot_snapshot = copy.deepcopy(self.dm.slot_store.export_snapshot())
                orig_built = copy.deepcopy(self.dm._last_built_json)
                orig_missing = copy.deepcopy(self.dm._last_missing)

                self._queue_write(
                    slot_candidate(
                        "water_depth",
                        "abc",
                        raw_key="水深",
                        raw_value="abc",
                    )
                )
                reply = self.dm.process("水深改为abc")

                self.assertEqual(self.dm.phase, "done")
                self.assertIn("已正式确认发布", reply)
                self.assertIn("无法就地修改参数", reply)
                self.assertEqual(self.dm.final_result, orig_result)
                self.assertEqual(self.dm.slot_store.export_snapshot(), orig_slot_snapshot)
                self.assertEqual(self.dm._last_built_json, orig_built)
                self.assertEqual(self.dm._last_missing, orig_missing)

    # ── 问题二：pending oilfield 的结构化处理与优先级 ──

    def test_p2_pending_oilfield_does_not_intercept_negation_update(self):
        """存在 pending oilfield 时，输入'不要取消任务，水深改成500米'：水深更新为500，pending oilfield 保持不被误杀"""
        seed_complete_valid_pipeline_task(self.dm, self.kb)
        self._install_pending_oilfield()
        version_before = self.dm.slot_store.version
        pending_name_before = copy.deepcopy(
            self.dm.slot_store.slots["pending_oilfield_name"].to_dict()
        )
        pending_candidates_before = copy.deepcopy(
            self.dm.slot_store.slots["pending_oilfield_candidates"].to_dict()
        )
        oilfield_before = copy.deepcopy(
            self.dm.slot_store.slots["oilfield_name"].to_dict()
        )

        self._queue_write(
            slot_candidate(
                "water_depth",
                500.0,
                raw_key="水深",
                raw_value="500米",
            )
        )
        reply = self.dm.process("不要取消任务，水深改成500米")

        self.assertTrue(reply)
        self.assertEqual(self.dm.slot_store.version, version_before + 1)
        self.assertEqual(self.dm.slot_store.slots["water_depth"].value, 500.0)
        self.assertEqual(self.dm.slot_store.slots["water_depth"].status, "valid")
        self.assertEqual(
            self.dm.slot_store.slots["pending_oilfield_name"].to_dict(),
            pending_name_before,
        )
        self.assertEqual(
            self.dm.slot_store.slots["pending_oilfield_candidates"].to_dict(),
            pending_candidates_before,
        )
        self.assertEqual(
            self.dm.slot_store.slots["oilfield_name"].to_dict(),
            oilfield_before,
        )
        self.assertNotEqual(self.dm.phase, "rejected")
        self.assertEqual(len(self.llm.classify_calls), 1)
        self.assertEqual(len(self.llm.extract_calls), 1)

    def test_p2_pending_oilfield_task_cancel_priority(self):
        """存在 pending oilfield 时，输入'取消当前任务'：必须走 TASK_CANCEL，相较于局部油田处理有更高优先级"""
        seed_complete_valid_pipeline_task(self.dm, self.kb)
        self._install_pending_oilfield()
        self.assertEqual(
            self.dm.slot_store.get_task_state().get("task_type_key"),
            "pipeline_inspection",
        )
        self.llm.queue_plan(make_plan("CONTROL", emergency_action="cancel"))

        reply = self.dm.process("取消当前任务")

        self.assertIn("任务已取消", reply)
        self.assertEqual(self.dm.phase, "rejected")
        self.assertIsNone(self.dm.final_result)
        self.assertIsNone(
            self.dm.slot_store.get_task_state().get("task_type_key")
        )
        self.assertIsNone(self.dm.slot_store.get_task_state().get("water_depth"))
        pending_name = self.dm.slot_store.slots["pending_oilfield_name"]
        pending_candidates = self.dm.slot_store.slots["pending_oilfield_candidates"]
        self.assertIsNone(pending_name.value)
        self.assertEqual(pending_name.status, "missing")
        self.assertIsNone(pending_candidates.value)
        self.assertEqual(pending_candidates.status, "missing")
        self.assertEqual(self.dm.control_state, "idle")
        self.assertIsNone(self.dm.last_control_request)
        self.assertEqual(len(self.llm.classify_calls), 1)
        self.assertEqual(len(self.llm.extract_calls), 0)

    def test_p2_pending_oilfield_explicit_rejection(self):
        """输入'这个油田不对'：只清除 pending oilfield 恢复原值，其他槽位不变"""
        seed_complete_valid_pipeline_task(self.dm, self.kb)
        self._install_pending_oilfield()
        version_before = self.dm.slot_store.version
        oilfield_before = copy.deepcopy(
            self.dm.slot_store.slots["oilfield_name"].to_dict()
        )
        water_depth_before = copy.deepcopy(
            self.dm.slot_store.slots["water_depth"].to_dict()
        )

        self.llm.queue_plan(make_plan("WRITE", pending_action="reject"))
        reply = self.dm.process("这个油田不对")

        self.assertIn("已取消当前待确认油田名称", reply)
        self.assertEqual(self.dm.slot_store.version, version_before + 1)
        self.assertEqual(
            self.dm.slot_store.slots["oilfield_name"].to_dict(),
            oilfield_before,
        )
        self.assertEqual(
            self.dm.slot_store.slots["water_depth"].to_dict(),
            water_depth_before,
        )
        pending_name = self.dm.slot_store.slots["pending_oilfield_name"]
        pending_candidates = self.dm.slot_store.slots["pending_oilfield_candidates"]
        self.assertIsNone(pending_name.value)
        self.assertEqual(pending_name.status, "missing")
        self.assertIsNone(pending_candidates.value)
        self.assertEqual(pending_candidates.status, "missing")
        self.assertEqual(len(self.llm.classify_calls), 1)
        self.assertEqual(len(self.llm.extract_calls), 0)

    def test_p2_pending_oilfield_explicit_confirmation(self):
        """输入'确认使用流花11-1油田'：只确认目标油田，不自动发布任务"""
        seed_complete_valid_pipeline_task(self.dm, self.kb)
        self._install_pending_oilfield()
        version_before = self.dm.slot_store.version
        water_depth_before = copy.deepcopy(
            self.dm.slot_store.slots["water_depth"].to_dict()
        )

        self.llm.queue_plan(
            make_plan(
                "WRITE",
                pending_action="confirm",
                subject_text="流花11-1油田",
            )
        )
        reply = self.dm.process("确认使用流花11-1油田")

        self.assertIn("已确认油田名称", reply)
        self.assertEqual(self.dm.slot_store.version, version_before + 1)
        self.assertEqual(self.dm.slot_store.slots["oilfield_name"].status, "valid")
        self.assertEqual(self.dm.slot_store.slots["oilfield_name"].value, "流花11-1油田")
        self.assertEqual(
            self.dm.slot_store.slots["oilfield_entity_id"].value,
            "liuhua_11_1",
        )
        self.assertIsNone(self.dm.slot_store.slots["pending_oilfield_name"].value)
        self.assertEqual(
            self.dm.slot_store.slots["pending_oilfield_name"].status,
            "missing",
        )
        self.assertIsNone(
            self.dm.slot_store.slots["pending_oilfield_candidates"].value
        )
        self.assertEqual(
            self.dm.slot_store.slots["pending_oilfield_candidates"].status,
            "missing",
        )
        self.assertEqual(
            self.dm.slot_store.slots["water_depth"].to_dict(),
            water_depth_before,
        )
        self.assertNotEqual(self.dm.phase, "done")
        self.assertEqual(len(self.llm.classify_calls), 1)
        self.assertEqual(len(self.llm.extract_calls), 0)

    # ── 问题三：done 快照真实发布证据验证与无缝迁移 ──

    def test_p3_done_snapshot_missing_disk_file_downgrades_phase(self):
        """恢复 done 快照但关联的 task_intent 文件不存在：不得保持 done，降级为 confirming/collecting 并生成新草稿 ID"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_task_dir = Path(tmp_dir) / "task"
            tmp_task_dir.mkdir(parents=True, exist_ok=True)

            snap = {
                "phase": "done",
                "mode": "normal",
                "task_state": {"task_type_key": "pipeline_inspection", "water_depth": 300.0, "intent_id": "TI2026063001"},
                "built_json": {"task_type_key": "pipeline_inspection", "water_depth": 300.0, "intent_id": "TI2026063001"},
                "slot_store": {
                    "store_version": 1,
                    "slots": {
                        "task_type_key": {"slot_name": "task_type_key", "value": "pipeline_inspection", "status": "valid", "version": 1},
                        "water_depth": {"slot_name": "water_depth", "value": 300.0, "status": "valid", "version": 1},
                        "intent_id": {"slot_name": "intent_id", "value": "TI2026063001", "status": "valid", "version": 1}
                    },
                    "unresolved": []
                }
            }

            with patch("src.task_intent_builder.get_task_dir", return_value=tmp_task_dir), \
                 patch("src.id_sequence.get_result_dir", return_value=Path(tmp_dir)):
                self.dm.load_snapshot(snap)

            self.assertNotEqual(self.dm.phase, "done")
            self.assertIsNone(self.dm.final_result)
            self.assertNotEqual(self.dm.slot_store.slots["intent_id"].value, "TI2026063001")

    def test_p3_done_snapshot_valid_disk_file_restores_done(self):
        """恢复 done 快照且磁盘上存在内容匹配的发布 JSON 文件：成功恢复 done 阶段和 final_result"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_task_dir = Path(tmp_dir) / "task"
            tmp_task_dir.mkdir(parents=True, exist_ok=True)
            pub_file = tmp_task_dir / "task_intent_TI2026063001.json"
            valid_intent = {
                "schema_version": 2,
                "internal_id": "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11",
                "task_id": "PI-20260630-001",
                "intent_id": "TI2026063001",
                "task_type": "pipeline_inspection",
                "priority": 7,
                "time": {"start": "2026-06-30T10:00:00+08:00", "end": "2026-06-30T12:00:00+08:00"},
                "location": {"oilfield": "南海一号", "water_depth_m": 300.0},
                "task": {
                    "type": "pipeline_inspection",
                    "details": {
                        "pipeline_type": "subsea_oil_gas",
                        "start_point": {"latitude": 20.0, "longitude": 110.0},
                        "end_point": {"latitude": 20.1, "longitude": 110.1},
                    },
                },
                "equipment": {
                    "robot_type": "observation_rov",
                    "payload": ["camera_hd"],
                    "support_vessel": {"name": "海洋石油201"},
                },
                "conditions": {"max_current_speed_knots": 2.0, "sea_state_level": 3},
            }
            with open(pub_file, "w", encoding="utf-8") as f:
                json.dump(valid_intent, f)

            snap = {
                "phase": "done",
                "mode": "normal",
                "task_state": {"internal_id": "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11", "task_id": "PI-20260630-001", "task_type_key": "pipeline_inspection", "water_depth": 300.0, "intent_id": "TI2026063001"},
                "built_json": {"task_type_key": "pipeline_inspection", "water_depth": 300.0, "intent_id": "TI2026063001"},
                "slot_store": {
                    "store_version": 1,
                    "slots": {
                        "internal_id": {"slot_name": "internal_id", "value": "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11", "status": "valid", "version": 1},
                        "task_id": {"slot_name": "task_id", "value": "PI-20260630-001", "status": "valid", "version": 1},
                        "task_type_key": {"slot_name": "task_type_key", "value": "pipeline_inspection", "status": "valid", "version": 1},
                        "water_depth": {"slot_name": "water_depth", "value": 300.0, "status": "valid", "version": 1},
                        "intent_id": {"slot_name": "intent_id", "value": "TI2026063001", "status": "valid", "version": 1}
                    },
                    "unresolved": []
                }
            }

            with patch("src.task_intent_builder.get_task_dir", return_value=tmp_task_dir), \
                 patch("src.id_sequence.get_result_dir", return_value=Path(tmp_dir)):
                self.dm.load_snapshot(snap)

            self.assertEqual(self.dm.phase, "done")
            self.assertIsNotNone(self.dm.final_result)
            self.assertEqual(self.dm.slot_store.slots["intent_id"].value, "TI2026063001")

    # ── 问题五：多槽位冲突与差异化 Candidate 验证 ──

    def test_p5_support_vessel_different_candidate_value_confirmation(self):
        """support_vessel 原值 A, candidate_value 为与 A 不同的 B：显式确认后值变为 B"""
        seed_complete_valid_pipeline_task(self.dm, self.kb)
        self._install_conflicts(support_vessel=True)
        version_before = self.dm.slot_store.version
        water_depth_before = copy.deepcopy(
            self.dm.slot_store.slots["water_depth"].to_dict()
        )

        self._queue_write(
            slot_candidate(
                "support_vessel",
                "海洋石油286",
                raw_key="支持船",
                raw_value="海洋石油286",
            )
        )
        reply = self.dm.process("确认将支持船修改为海洋石油286")

        self.assertTrue(reply)
        self.assertEqual(self.dm.slot_store.version, version_before + 1)
        slot = self.dm.slot_store.slots["support_vessel"]
        self.assertEqual(slot.status, "valid")
        self.assertEqual(slot.value, "海洋石油286")
        self.assertIsNone(slot.candidate_value)
        self.assertIsNone(slot.validation_error)
        self.assertEqual(
            self.dm.slot_store.slots["water_depth"].to_dict(),
            water_depth_before,
        )
        self.assertEqual(len(self.llm.classify_calls), 1)
        self.assertEqual(len(self.llm.extract_calls), 1)

    def test_p5_multiple_conflicts_targeted_confirmation(self):
        """support_vessel 和 water_depth 同时处于 conflict：只确认 support_vessel 时，water_depth 保持 conflict"""
        seed_complete_valid_pipeline_task(self.dm, self.kb)
        self._install_conflicts(support_vessel=True, water_depth=True)
        version_before = self.dm.slot_store.version
        water_depth_before = copy.deepcopy(
            self.dm.slot_store.slots["water_depth"].to_dict()
        )

        self._queue_write(
            slot_candidate(
                "support_vessel",
                "海洋石油286",
                raw_key="支持船",
                raw_value="海洋石油286",
            )
        )
        self.dm.process("确认支持船为海洋石油286")

        self.assertEqual(self.dm.slot_store.version, version_before + 1)
        support_slot = self.dm.slot_store.slots["support_vessel"]
        self.assertEqual(support_slot.status, "valid")
        self.assertEqual(support_slot.value, "海洋石油286")
        self.assertIsNone(support_slot.candidate_value)
        self.assertEqual(
            self.dm.slot_store.slots["water_depth"].to_dict(),
            water_depth_before,
        )
        self.assertEqual(len(self.llm.classify_calls), 1)
        self.assertEqual(len(self.llm.extract_calls), 1)

    def test_p5_payload_conflict_targeted_cancellation(self):
        """payload 处于 conflict：输入'取消载荷修改'，保留原 payload 并清除候选值"""
        seed_complete_valid_pipeline_task(self.dm, self.kb)
        original_payload = copy.deepcopy(
            self.dm.slot_store.slots["payload"].value
        )
        self._install_conflicts(payload=True)
        version_before = self.dm.slot_store.version
        water_depth_before = copy.deepcopy(
            self.dm.slot_store.slots["water_depth"].to_dict()
        )

        # 当前生产实现仍以原句识别定向取消；模型替身只显式提供 WRITE
        # 与完整空 extraction，不按关键词分支。
        self._queue_write()
        reply = self.dm.process("取消载荷修改")

        self.assertIn("未写入任务状态", reply)
        self.assertEqual(self.dm.slot_store.version, version_before + 1)
        payload_slot = self.dm.slot_store.slots["payload"]
        self.assertEqual(payload_slot.status, "valid")
        self.assertEqual(payload_slot.value, original_payload)
        self.assertIsNone(payload_slot.candidate_value)
        self.assertIsNone(payload_slot.validation_error)
        self.assertEqual(
            self.dm.slot_store.slots["water_depth"].to_dict(),
            water_depth_before,
        )
        self.assertEqual(len(self.llm.classify_calls), 1)
        self.assertEqual(len(self.llm.extract_calls), 1)

    def test_p5_multiple_conflicts_ambiguous_confirmation_requires_clarification(self):
        """两个槽位同时 conflict 时，输入模糊的'确认这个修改'：不更新任何冲突槽位，要求澄清"""
        seed_complete_valid_pipeline_task(self.dm, self.kb)
        self._install_conflicts(support_vessel=True, water_depth=True)
        version_before = self.dm.slot_store.version
        support_before = copy.deepcopy(
            self.dm.slot_store.slots["support_vessel"].to_dict()
        )
        water_depth_before = copy.deepcopy(
            self.dm.slot_store.slots["water_depth"].to_dict()
        )

        self._queue_write()
        reply = self.dm.process("确认这个修改")

        self.assertIn("未写入任务状态", reply)
        self.assertEqual(self.dm.slot_store.version, version_before + 1)
        self.assertEqual(
            self.dm.slot_store.slots["support_vessel"].to_dict(),
            support_before,
        )
        self.assertEqual(
            self.dm.slot_store.slots["water_depth"].to_dict(),
            water_depth_before,
        )
        self.assertEqual(len(self.llm.classify_calls), 1)
        self.assertEqual(len(self.llm.extract_calls), 1)


if __name__ == "__main__":
    unittest.main()
