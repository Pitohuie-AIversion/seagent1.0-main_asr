"""
test_validation_publish_safety_closeout.py
SEAgent G5 — Validation & Publish Safety Final Closeout Unit Tests

验证 8 个核心业务结果：
1. 合法任务：valid，可以发布；
2. 非法 water_depth (NaN/Inf/bool/非正数/字符串)：validation_error，不发布；
3. max_depth_m 缺失/非法：validation_error，不发布；
4. malformed start_time：validation_error，不跳过动态检查；
5. current_velocity / turbidity：现有 soft/hard C013-C017 正确；
6. soft ack：当前状态下可继续；state/version 变化后失效；
7. hard violation：无论用户怎么确认都不能发布；
8. Validator/state lookup 抛异常：final 不生成，fail closed。
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from src.dialogue_manager import DialogueManager
from src.knowledge_retriever import KnowledgeBase
from src.session_state import SessionState
from src.slot_store import ValidationAcknowledgement
from src.simulated_time import get_current_datetime
from src.task_intent_builder import TaskIntentBuilder
from src.validator import TaskValidator, ValidationResult, Violation


class TestValidationPublishSafetyCloseout(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.artifacts_dir = Path(self.temp_dir.name)
        self.kb = KnowledgeBase()
        self.validator = TaskValidator(self.kb)
        self.builder = TaskIntentBuilder(kb=self.kb)

    def tearDown(self):
        self.temp_dir.cleanup()

    # 1. 合法任务：valid，可以发布
    def test_01_legitimate_task_valid_and_publishable(self):
        now_str = get_current_datetime().isoformat()
        mock_snap = {
            "unit_id": "WROV-250-001",
            "status_ref": "WROV-250-001",
            "state_version": 1,
            "state": {
                "overall_status": "available",
                "update_timestamp": now_str,
                "confidence": 0.95,
                "survival_status": "normal",
                "thruster_status": "normal",
                "depth_keeping_status": "normal",
                "sonar_status": "normal",
                "vision_status": "normal",
                "arm_status": "normal",
                "end_effector_status": "normal",
                "tether_connection_status": "normal",
            },
        }

        mock_kb = MagicMock(spec=KnowledgeBase)
        mock_kb.get_constraints.return_value = self.kb.get_constraints()
        mock_kb.get_unit_state_snapshot.return_value = mock_snap
        mock_kb.resolve_robot_unit.return_value = {
            "robot": {"full_name": "ROV-001", "max_depth_m": 3000, "robot_class": "rov"},
            "unit_id": "WROV-250-001",
        }
        mock_kb.robot_matches_task.return_value = True
        mock_kb.validate_robot_selection_from_task_state.return_value = {
            "unit_id": "WROV-250-001",
        }

        v_handler = TaskValidator(mock_kb)
        task_state = {
            "task_type_key": "pipeline_inspection",
            "equipment_class": "rov",
            "equipment_family": "work_class_rov",
            "equipment_type": "heavy_work_class_rov",
            "equipment_unit_id": "WROV-250-001",
            "water_depth": 300.0,
            "start_time": now_str,
        }

        val_res = v_handler.validate_task(task_state, purpose="publish")
        self.assertEqual(val_res.overall_status, "valid")
        self.assertIsNone(val_res.error)
        self.assertEqual(len(val_res.violations), 0)

    # 2. 非法 water_depth (NaN/Inf/bool/非正数/字符串)：validation_error，不发布
    def test_02_illegal_water_depth_fail_closed(self):
        base_state = {
            "task_type_key": "pipeline_inspection",
            "equipment_unit_id": "WROV-250-001",
            "equipment_type": "heavy_work_class_rov",
            "start_time": "2026-08-10T12:00:00+08:00",
        }

        invalid_depths = [
            True,
            False,
            "invalid_string",
            float("nan"),
            float("inf"),
            -10.0,
            0,
        ]

        for bad_depth in invalid_depths:
            state = dict(base_state)
            state["water_depth"] = bad_depth
            val_res = self.validator.validate_task(state, purpose="publish")
            self.assertEqual(
                val_res.overall_status,
                "validation_error",
                f"Expected validation_error for water_depth={bad_depth}, got {val_res.overall_status}",
            )
            self.assertIsNotNone(val_res.error)
            self.assertEqual(val_res.error.get("code"), "INVALID_WATER_DEPTH")

    # 3. max_depth_m 缺失/非法：validation_error，不发布
    def test_03_max_depth_m_missing_or_invalid_fail_closed(self):
        task_state = {
            "task_type_key": "pipeline_inspection",
            "equipment_unit_id": "WROV-250-001",
            "equipment_type": "heavy_work_class_rov",
            "water_depth": 500.0,
        }

        # Mock ROV spec with missing max_depth_m
        bad_rov = {"full_name": "Test ROV", "robot_class": "rov"}  # no max_depth_m
        mock_kb = MagicMock(spec=KnowledgeBase)
        mock_kb.get_constraints.return_value = self.kb.get_constraints()
        mock_kb.resolve_robot_unit.return_value = {"robot": bad_rov, "unit_id": "WROV-250-001"}
        mock_kb.get_unit_state_snapshot.return_value = {
            "unit_id": "WROV-250-001",
            "status_ref": "REF-001",
            "state_version": 1,
            "state": {"overall_status": "available"},
        }
        mock_kb.robot_matches_task.return_value = True
        mock_kb.validate_robot_selection_from_task_state.return_value = {
            "unit_id": "WROV-250-001",
        }

        custom_validator = TaskValidator(mock_kb)
        val_res = custom_validator.validate_task(task_state, purpose="publish")
        self.assertIn(val_res.overall_status, ("blocked_hard", "validation_error"))
        has_depth_err = any(v.constraint_id == "C002" or "max_depth_m" in v.message for v in val_res.violations)
        self.assertTrue(has_depth_err)

    # 4. malformed start_time：validation_error，不跳过动态检查
    def test_04_malformed_start_time_fail_closed(self):
        task_state = {
            "task_type_key": "pipeline_inspection",
            "equipment_unit_id": "WROV-250-001",
            "water_depth": 300.0,
            "start_time": "invalid_time_string",
        }

        val_res = self.validator.validate_task(task_state, purpose="publish")
        self.assertEqual(val_res.overall_status, "validation_error")
        self.assertIsNotNone(val_res.error)
        self.assertEqual(val_res.error.get("code"), "MALFORMED_TIME_FORMAT")

    # 5. current_velocity / turbidity：现有 soft/hard C013-C017 正确
    def test_05_dynamic_telemetry_soft_and_hard_violations(self):
        now_str = get_current_datetime().isoformat()
        # Soft violation (C015: current_velocity 0.6 m/s)
        state_snap_soft = {
            "unit_id": "WROV-250-001",
            "status_ref": "REF-001",
            "state_version": 1,
            "state": {"overall_status": "available", "current_velocity": 0.6, "update_timestamp": now_str},
        }

        # Hard violation (C017: current_velocity 1.5 m/s)
        state_snap_hard = {
            "unit_id": "WROV-250-001",
            "status_ref": "REF-001",
            "state_version": 1,
            "state": {"overall_status": "available", "current_velocity": 1.5, "update_timestamp": now_str},
        }

        mock_kb = MagicMock(spec=KnowledgeBase)
        mock_kb.get_constraints.return_value = self.kb.get_constraints()
        mock_kb.resolve_robot_unit.return_value = {
            "robot": {"full_name": "ROV-001", "max_depth_m": 3000, "robot_class": "rov"},
            "unit_id": "WROV-250-001",
        }
        mock_kb.robot_matches_task.return_value = True
        mock_kb.validate_robot_selection_from_task_state.return_value = {
            "unit_id": "WROV-250-001",
        }

        v_handler = TaskValidator(mock_kb)
        task_state = {
            "task_type_key": "pipeline_inspection",
            "equipment_unit_id": "WROV-250-001",
            "water_depth": 300.0,
            "start_time": now_str,
        }

        mock_kb.get_unit_state_snapshot.return_value = state_snap_soft
        res_soft = v_handler.validate_task(task_state, purpose="publish")
        self.assertEqual(res_soft.overall_status, "blocked_soft")
        self.assertTrue(any(v.constraint_id == "C015" for v in res_soft.violations))

        mock_kb.get_unit_state_snapshot.return_value = state_snap_hard
        res_hard = v_handler.validate_task(task_state, purpose="publish")
        self.assertEqual(res_hard.overall_status, "blocked_hard")
        self.assertTrue(any(v.constraint_id == "C017" for v in res_hard.violations))

    # 6. soft ack：当前状态下可继续；state/version 变化后失效
    def test_06_soft_ack_invalidation_on_state_change(self):
        dm = DialogueManager(kb=self.kb)
        dm.task_state = {
            "task_type_key": "pipeline_inspection",
            "equipment_unit_id": "WROV-250-001",
            "equipment_class": "rov",
            "equipment_family": "work_class_rov",
            "equipment_type": "heavy_work_class_rov",
            "water_depth": 300.0,
            "intent_id": "TI20260810001",
        }

        # Initial validation result with soft warning
        val_res_v1 = ValidationResult(
            overall_status="blocked_soft",
            validated_at="2026-08-10T12:00:00",
            task_version=1,
            validation_version=1,
            validation_fingerprint="fp_v1",
            state_snapshot={"status_ref": "REF-001", "state_version": 1},
            violations=[
                Violation(
                    constraint_id="C015",
                    constraint_name="流速中警示",
                    message="流速偏高",
                    severity="soft",
                )
            ],
        )
        dm.slot_store.validation_result = val_res_v1

        # Simulate user acknowledging warning
        v_soft = val_res_v1.violations[0]
        ack = ValidationAcknowledgement(
            constraint_id=v_soft.constraint_id,
            acknowledged_at="2026-08-10T12:00:00",
            task_version=val_res_v1.task_version,
            validation_version=val_res_v1.validation_version,
            validation_fingerprint=val_res_v1.validation_fingerprint,
            status_ref="REF-001",
            state_version=1,
            field="current_velocity",
            value=v_soft.observed_value,
        )
        dm.slot_store.validation_acknowledgements.append(ack)
        self.assertTrue(dm._is_whitelisted(v_soft))


        # Mutate state version in snapshot -> ack must invalidate
        val_res_v2 = ValidationResult(
            overall_status="blocked_soft",
            validated_at="2026-08-10T12:05:00",
            task_version=1,
            validation_version=2,
            validation_fingerprint="fp_v2",  # changed fingerprint
            state_snapshot={"status_ref": "REF-001", "state_version": 2},  # state_version mutated
            violations=[v_soft],
        )
        dm.slot_store.validation_result = val_res_v2
        self.assertFalse(dm._is_whitelisted(v_soft))

    # 7. hard violation：无论用户怎么确认都不能发布
    def test_07_hard_violation_cannot_be_bypassed(self):
        dm = DialogueManager(kb=self.kb)
        dm._transition_phase("blocked_hard", reason="test_hard_block")
        reply = dm.process("确认", request_id="req_hard_bypass")
        self.assertTrue("不能" in reply or "无法" in reply or "限制" in reply or "硬" in reply or "阻断" in reply)
        self.assertEqual(dm.phase, "blocked_hard")

    # 8. Validator/state lookup 抛异常：final 不生成，fail closed
    def test_08_validator_exception_fail_closed(self):
        mock_kb = MagicMock(spec=KnowledgeBase)
        mock_kb.get_unit_state_snapshot.side_effect = RuntimeError("Telemetry DB connection dropped")
        mock_kb.validate_robot_selection_from_task_state.return_value = {
            "unit_id": "WROV-250-001",
        }
        v_handler = TaskValidator(mock_kb)

        task_state = {
            "task_type_key": "pipeline_inspection",
            "equipment_unit_id": "WROV-250-001",
        }
        res = v_handler.validate_task(task_state, purpose="publish")
        self.assertEqual(res.overall_status, "validation_error")
        self.assertIsNotNone(res.error)
        self.assertIn("STATE_READ_FAILED", res.error.get("code", ""))


if __name__ == "__main__":
    unittest.main()
