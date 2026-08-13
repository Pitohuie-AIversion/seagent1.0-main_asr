"""
tests/test_issue_14_validator_snapshot.py
针对 Issue #14 批次一与批次二的定向单元测试：
1. 严格单机状态快照接口 get_unit_state_snapshot
2. TaskValidator 结构化校验服务 (validate_task)
"""

import shutil
import tempfile
import unittest
from pathlib import Path

from src.knowledge_retriever import KnowledgeBase
from src.validator import TaskValidator, ValidationResult, Violation
from src.exceptions import StateSelectorError, StateSnapshotValidationError


def _make_kb(tmp_dir: Path) -> KnowledgeBase:
    state_file = tmp_dir / "state.yaml"
    shutil.copy("config/state.yaml", state_file)
    kb_inst = KnowledgeBase()
    kb_inst.state_info.state_file = state_file
    return kb_inst


class TestGetUnitStateSnapshotStrict(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.kb = _make_kb(Path(self._tmp))
        self.validator = TaskValidator(self.kb)

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_get_unit_state_snapshot_strict(self):
        """测试批次一：get_unit_state_snapshot 必须精确匹配 unit_id 且按 status_ref 读取。"""
        snapshot = self.kb.get_unit_state_snapshot("OBSROV-75-001")
        self.assertIsInstance(snapshot, dict)
        self.assertEqual(snapshot["unit_id"], "OBSROV-75-001")
        self.assertEqual(snapshot["status_ref"], "OBSROV-75-001")
        self.assertIn("state_version", snapshot)
        self.assertIn("updated_at", snapshot)
        self.assertIn("state", snapshot)

        # 不存在的 unit_id 抛出 StateSelectorError
        with self.assertRaises(StateSelectorError):
            self.kb.get_unit_state_snapshot("NON_EXISTENT_UNIT_999")

        # 传入空字符串抛出 StateSelectorError
        with self.assertRaises(StateSelectorError):
            self.kb.get_unit_state_snapshot("")

    def test_turbidity_and_velocity_thresholds(self):
        """测试浑浊度 (C013/C014) 与流速 (C015/C016/C017) 的分级逻辑。"""
        self.kb.state_info.set_status("OBSROV-75-001", {"turbidity": 7, "current_velocity": 0.7})
        task_state = {
            "equipment_unit_id": "OBSROV-75-001",
            "task_type_key": "pipeline_inspection",
        }
        res = self.validator.validate_task(task_state)
        c_ids = {v.constraint_id for v in res.violations}
        self.assertIn("C013", c_ids)
        self.assertNotIn("C014", c_ids)
        self.assertIn("C015", c_ids)
        self.assertNotIn("C016", c_ids)
        self.assertNotIn("C017", c_ids)
        self.assertEqual(res.overall_status, "blocked_soft")

        self.kb.state_info.set_status("OBSROV-75-001", {"turbidity": 15, "current_velocity": 0.9})
        res = self.validator.validate_task(task_state)
        c_ids = {v.constraint_id for v in res.violations}
        self.assertNotIn("C013", c_ids)
        self.assertIn("C014", c_ids)
        self.assertNotIn("C015", c_ids)
        self.assertIn("C016", c_ids)
        self.assertNotIn("C017", c_ids)

        self.kb.state_info.set_status("OBSROV-75-001", {"turbidity": 3, "current_velocity": 1.3})
        res = self.validator.validate_task(task_state)
        c_ids = {v.constraint_id for v in res.violations}
        self.assertIn("C017", c_ids)
        self.assertEqual(res.overall_status, "blocked_hard")

    def test_single_unit_isolation(self):
        """测试同一型号多台设备，只读取用户选择的 unit_id 的状态快照。"""
        self.kb.state_info.set_status("LROV-150-001", {"overall_status": "available", "current_velocity": 0.1})
        self.kb.state_info.set_status("LROV-150-002", {"overall_status": "available", "current_velocity": 1.5})

        task1 = {"equipment_unit_id": "LROV-150-001", "task_type_key": "pipeline_inspection"}
        res1 = self.validator.validate_task(task1)
        self.assertEqual(res1.overall_status, "valid")
        self.assertEqual(res1.state_snapshot["unit_id"], "LROV-150-001")

        task2 = {"equipment_unit_id": "LROV-150-002", "task_type_key": "pipeline_inspection"}
        res2 = self.validator.validate_task(task2)
        self.assertEqual(res2.overall_status, "blocked_hard")
        self.assertEqual(res2.state_snapshot["unit_id"], "LROV-150-002")

    def test_ambiguous_family_returns_validation_error(self):
        """当只提供 family/type 且对应多台单机时，无法唯一确定单机，应返回 validation_error。"""
        task_state = {
            "equipment_family": "light_work_class_rov",
            "task_type_key": "pipeline_inspection",
        }
        res = self.validator.validate_task(task_state)
        self.assertEqual(res.overall_status, "validation_error")
        self.assertIsNotNone(res.error)
        self.assertEqual(res.error["code"], "AMBIGUOUS_UNIT_SELECTOR")
        self.assertGreater(len(res.violations), 0)
        self.assertEqual(res.violations[0].constraint_id, "VAL_ERR")

    def test_future_task_pending_runtime_validation(self):
        """未来执行的任务（start_time 晚于当前）不使用当前遥测流速阻断，标记为 pending_runtime_validation。"""
        self.kb.state_info.set_status("OBSROV-75-001", {"current_velocity": 1.5})
        future_time = "2099-01-01T12:00:00"
        task_state = {
            "equipment_unit_id": "OBSROV-75-001",
            "task_type_key": "pipeline_inspection",
            "start_time": future_time,
        }
        res = self.validator.validate_task(task_state)
        self.assertEqual(res.overall_status, "pending_runtime_validation")
        c_ids = {v.constraint_id for v in res.violations}
        self.assertNotIn("C017", c_ids)

    def test_mixed_robot_hierarchy_is_validation_error(self):
        """显式 Class 与 Family/Variant/Unit 不一致时，Validator 必须失败关闭。"""
        task_state = {
            "task_type_key": "pipeline_inspection",
            "equipment_class": "auv",
            "equipment_family": "观察级深海机器人",
            "equipment_type": "观察级深海机器人 75HP",
            "equipment_unit_id": "OBSROV-75-001",
        }

        res = self.validator.validate_task(task_state, purpose="publish")

        self.assertEqual(res.overall_status, "validation_error")
        self.assertIsNotNone(res.error)
        self.assertEqual(res.error["code"], "FAMILY_CLASS_MISMATCH")
        self.assertEqual(res.violations[0].constraint_id, "VAL_ERR")

    def test_partial_disallowed_class_or_family_fails_closed_interactive(self):
        """只有 Class/Family 时也必须执行任务准入，不能等不存在的 C001/C002 fact。"""
        cases = (
            {"equipment_class": "auv"},
            {"equipment_family": "观察级深海机器人"},
        )
        for selectors in cases:
            with self.subTest(selectors=selectors):
                res = self.validator.validate_task(
                    {
                        "task_type_key": "tree_valve_operation",
                        **selectors,
                    }
                )

                self.assertEqual(res.overall_status, "validation_error")
                self.assertIsNotNone(res.error)
                self.assertEqual(
                    res.error["code"],
                    "CLASS_NOT_ALLOWED_FOR_TASK",
                )
                self.assertEqual(res.violations[0].constraint_id, "VAL_ERR")
                self.assertEqual(res.violations[0].severity, "hard")
                self.assertIsNone(res.state_snapshot)

    def test_partial_disallowed_class_fails_closed_in_incremental_validation(self):
        """增量校验与全量校验保持相同的 Class-only fail-closed 语义。"""
        violations = self.validator.validate_for_fields(
            {
                "task_type_key": "tree_valve_operation",
                "equipment_class": "auv",
            },
            changed_fields={"equipment_class"},
        )

        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].constraint_id, "VAL_ERR")
        self.assertEqual(violations[0].severity, "hard")
        self.assertIn("not allowed", violations[0].message)

    def test_exact_unit_without_explicit_parents_uses_registry_hierarchy(self):
        """只提供精确 Unit 时允许从注册表反推父级，不误伤旧调用方。"""
        task_state = {
            "task_type_key": "pipeline_inspection",
            "equipment_unit_id": "OBSROV-75-001",
        }

        res = self.validator.validate_task(task_state)

        self.assertNotEqual(res.overall_status, "validation_error")
        self.assertIsNone(res.error)

    def test_publish_fails_closed_when_static_robot_validator_is_unavailable(self):
        """发布边界不能因降级 KB 缺少四级校验器而跳过静态关系校验。"""

        class KnowledgeBaseWithoutStaticRobotValidator:
            def get_constraints(self):
                return []

        validator = TaskValidator(KnowledgeBaseWithoutStaticRobotValidator())

        res = validator.validate_task(
            {
                "task_type_key": "pipeline_inspection",
                "equipment_unit_id": "OBSROV-75-001",
            },
            purpose="publish",
        )

        self.assertEqual(res.overall_status, "validation_error")
        self.assertEqual(
            res.error["code"],
            "STATIC_ROBOT_VALIDATOR_UNAVAILABLE",
        )

    def test_runtime_execution_requires_exact_unit(self):
        """执行期与 preview/publish 一样，必须已锁定具体 Unit。"""
        res = self.validator.validate_task(
            {"task_type_key": "pipeline_inspection"},
            purpose="runtime_execution",
        )

        self.assertEqual(res.overall_status, "validation_error")
        self.assertEqual(res.error["code"], "MISSING_UNIT_ID")

    def test_static_validator_result_contract_fails_closed(self):
        """None/空字典/错字段不得伪装为已验证的发布级 Unit。"""
        task_state = {
            "task_type_key": "pipeline_inspection",
            "equipment_unit_id": "OBSROV-75-001",
        }
        original = self.kb.validate_robot_selection_from_task_state
        try:
            for broken_result in (None, {}, {"foo": "bar"}, {"unit_id": ""}):
                with self.subTest(broken_result=broken_result):
                    self.kb.validate_robot_selection_from_task_state = (
                        lambda *_args, _result=broken_result, **_kwargs: _result
                    )
                    res = self.validator.validate_task(
                        task_state,
                        purpose="publish",
                    )
                    self.assertEqual(res.overall_status, "validation_error")
                    self.assertEqual(
                        res.error["code"],
                        "STATIC_ROBOT_VALIDATOR_FAILURE",
                    )
        finally:
            self.kb.validate_robot_selection_from_task_state = original

    def test_broken_static_validator_cannot_invent_missing_runtime_unit(self):
        """执行期输入仍必须自带 Unit，不能信任坏 helper 凭空返回的 ID。"""
        original = self.kb.validate_robot_selection_from_task_state
        try:
            self.kb.validate_robot_selection_from_task_state = (
                lambda *_args, **_kwargs: {"unit_id": "OBSROV-75-001"}
            )
            res = self.validator.validate_task(
                {"task_type_key": "pipeline_inspection"},
                purpose="runtime_execution",
            )
        finally:
            self.kb.validate_robot_selection_from_task_state = original

        self.assertEqual(res.overall_status, "validation_error")
        self.assertEqual(res.error["code"], "MISSING_UNIT_ID")


if __name__ == "__main__":
    unittest.main()
