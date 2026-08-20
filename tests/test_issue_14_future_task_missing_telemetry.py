"""
test_issue_14_future_task_missing_telemetry.py — 覆盖未来规划任务在遥测缺失/过期时的 pending_runtime_validation 逻辑
"""

import shutil
import tempfile
import unittest
from pathlib import Path

from src.knowledge_retriever import KnowledgeBase
from src.validator import TaskValidator


class TestFutureTaskMissingTelemetry(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        state_file = Path(self._tmp) / "state.yaml"
        shutil.copy("config/state.yaml", state_file)
        self.kb = KnowledgeBase()
        self.kb.state_info.state_file = state_file

        # 明确删除目标 status_ref ("OBSROV-75-001") 的状态记录
        snap = self.kb.state_info._load_state_unlocked()
        if "robots" in snap and "OBSROV-75-001" in snap["robots"]:
            del snap["robots"]["OBSROV-75-001"]
            self.kb.state_info._save_state_unlocked(snap)

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_future_task_missing_telemetry_returns_pending_runtime_validation(self):
        validator = TaskValidator(self.kb)

        # 提交一个未来两周的任务
        from src.simulated_time import get_current_datetime
        from datetime import timedelta
        future_start = get_current_datetime() + timedelta(days=14)
        future_end = future_start + timedelta(hours=8)
        task_state = {
            "task_type_key": "pipeline_inspection",
            "equipment_unit_id": "OBSROV-75-001",
            "start_time": future_start.isoformat(),
            "end_time": future_end.isoformat(),
            "water_depth": 300,
        }

        res = validator.validate_task(task_state, purpose="interactive")
        # 未来任务在缺乏当前具体执行环境时，不应当错判为 error，而应精确返回 pending_runtime_validation
        self.assertEqual(res.overall_status, "pending_runtime_validation")


if __name__ == "__main__":
    unittest.main()
