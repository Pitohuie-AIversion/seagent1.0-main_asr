"""
tests/test_equipment_unit_interactive_selection.py

测试在多轮对话交互槽位收集阶段：
当用户选定设备型号/系列（该型号下存在多台在役单机）而尚未指定具体单机编号时：
1. Validator 在 interactive 模式下不误报 VAL_ERR 单机状态校验硬性违规；
2. DialogueManager 阶段保持为 collecting，不进入 blocked_hard；
3. 系统正常引导用户从候选列表中选择具体单机编号；
4. 当用户在发布确认阶段（publish 模式）仍缺少单机编号时，严格阻断发布并报 validation_error。
"""

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from src.knowledge_retriever import KnowledgeBase
from src.validator import TaskValidator


def _make_kb(tmp_dir: Path) -> KnowledgeBase:
    state_file = tmp_dir / "state.yaml"
    shutil.copy("config/state.yaml", state_file)
    kb_inst = KnowledgeBase()
    kb_inst.state_info.state_file = state_file
    return kb_inst


class TestEquipmentUnitInteractiveSelection(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.kb = _make_kb(Path(self._tmp))
        self.validator = TaskValidator(self.kb)

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_interactive_variant_selection_with_multiple_units_not_blocked(self):
        """用户选定了轻型工作级深海机器人 150HP（对应 LROV-150-001 和 LROV-150-002），交互阶段不应产生 VAL_ERR 违规。"""
        task_state = {
            "task_type": "管缆巡检",
            "task_type_key": "pipeline_inspection",
            "cable_type": "power_cable",
            "water_depth": 100.0,
            "start_point": "19.8,113.5",
            "end_point": "19.8,113.6",
            "equipment_class": "observation_rov",
            "equipment_family": "light_work_class_rov",
            "equipment_type": "轻型工作级深海机器人 150HP",
            "start_time": "2099-08-14T14:51:00",
            "end_time": "2099-08-14T16:31:00",
        }
        res = self.validator.validate_task(task_state, purpose="interactive")
        self.assertIsNone(res.error)
        self.assertIsNone(res.state_snapshot)
        val_errors = [v for v in res.violations if v.constraint_id == "VAL_ERR"]
        self.assertEqual(val_errors, [])
        self.assertNotEqual(res.overall_status, "blocked_hard")
        self.assertNotEqual(res.overall_status, "validation_error")

    def test_publish_mode_without_unit_id_is_blocked(self):
        """在发布阶段 (publish)，如果仍未指定确切单机编号，必须返回 validation_error 并阻断。"""
        task_state = {
            "task_type": "管缆巡检",
            "task_type_key": "pipeline_inspection",
            "cable_type": "power_cable",
            "water_depth": 100.0,
            "start_point": "19.8,113.5",
            "end_point": "19.8,113.6",
            "equipment_class": "observation_rov",
            "equipment_family": "light_work_class_rov",
            "equipment_type": "轻型工作级深海机器人 150HP",
            "start_time": "2099-08-14T14:51:00",
            "end_time": "2099-08-14T16:31:00",
        }
        res = self.validator.validate_task(task_state, purpose="publish")
        self.assertEqual(res.overall_status, "validation_error")
        self.assertIsNotNone(res.error)
        val_errors = [v for v in res.violations if v.constraint_id == "VAL_ERR"]
        self.assertGreater(len(val_errors), 0)

    def test_incremental_validation_for_variant_field_does_not_fail(self):
        """增量字段校验（用户更新 equipment_type）时不应返回 VAL_ERR 违规。"""
        task_state = {
            "task_type_key": "pipeline_inspection",
            "equipment_type": "轻型工作级深海机器人 150HP",
            "water_depth": 100.0,
        }
        violations = self.validator.validate_for_fields(task_state, changed_fields={"equipment_type"})
        val_errors = [v for v in violations if v.constraint_id == "VAL_ERR"]
        self.assertEqual(val_errors, [])

    def test_dialogue_manager_select_variant_keeps_collecting_phase(self):
        """在 DialogueManager 中输入设备型号，该型号有多台单机时，phase 必须保持 collecting，正常引导选择单机。"""
        from src.dialogue_manager import DialogueManager

        mock_llm = MagicMock()
        mock_llm.chat.return_value = "好的，已选择轻型工作级深海机器人 150HP。请从以下编号中选择具体单机：LROV-150-001 或 LROV-150-002。"
        mock_llm.filter_reply.side_effect = lambda text, *args, **kwargs: text

        dm = DialogueManager(llm=mock_llm)
        dm.task_state = {
            "task_type": "管缆巡检",
            "task_type_key": "pipeline_inspection",
            "cable_type": "power_cable",
            "water_depth": 100.0,
            "start_point": "19.8,113.5",
            "end_point": "19.8,113.6",
            "equipment_class": "observation_rov",
            "equipment_family": "light_work_class_rov",
            "start_time": "2099-08-14T14:51:00",
            "end_time": "2099-08-14T16:31:00",
        }
        from src.slot_store import Slot
        dm.phase = "collecting"
        dm.slot_store.slots["equipment_type"] = Slot(slot_name="equipment_type", value="轻型工作级深海机器人 150HP", status="confirmed")

        # 触发交互校验刷新
        val_res = dm._refresh_validation(purpose="interactive", changed_fields={"equipment_type"})
        val_errors = [v for v in val_res.violations if v.constraint_id == "VAL_ERR"]
        self.assertEqual(val_errors, [])
        self.assertNotEqual(val_res.overall_status, "blocked_hard")
        self.assertNotEqual(val_res.overall_status, "validation_error")
        # 确认 phase 保持在 collecting
        self.assertEqual(dm.phase, "collecting")

