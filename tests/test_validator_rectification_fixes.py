"""
tests/test_validator_rectification_fixes.py
测试 validator 修复项：
1. update_at 时间戳多字段降级兼容
2. min_inclusive 阈值闭区间匹配
3. 维护/故障状态诊断文案精准化
4. 非字符串 equipment_unit_id Selector 兼容解析
"""

import shutil
import sys
import tempfile
from pathlib import Path
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.validator import TaskValidator, Violation, _matches_numeric_thresholds, _display_threshold
from src.knowledge_retriever import KnowledgeBase


def _make_isolated_kb(tmp_dir: Path) -> KnowledgeBase:
    state_file = tmp_dir / "state.yaml"
    shutil.copy("config/state.yaml", state_file)
    kb_inst = KnowledgeBase()
    kb_inst.state_info.state_file = state_file
    return kb_inst


class TestValidatorRectificationFixes(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.kb = _make_isolated_kb(Path(self._tmp))
        self.validator = TaskValidator(self.kb)

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_min_inclusive_threshold(self):
        thresholds = {"min_inclusive": 1.0, "max_inclusive": 2.0}
        self.assertFalse(_matches_numeric_thresholds(0.9, thresholds))
        self.assertTrue(_matches_numeric_thresholds(1.0, thresholds))
        self.assertTrue(_matches_numeric_thresholds(1.5, thresholds))
        self.assertTrue(_matches_numeric_thresholds(2.0, thresholds))
        self.assertFalse(_matches_numeric_thresholds(2.1, thresholds))
        self.assertEqual(_display_threshold(thresholds), 2.0)

    def test_positive_water_depth_required(self):
        val, err = self.validator._validate_water_depth_value(500)
        self.assertIsNone(err)
        self.assertEqual(val, 500.0)

        val_zero, err_zero = self.validator._validate_water_depth_value(0)
        self.assertIsNotNone(err_zero)
        self.assertEqual(err_zero["code"], "INVALID_WATER_DEPTH")

    def test_fault_maintenance_status_message(self):
        self.kb.state_info.set_status("OBSROV-75-001", {"overall_status": "fault"})
        task_state = {
            "equipment_unit_id": "OBSROV-75-001",
            "task_type_key": "pipeline_inspection",
        }
        res = self.validator.validate_task(task_state, purpose="publish")
        self.assertEqual(res.overall_status, "blocked_hard")
        self.assertGreater(len(res.violations), 0)
        v_msg = res.violations[0].message
        self.assertIn("故障/维护状态", v_msg)

    def test_update_at_field_fallback(self):
        snapshot = {
            "unit_id": "CRAWLER-1600-001",
            "status_ref": "CRAWLER-1600-001",
            "state_version": 1,
            "state": {
                "overall_status": "available",
                "update_at": "2026-06-30T17:38:00+08:00",
            },
        }
        err = self.validator._validate_state_snapshot_content("CRAWLER-1600-001", snapshot)
        self.assertIsNone(err)

    def test_non_string_unit_selector(self):
        task_state = {
            "equipment_unit_id": "OBSROV-75-001",
            "task_type_key": "pipeline_inspection",
        }
        res = self.validator.validate_task(task_state)
        self.assertIsNotNone(res)
        self.assertEqual(res.state_snapshot["unit_id"], "OBSROV-75-001")


if __name__ == "__main__":
    unittest.main()
