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
from src.validation.validator import TaskValidator


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
        val_errors = [v for v in res.violations if v.check_type == "validation_error"]
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
        val_errors = [v for v in res.violations if v.check_type == "validation_error"]
        self.assertGreater(len(val_errors), 0)

    def test_incremental_validation_for_variant_field_does_not_fail(self):
        """增量字段校验（用户更新 equipment_type）时不应返回 validation_error 违规。"""
        task_state = {
            "task_type_key": "pipeline_inspection",
            "equipment_type": "轻型工作级深海机器人 150HP",
            "water_depth": 100.0,
        }
        violations = self.validator.validate_for_fields(task_state, changed_fields={"equipment_type"}, purpose="interactive")
        val_errors = [v for v in violations if v.check_type == "validation_error"]
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
        from src.slots.slot_store import Slot
        dm.phase = "collecting"
        dm.slot_store.slots["equipment_type"] = Slot(slot_name="equipment_type", value="轻型工作级深海机器人 150HP", status="confirmed")

        # 触发交互校验刷新
        val_res = dm._refresh_validation(purpose="interactive", changed_fields={"equipment_type"})
        val_errors = [v for v in val_res.violations if v.check_type == "validation_error"]
        self.assertEqual(val_errors, [])
        self.assertNotEqual(val_res.overall_status, "blocked_hard")
        self.assertNotEqual(val_res.overall_status, "validation_error")
        # 确认 phase 保持在 collecting
        self.assertEqual(dm.phase, "collecting")

    def test_oral_serial_unit_resolution(self):
        """测试 001号、001号机、002号机、2号机等口语化单机编号的精准解析。"""
        # 在指定 variant 情况下，001 相关口语均应解析为 LROV-150-001
        for alias in ["001号", "001号机", "1号机", "1号", "001", "一号机"]:
            unit = self.kb.resolve_robot_unit(alias, "pipeline_inspection", "轻型工作级深海机器人 150HP")
            self.assertIsNotNone(unit, f"Failed to resolve {alias}")
            self.assertEqual(unit["unit_id"], "LROV-150-001")

        # 在指定 variant 情况下，002 相关口语均应解析为 LROV-150-002
        for alias in ["002号", "002号机", "2号机", "2号", "002", "二号机"]:
            unit = self.kb.resolve_robot_unit(alias, "pipeline_inspection", "轻型工作级深海机器人 150HP")
            self.assertIsNotNone(unit, f"Failed to resolve {alias}")
            self.assertEqual(unit["unit_id"], "LROV-150-002")

    def test_simultaneous_parent_and_valid_unit_update_all_succeed(self):
        """测试同轮输入系列、型号及 001号 单机编号时，四级槽位均成功写入 valid。"""
        from src.dialogue_manager import DialogueManager

        dm = DialogueManager()
        slots = dm.slot_store.clone_slots()
        slots["task_type_key"].value = "pipeline_inspection"
        slots["task_type_key"].status = "valid"
        updates = {
            "equipment_family": "轻型工作级深海机器人",
            "equipment_type": "150HP",
            "equipment_unit_id": "001号",
        }
        dm._apply_updates_in_transaction(updates, slots, allow_overwrite=True)
        dm.slot_store.commit_transaction(slots, [])

        res_slots = dm.slot_store.slots
        self.assertEqual(res_slots["equipment_family"].status, "valid")
        self.assertEqual(res_slots["equipment_family"].value, "轻型工作级深海机器人")
        self.assertEqual(res_slots["equipment_type"].status, "valid")
        self.assertEqual(res_slots["equipment_type"].value, "轻型工作级深海机器人 150HP")
        self.assertEqual(res_slots["equipment_unit_id"].status, "valid")
        self.assertEqual(res_slots["equipment_unit_id"].value, "LROV-150-001")
        self.assertEqual(res_slots["equipment_name"].status, "valid")
        self.assertEqual(res_slots["equipment_name"].value, "轻型工作级深海机器人150HP-001")

    def test_unit_failure_does_not_rollback_valid_parent_slots(self):
        """当单机编号未知或不合法时，已合法校验的系列与型号槽位必须保留 valid，不得连坐回滚抹除。"""
        from src.dialogue_manager import DialogueManager

        dm = DialogueManager()
        slots = dm.slot_store.clone_slots()
        slots["task_type_key"].value = "pipeline_inspection"
        slots["task_type_key"].status = "valid"
        updates = {
            "equipment_family": "轻型工作级深海机器人",
            "equipment_type": "150HP",
            "equipment_unit_id": "999号机不存在",
        }
        dm._apply_updates_in_transaction(updates, slots, allow_overwrite=True)
        dm.slot_store.commit_transaction(slots, [])

        res_slots = dm.slot_store.slots
        # 上级系列和型号应成功保留为 valid
        self.assertEqual(res_slots["equipment_family"].status, "valid")
        self.assertEqual(res_slots["equipment_family"].value, "轻型工作级深海机器人")
        self.assertEqual(res_slots["equipment_type"].status, "valid")
        self.assertEqual(res_slots["equipment_type"].value, "轻型工作级深海机器人 150HP")
        # 仅目标 unit 标记为 invalid
        self.assertEqual(res_slots["equipment_unit_id"].status, "invalid")
        self.assertIsNone(res_slots["equipment_unit_id"].value)
        self.assertEqual(res_slots["equipment_unit_id"].candidate_value, "999号机不存在")
        self.assertIn("Unknown fleet unit", res_slots["equipment_unit_id"].validation_error)

    def test_selected_family_and_unit_auto_converges_parent_variant(self):
        """用户已选定系列（轻型），输入 001号 时自动在系列范围内消歧，并自动收敛父级型号与大类。"""
        from src.dialogue_manager import DialogueManager

        dm = DialogueManager()
        slots = dm.slot_store.clone_slots()
        slots["task_type_key"].value = "pipeline_inspection"
        slots["task_type_key"].status = "valid"
        # 上一轮已确定系列，但未指定具体型号
        slots["equipment_family"].value = "轻型工作级深海机器人"
        slots["equipment_family"].status = "valid"
        self.assertIsNone(slots["equipment_type"].value)
        self.assertEqual(slots["equipment_type"].status, "missing")

        # 本轮仅输入单机编号
        updates = {
            "equipment_unit_id": "001号",
        }
        dm._apply_updates_in_transaction(updates, slots, allow_overwrite=True)
        dm.slot_store.commit_transaction(slots, [])

        res_slots = dm.slot_store.slots
        # 验证单机编号成功解析为 LROV-150-001
        self.assertEqual(res_slots["equipment_unit_id"].status, "valid")
        self.assertEqual(res_slots["equipment_unit_id"].value, "LROV-150-001")
        # 验证父级型号自动收敛为轻型工作级深海机器人 150HP 且状态为 valid
        self.assertEqual(res_slots["equipment_type"].status, "valid")
        self.assertEqual(res_slots["equipment_type"].value, "轻型工作级深海机器人 150HP")
        # 验证设备大类自动收敛
        self.assertEqual(res_slots["equipment_class"].status, "valid")
        self.assertEqual(res_slots["equipment_class"].value, "observation_rov")

    def test_simultaneous_family_and_unit_without_type_auto_converges(self):
        """同轮输入系列（轻型）与单机（001号）而未指定型号时，自动收敛父级型号为 150HP。"""
        from src.dialogue_manager import DialogueManager

        dm = DialogueManager()
        slots = dm.slot_store.clone_slots()
        slots["task_type_key"].value = "pipeline_inspection"
        slots["task_type_key"].status = "valid"

        updates = {
            "equipment_family": "轻型",
            "equipment_unit_id": "001号",
        }
        dm._apply_updates_in_transaction(updates, slots, allow_overwrite=True)
        dm.slot_store.commit_transaction(slots, [])

        res_slots = dm.slot_store.slots
        self.assertEqual(res_slots["equipment_family"].status, "valid")
        self.assertEqual(res_slots["equipment_family"].value, "轻型工作级深海机器人")
        self.assertEqual(res_slots["equipment_type"].status, "valid")
        self.assertEqual(res_slots["equipment_type"].value, "轻型工作级深海机器人 150HP")
        self.assertEqual(res_slots["equipment_unit_id"].status, "valid")
        self.assertEqual(res_slots["equipment_unit_id"].value, "LROV-150-001")

    def test_composite_unit_input_auto_converges(self):
        """直接输入带系列前缀的复合单机编号（轻型 001号 或 轻型001号），自动收敛系列、型号与单机。"""
        from src.dialogue_manager import DialogueManager

        for text_input in ["轻型 001号", "轻型001号"]:
            dm = DialogueManager()
            slots = dm.slot_store.clone_slots()
            slots["task_type_key"].value = "pipeline_inspection"
            slots["task_type_key"].status = "valid"

            updates = {
                "equipment_unit_id": text_input,
            }
            dm._apply_updates_in_transaction(updates, slots, allow_overwrite=True)
            dm.slot_store.commit_transaction(slots, [])

            res_slots = dm.slot_store.slots
            self.assertEqual(res_slots["equipment_unit_id"].status, "valid", f"Failed for {text_input}")
            self.assertEqual(res_slots["equipment_unit_id"].value, "LROV-150-001")
            self.assertEqual(res_slots["equipment_type"].status, "valid")
            self.assertEqual(res_slots["equipment_type"].value, "轻型工作级深海机器人 150HP")
            self.assertEqual(res_slots["equipment_family"].status, "valid")
            self.assertEqual(res_slots["equipment_family"].value, "轻型工作级深海机器人")

