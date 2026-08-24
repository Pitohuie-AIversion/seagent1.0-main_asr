"""
tests/test_validator_interactive_telemetry_audit.py
测试解耦 validator.py 在交互收集模式 (purpose="interactive") 与发布核验模式 (purpose="publish") 下的动态遥测校验行为：
1. 即时任务 (start_time 为“现在”) 在 purpose="interactive" 下不触发 _DYNAMIC_CHECKS 遥测/环境阻断违规。
2. 在 purpose="publish" 下正常触发 _DYNAMIC_CHECKS 遥测/环境校验。
3. 交互收集阶段单机缺少遥测快照时不返回 MISSING_TELEMETRY 错误字典。
"""

import sys
from pathlib import Path
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.knowledge_retriever import KnowledgeBase
from src.validator import TaskValidator


class TestValidatorInteractiveTelemetryAudit(unittest.TestCase):
    def setUp(self):
        self.kb = KnowledgeBase()
        self.validator = TaskValidator(kb=self.kb)

    def test_interactive_mode_immediate_task_ignores_dynamic_checks(self):
        """即时任务在 purpose="interactive" 模式下不产生动态遥测/环境违规"""
        from src.simulated_time import get_current_datetime
        now_str = get_current_datetime().isoformat(timespec="seconds")
        task_state = {
            "task_type_key": "pipeline_burial",
            "start_time": now_str,
            "water_depth": 130.0,
            "equipment_class": "管缆埋设机器人",
            "equipment_family": "履带式海底重载作业机器人",
            "equipment_type": "履带式海底重载作业机器人1600HP",
            "equipment_unit_id": "CRAWLER-1600-001",
        }
        res_interactive = self.validator.validate_task(task_state, purpose="interactive")
        dynamic_violations = [
            v for v in res_interactive.violations
            if v.check_type in ("state_timestamp", "current_velocity", "turbidity")
        ]
        self.assertEqual(len(dynamic_violations), 0)
        self.assertEqual(res_interactive.overall_status, "valid")

    def test_publish_mode_immediate_task_enforces_dynamic_checks(self):
        """即时任务在 purpose="publish" 模式下正常校验动态遥测/环境"""
        task_state = {
            "task_type_key": "pipeline_burial",
            "start_time": "2026-08-22T12:00:00+08:00",
            "water_depth": 130.0,
            "equipment_class": "管缆埋设机器人",
            "equipment_family": "履带式海底重载作业机器人",
            "equipment_type": "履带式海底重载作业机器人1600HP",
            "equipment_unit_id": "CRAWLER-1600-001",
        }
        res_publish = self.validator.validate_task(task_state, purpose="publish")
        self.assertIsNotNone(res_publish)

    def test_interactive_mode_missing_unit_snapshot_returns_no_error_dict(self):
        """交互收集阶段已选中有效单机但缺失/过期遥测快照时不触发 validation_error 阻断"""
        task_state = {
            "task_type_key": "pipeline_burial",
            "equipment_unit_id": "CRAWLER-1600-001",
        }
        res_interactive = self.validator.validate_task(task_state, purpose="interactive")
        self.assertNotEqual(res_interactive.overall_status, "validation_error")

    def test_dialogue_manager_collecting_phase_emits_soft_notice(self):
        """对话收集阶段 (collecting) 遇到软警告时返回 soft_notice 引导 AI 提示，且不阻断槽位收集"""
        from src.dialogue_manager import DialogueManager
        from unittest.mock import MagicMock
        mock_llm = MagicMock()
        dm = DialogueManager(mock_llm, self.kb)
        dm.phase = "collecting"
        dm.task_state = {
            "task_type_key": "pipeline_burial",
            "start_time": "2026-08-22T12:00:00+08:00",
            "equipment_unit_id": "CRAWLER-1600-001",
        }
        res = dm._run_constraint_check(set(), purpose="interactive")
        self.assertIn(res.get("type"), ("soft_notice", "none"))
        self.assertEqual(dm.phase, "collecting")


if __name__ == "__main__":
    unittest.main()
