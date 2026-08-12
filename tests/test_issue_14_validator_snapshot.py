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
        snapshot = self.kb.get_unit_state_snapshot("OBSROV--001")
        self.assertIsInstance(snapshot, dict)
        self.assertEqual(snapshot["unit_id"], "OBSROV--001")
        self.assertEqual(snapshot["status_ref"], "OBSROV-001")
        self.assertIn("state_version", snapshot)
        self.assertIn("updated_at", snapshot)
        self.assertIn("state", snapshot)

        # status_ref 直接传入抛出 StateSelectorError
        with self.assertRaises(StateSelectorError):
            self.kb.get_unit_state_snapshot("OBSROV-001")

        # 不存在的 unit_id 抛出 StateSelectorError
        with self.assertRaises(StateSelectorError):
            self.kb.get_unit_state_snapshot("NON_EXISTENT_UNIT_999")

        # 传入空字符串抛出 StateSelectorError
        with self.assertRaises(StateSelectorError):
            self.kb.get_unit_state_snapshot("")

    def test_turbidity_and_velocity_thresholds(self):
        """测试浑浊度 (C013/C014) 与流速 (C015/C016/C017) 的分级逻辑。"""
        self.kb.state_info.set_status("OBSROV-001", {"turbidity": 7, "current_velocity": 0.7})
        task_state = {
            "equipment_unit_id": "OBSROV--001",
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

        self.kb.state_info.set_status("OBSROV-001", {"turbidity": 15, "current_velocity": 0.9})
        res = self.validator.validate_task(task_state)
        c_ids = {v.constraint_id for v in res.violations}
        self.assertNotIn("C013", c_ids)
        self.assertIn("C014", c_ids)
        self.assertNotIn("C015", c_ids)
        self.assertIn("C016", c_ids)
        self.assertNotIn("C017", c_ids)

        self.kb.state_info.set_status("OBSROV-001", {"turbidity": 3, "current_velocity": 1.3})
        res = self.validator.validate_task(task_state)
        c_ids = {v.constraint_id for v in res.violations}
        self.assertIn("C017", c_ids)
        self.assertEqual(res.overall_status, "blocked_hard")

    def test_single_unit_isolation(self):
        """测试同一型号多台设备，只读取用户选择的 unit_id 的状态快照。"""
        self.kb.state_info.set_status("LROV--001", {"overall_status": "available", "current_velocity": 0.1})
        self.kb.state_info.set_status("LROV-002", {"overall_status": "available", "current_velocity": 1.5})

        task1 = {"equipment_unit_id": "LROV--001", "task_type_key": "pipeline_inspection"}
        res1 = self.validator.validate_task(task1)
        self.assertEqual(res1.overall_status, "valid")
        self.assertEqual(res1.state_snapshot["unit_id"], "LROV--001")

        task2 = {"equipment_unit_id": "LROV--002", "task_type_key": "pipeline_inspection"}
        res2 = self.validator.validate_task(task2)
        self.assertEqual(res2.overall_status, "blocked_hard")
        self.assertEqual(res2.state_snapshot["unit_id"], "LROV--002")

    def test_ambiguous_family_is_runtime_pending_not_val_err(self):
        """只提供 family/type 且对应多台单机时，应等待明确单机，不生成 VAL_ERR 业务违规。"""
        task_state = {
            "equipment_family": "observation_rov",
            "task_type_key": "pipeline_inspection",
        }
        res = self.validator.validate_task(task_state)
        self.assertEqual(res.overall_status, "pending_runtime_validation")
        self.assertEqual(res.runtime_diagnostic["code"], "AMBIGUOUS_UNIT_SELECTOR")
        self.assertEqual(res.violations, [])
        self.assertIsNone(res.error)

    def test_uncaught_validator_exception_is_not_business_constraint(self):
        """Validator 内部程序异常不再包装成 VAL_ERR 业务硬约束。"""
        original = self.validator._resolve_single_unit_snapshot
        try:
            def boom(*_args, **_kwargs):
                raise RuntimeError("synthetic validator bug")

            self.validator._resolve_single_unit_snapshot = boom
            res = self.validator.validate_task({
                "equipment_unit_id": "OBSROV--001",
                "task_type_key": "pipeline_inspection",
            })
        finally:
            self.validator._resolve_single_unit_snapshot = original

        self.assertEqual(res.overall_status, "pending_runtime_validation")
        self.assertEqual(res.runtime_diagnostic["code"], "VALIDATOR_EXCEPTION")
        self.assertEqual(res.violations, [])

    def test_communication_status_uses_telemetry_fields_not_robot_class(self):
        """通信检查读取状态字段本身，不再根据 robot_class 选择水声或脐带缆分支。"""
        self.kb.state_info.set_status(
            "OBSROV-001",
            {
                "overall_status": "available",
                "acoustic_comms_status": "abnormal",
                "tether_connection_status": "normal",
            },
        )
        res_acoustic = self.validator.validate_task({
            "equipment_unit_id": "OBSROV--001",
            "task_type_key": "pipeline_inspection",
        })
        c27 = next(v for v in res_acoustic.violations if v.constraint_id == "C027")
        self.assertIn("水声无线通信异常", c27.observed_value)

        self.kb.state_info.set_status(
            "AUV-324cc-001",
            {
                "overall_status": "available",
                "acoustic_comms_status": "normal",
                "tether_connection_status": "weak",
            },
        )
        res_tether = self.validator.validate_task({
            "equipment_unit_id": "AUV-324cc-001",
            "task_type_key": "pipeline_inspection",
        })
        c27 = next(v for v in res_tether.violations if v.constraint_id == "C027")
        self.assertIn("与母船连接异常", c27.observed_value)

    def test_future_task_pending_runtime_validation(self):
        """未来执行的任务（start_time 晚于当前）不使用当前遥测流速阻断，标记为 pending_runtime_validation。"""
        self.kb.state_info.set_status("OBSROV-001", {"current_velocity": 1.5})
        future_time = "2099-01-01T12:00:00"
        task_state = {
            "equipment_unit_id": "OBSROV--001",
            "task_type_key": "pipeline_inspection",
            "start_time": future_time,
        }
        res = self.validator.validate_task(task_state)
        self.assertEqual(res.overall_status, "pending_runtime_validation")
        c_ids = {v.constraint_id for v in res.violations}
        self.assertNotIn("C017", c_ids)


if __name__ == "__main__":
    unittest.main()
