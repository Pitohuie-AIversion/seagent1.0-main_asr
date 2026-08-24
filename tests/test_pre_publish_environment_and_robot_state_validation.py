"""
测试发布前环境信息与机器人状态判断逻辑：
确保在任务收集完成进入 confirming 阶段以及最终确认发布前，
环境信息（海况、流速、浑浊度等）与机器人状态（各子系统状态、总体可用性）被完整过一遍并给出相应提醒与阻断。
"""

import copy
import json
import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from datetime import datetime
from zoneinfo import ZoneInfo

from src.dialogue_manager import DialogueManager
from src.knowledge_retriever import KnowledgeBase
from src.llm_client import LLMClient
from src.simulated_time import get_simulated_time
from src.ui_state_builder import build_frontend_ui_state
from src.validator import TaskValidator, Violation


class TestPrePublishEnvironmentAndRobotStateValidation(unittest.TestCase):

    def setUp(self):
        get_simulated_time().set_current_time(
            datetime(2026, 8, 14, 17, 0, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        )
        self.kb = KnowledgeBase()
        self.llm = LLMClient(None, None)
        self.dm = DialogueManager(self.llm, self.kb, session_id="test_pre_publish_validation")

    def tearDown(self):
        get_simulated_time().reset()

    def test_pre_publish_surfaces_thruster_soft_warning_on_complete_slots(self):
        """当所有必填槽位填齐准备进入确认时，单机推进器状态异常能正确触发 C022 软警告。"""
        task_state = {
            "task_type_key": "pipeline_inspection",
            "task_type": "管道巡检",
            "start_time": "2026-08-14 17:30:00",
            "end_time": "2026-08-14 19:00:00",
            "water_depth": 500.0,
            "cable_type": "海底油气管道",
            "equipment_class": "observation_rov",
            "equipment_family": "light_work_class_rov",
            "equipment_type": "轻型工作级深海机器人 150HP",
            "equipment_unit_id": "LROV-150-001",
            "payload": ["激光标尺"],
            "support_vessel": "海洋石油 681",
            "oilfield_name": "流花11-1油田",
        }
        mock_snapshot = {
            "unit_id": "LROV-150-001",
            "status_ref": "LROV-150-001",
            "state_version": 1,
            "updated_at": "2026-08-14T16:00:00+08:00",
            "state": {
                "overall_status": "available",
                "is_online": True,
                "is_busy": False,
                "survival_status": "normal",
                "thruster_status": "abnormal",  # 推进器异常触发 C022
                "updated_at": "2026-08-14T16:00:00+08:00",
            }
        }

        with patch.object(self.kb, "get_unit_state_snapshot", return_value=mock_snapshot), \
             patch.object(self.kb.state_info, "get_unit_state_snapshot", return_value=mock_snapshot):
            res = self.dm.validator.validate_task(task_state, purpose="preview")
            violation_ids = [v.constraint_id for v in res.violations]
            self.assertIn("C022", violation_ids, "发布前 preview 必须检查出 C022 推进器状态异常软警告")

    def test_pre_publish_blocks_hard_on_offline_robot_state(self):
        """当机器人总体状态为离线时，发布前 preview/publish 必须阻断为 hard 约束。"""
        task_state = {
            "task_type_key": "pipeline_inspection",
            "task_type": "管道巡检",
            "start_time": "2026-08-14 17:30:00",
            "end_time": "2026-08-14 19:00:00",
            "water_depth": 500.0,
            "cable_type": "海底油气管道",
            "equipment_class": "observation_rov",
            "equipment_family": "light_work_class_rov",
            "equipment_type": "轻型工作级深海机器人 150HP",
            "equipment_unit_id": "LROV-150-001",
            "payload": ["激光标尺"],
            "support_vessel": "海洋石油 681",
        }
        mock_snapshot = {
            "unit_id": "LROV-150-001",
            "status_ref": "LROV-150-001",
            "state_version": 1,
            "updated_at": "2026-08-14T16:00:00+08:00",
            "state": {
                "overall_status": "offline",
                "is_online": False,
                "updated_at": "2026-08-14T16:00:00+08:00",
            }
        }

        with patch.object(self.kb, "get_unit_state_snapshot", return_value=mock_snapshot), \
             patch.object(self.kb.state_info, "get_unit_state_snapshot", return_value=mock_snapshot):
            res = self.dm.validator.validate_task(task_state, purpose="publish")
            self.assertEqual(res.overall_status, "blocked_hard")
            violation_ids = [v.constraint_id for v in res.violations]
            self.assertIn("C020", violation_ids)

    def test_future_task_does_not_block_on_current_robot_overall_status(self):
        """未来排期任务不应被 state.yaml 中当前单机忙碌/离线状态触发 C020 阻断。"""
        task_state = {
            "task_type_key": "pipeline_inspection",
            "task_type": "管道巡检",
            "start_time": "2026-08-18 06:00:00",
            "end_time": "2026-08-18 08:00:00",
            "water_depth": 500.0,
            "cable_type": "海底油气管道",
            "equipment_class": "observation_rov",
            "equipment_family": "light_work_class_rov",
            "equipment_type": "轻型工作级深海机器人 150HP",
            "equipment_unit_id": "LROV-150-001",
            "payload": ["激光标尺"],
            "support_vessel": "海洋石油 681",
        }
        mock_snapshot = {
            "unit_id": "LROV-150-001",
            "status_ref": "LROV-150-001",
            "state_version": 1,
            "updated_at": "2026-08-14T16:50:00+08:00",
            "state": {
                "overall_status": "unavailable",
                "is_busy": True,
                "updated_at": "2026-08-14T16:50:00+08:00",
            }
        }

        with patch.object(self.kb, "get_unit_state_snapshot", return_value=mock_snapshot), \
             patch.object(self.kb.state_info, "get_unit_state_snapshot", return_value=mock_snapshot):
            res = self.dm.validator.validate_task(task_state, purpose="publish")

        self.assertEqual(res.overall_status, "pending_runtime_validation")
        violation_ids = [v.constraint_id for v in res.violations]
        self.assertIn("C032", violation_ids)
        self.assertNotIn("C020", violation_ids)

    def test_pre_publish_checks_environmental_turbidity_and_velocity(self):
        """发布前核验必须过一遍海况环境（流速 C015、浑浊度 C013）。"""
        task_state = {
            "task_type_key": "pipeline_inspection",
            "task_type": "管道巡检",
            "start_time": "2026-08-14 17:30:00",
            "end_time": "2026-08-14 19:00:00",
            "water_depth": 500.0,
            "cable_type": "海底油气管道",
            "equipment_type": "轻型工作级深海机器人 150HP",
            "equipment_unit_id": "LROV-150-001",
        }
        mock_snapshot = {
            "unit_id": "LROV-150-001",
            "status_ref": "LROV-150-001",
            "state_version": 1,
            "updated_at": "2026-08-14T16:00:00+08:00",
            "state": {
                "overall_status": "available",
                "is_online": True,
                "water_current_velocity": 0.65,  # 触发 C015
                "water_turbidity": 6.5,           # 触发 C013
                "updated_at": "2026-08-14T16:00:00+08:00",
            }
        }

        with patch.object(self.kb, "get_unit_state_snapshot", return_value=mock_snapshot), \
             patch.object(self.kb.state_info, "get_unit_state_snapshot", return_value=mock_snapshot):
            res = self.dm.validator.validate_task(task_state, purpose="preview")
            violation_ids = [v.constraint_id for v in res.violations]
            self.assertIn("C015", violation_ids)
            self.assertIn("C013", violation_ids)

    def test_dialogue_process_keeps_soft_warnings_in_sidebar_only(self):
        """软约束触发后保留在右侧看板，左侧回复不重复提醒。"""
        from src.intent_router import IntentRouteResult
        task_state = {
            "task_type_key": "pipeline_inspection",
            "task_type": "管缆巡检",
            "start_time": "2026-08-14T17:30:00",
            "end_time": "2026-08-14T19:00:00",
            "start_point": {"lat": 19.8, "lon": 113.0},
            "end_point": {"lat": 20.0, "lon": 113.0},
            "cable_type": "海底油气管道",
            "equipment_class": "observation_rov",
            "equipment_family": "light_work_class_rov",
            "equipment_type": "轻型工作级深海机器人 150HP",
            "equipment_unit_id": "LROV-150-001",
            "payload": ["激光标尺"],
            "support_vessel": "海洋石油 681",
            "oilfield_name": "流花11-1油田",
        }
        schema = self.dm.builder.get_schema("pipeline_inspection", "normal")
        self.dm.slot_store.init_task_slots(schema)
        schema_map = {f["key"]: f for f in schema}
        for k, v in task_state.items():
            from src.slot_store import Slot
            vtype = schema_map.get(k, {}).get("type", "string")
            self.dm.slot_store.slots[k] = Slot(k, value=v, raw_value=v, status="valid", value_type=vtype)
        self.dm.slot_store.slots["task_type_key"] = Slot("task_type_key", value="pipeline_inspection", raw_value="pipeline_inspection", status="valid", value_type="string")
        self.dm.task_state = self.dm.slot_store.get_task_state()

        mock_snapshot = {
            "unit_id": "LROV-150-001",
            "status_ref": "LROV-150-001",
            "state_version": 1,
            "updated_at": "2026-08-14T16:00:00+08:00",
            "state": {
                "overall_status": "available",
                "is_online": True,
                "thruster_status": "abnormal",  # C022
                "updated_at": "2026-08-14T16:00:00+08:00",
            }
        }
        route_res = IntentRouteResult(interaction_type="WRITE", confidence=1.0, reason="write", query_intent=None)

        with patch.object(self.kb, "get_unit_state_snapshot", return_value=mock_snapshot), \
             patch.object(self.kb.state_info, "get_unit_state_snapshot", return_value=mock_snapshot), \
             patch.object(self.dm.intent_router, "route", return_value=route_res), \
             patch.object(self.dm.extractor, "extract_updates", return_value={"slot_candidates": [{"canonical_key": "water_depth", "normalized_value": 500.0, "raw_value": "500米", "status": "valid"}], "unresolved": []}), \
            patch.object(self.dm.llm, "chat", return_value="所有必填字段已收集完成。请确认是否发布该任务？"):
            reply = self.dm.process("水深500米")
            self.assertEqual(self.dm.phase, "blocked_soft")
            self.assertNotIn("C022", reply)
            self.assertNotIn("推进器状态", reply)
            ui_state = build_frontend_ui_state(self.dm)
            soft_ids = {
                warning.get("constraint_id")
                for warning in ui_state["constraint_state"]["soft_warnings"]
            }
            self.assertIn("C022", soft_ids)

    def test_dialogue_process_surfaces_hard_constraints_in_reply_and_sidebar(self):
        """硬约束触发后左侧回复和右侧看板都必须显示阻塞详情。"""
        from src.intent_router import IntentRouteResult
        task_state = {
            "task_type_key": "pipeline_inspection",
            "task_type": "管缆巡检",
            "start_time": "2026-08-14T17:30:00",
            "end_time": "2026-08-14T19:00:00",
            "start_point": {"lat": 19.8, "lon": 113.0},
            "end_point": {"lat": 20.0, "lon": 113.0},
            "cable_type": "海底油气管道",
            "equipment_class": "observation_rov",
            "equipment_family": "light_work_class_rov",
            "equipment_type": "轻型工作级深海机器人 150HP",
            "equipment_unit_id": "LROV-150-001",
            "payload": ["激光标尺"],
            "support_vessel": "海洋石油 681",
            "oilfield_name": "流花11-1油田",
        }
        schema = self.dm.builder.get_schema("pipeline_inspection", "normal")
        self.dm.slot_store.init_task_slots(schema)
        schema_map = {f["key"]: f for f in schema}
        for k, v in task_state.items():
            from src.slot_store import Slot
            vtype = schema_map.get(k, {}).get("type", "string")
            self.dm.slot_store.slots[k] = Slot(k, value=v, raw_value=v, status="valid", value_type=vtype)
        self.dm.slot_store.slots["task_type_key"] = Slot("task_type_key", value="pipeline_inspection", raw_value="pipeline_inspection", status="valid", value_type="string")
        self.dm.task_state = self.dm.slot_store.get_task_state()

        mock_snapshot = {
            "unit_id": "LROV-150-001",
            "status_ref": "LROV-150-001",
            "state_version": 1,
            "updated_at": "2026-08-14T16:00:00+08:00",
            "state": {
                "overall_status": "offline",
                "is_online": False,
                "updated_at": "2026-08-14T16:00:00+08:00",
            }
        }
        route_res = IntentRouteResult(interaction_type="WRITE", confidence=1.0, reason="write", query_intent=None)

        with patch.object(self.kb, "get_unit_state_snapshot", return_value=mock_snapshot), \
             patch.object(self.kb.state_info, "get_unit_state_snapshot", return_value=mock_snapshot), \
             patch.object(self.dm.intent_router, "route", return_value=route_res), \
             patch.object(self.dm.extractor, "extract_updates", return_value={"slot_candidates": [{"canonical_key": "water_depth", "normalized_value": 500.0, "raw_value": "500米", "status": "valid"}], "unresolved": []}), \
             patch.object(self.dm.llm, "chat", return_value="所有必填字段已收集完成。请确认是否发布该任务？"):
            reply = self.dm.process("水深500米")
            self.assertEqual(self.dm.phase, "blocked_hard")
            self.assertIn("C020", reply)
            ui_state = build_frontend_ui_state(self.dm)
            hard_ids = {
                violation.get("constraint_id")
                for violation in ui_state["constraint_state"]["hard_violations"]
            }
            self.assertIn("C020", hard_ids)

    def test_future_task_requires_runtime_notice_ack_before_publish(self):
        """未来排期任务跳过 state.yaml 运行态检查，但 C032 仍需用户确认忽略。"""
        import uuid
        from src.intent_router import IntentRouteResult
        task_state = {
            "internal_id": str(uuid.uuid4()),
            "intent_id": "TI202608140001",
            "task_type_key": "pipeline_inspection",
            "task_type": "管道巡检",
            "start_time": "2026-08-18 06:00:00",
            "end_time": "2026-08-18 08:00:00",
            "start_point": {"lat": 19.8, "lon": 113.0},
            "end_point": {"lat": 20.0, "lon": 113.0},
            "water_depth": 500.0,
            "cable_type": "海底油气管道",
            "equipment_class": "observation_rov",
            "equipment_family": "light_work_class_rov",
            "equipment_type": "轻型工作级深海机器人 150HP",
            "equipment_unit_id": "LROV-150-001",
            "payload": ["激光标尺"],
            "support_vessel": "海洋石油 681",
            "oilfield_name": "流花11-1油田",
        }
        schema = self.dm.builder.get_schema("pipeline_inspection", "normal")
        self.dm.slot_store.init_task_slots(schema)
        for k, v in task_state.items():
            from src.slot_store import Slot
            self.dm.slot_store.slots[k] = Slot(k, value=v, status="valid")
        self.dm.task_state = self.dm.slot_store.get_task_state()

        mock_snapshot = {
            "unit_id": "LROV-150-001",
            "status_ref": "LROV-150-001",
            "state_version": 1,
            "updated_at": "2026-08-14T16:00:00+08:00",
            "state": {
                "overall_status": "available",
                "is_online": True,
                "thruster_status": "abnormal",  # 推进器异常 C022 软警告
                "updated_at": "2026-08-14T16:00:00+08:00",
            }
        }

        with patch.object(self.kb, "get_unit_state_snapshot", return_value=mock_snapshot), \
             patch.object(self.kb.state_info, "get_unit_state_snapshot", return_value=mock_snapshot), \
             patch.object(self.kb.state_info, "guard_unit_state_version"):
            self.dm._run_constraint_check({"start_time"}, purpose="preview")
            self.assertEqual(self.dm.phase, "blocked_soft")
            blocking_ids = {v.constraint_id for v in self.dm._blocking_violations}
            self.assertIn("C032", blocking_ids)
            self.assertNotIn("C022", blocking_ids)

            ignore_reply = self.dm.process("忽略警告")
            self.assertEqual(self.dm.phase, "confirming", f"忽略 C032 后应进入 confirming，实际回复: {ignore_reply}")
            self.assertIn("已记录您对当前软警告的确认", ignore_reply)

            confirm_reply = self.dm.process("确认发布")
            self.assertEqual(self.dm.phase, "done", f"确认发布后应进入 done，实际回复: {confirm_reply}")
            self.assertIn("已加入计划池", confirm_reply)

    def test_immediate_task_ignore_soft_warning_and_publish_success(self):
        """即时任务在设备异常触发软警告后，用户忽略软警告能够成功发布。"""
        import uuid
        task_state = {
            "internal_id": str(uuid.uuid4()),
            "intent_id": "TI202608140002",
            "task_type_key": "pipeline_inspection",
            "task_type": "管道巡检",
            "start_time": "2026-08-14 17:05:00",
            "end_time": "2026-08-14 19:00:00",
            "start_point": {"lat": 19.8, "lon": 113.0},
            "end_point": {"lat": 20.0, "lon": 113.0},
            "water_depth": 500.0,
            "cable_type": "海底油气管道",
            "equipment_class": "observation_rov",
            "equipment_family": "light_work_class_rov",
            "equipment_type": "轻型工作级深海机器人 150HP",
            "equipment_unit_id": "LROV-150-001",
            "payload": ["激光标尺"],
            "support_vessel": "海洋石油 681",
            "oilfield_name": "流花11-1油田",
        }
        schema = self.dm.builder.get_schema("pipeline_inspection", "normal")
        self.dm.slot_store.init_task_slots(schema)
        for k, v in task_state.items():
            from src.slot_store import Slot
            self.dm.slot_store.slots[k] = Slot(k, value=v, status="valid")
        self.dm.task_state = self.dm.slot_store.get_task_state()

        mock_snapshot = {
            "unit_id": "LROV-150-001",
            "status_ref": "LROV-150-001",
            "state_version": 1,
            "updated_at": "2026-08-14T16:55:00+08:00",
            "state": {
                "overall_status": "available",
                "is_online": True,
                "thruster_status": "abnormal",  # C022 软警告
                "updated_at": "2026-08-14T16:55:00+08:00",
            }
        }

        with patch.object(self.kb, "get_unit_state_snapshot", return_value=mock_snapshot), \
             patch.object(self.kb.state_info, "get_unit_state_snapshot", return_value=mock_snapshot), \
             patch.object(self.kb.state_info, "check_runtime_availability", return_value={"available": True}), \
             patch.object(self.kb.state_info, "guard_unit_state_version"):
            # 1. 触发推进器异常 C022 软警告
            self.dm._run_constraint_check({"equipment_unit_id"}, purpose="preview")
            self.assertEqual(self.dm.phase, "blocked_soft")

            # 2. 用户执行“忽略警告”
            ignore_reply = self.dm.process("忽略警告")
            self.assertEqual(self.dm.phase, "confirming")

            # 3. 用户确认发布
            confirm_reply = self.dm.process("确认发布")
            self.assertEqual(self.dm.phase, "done", f"确认发布应成功进入 done，实际回复: {confirm_reply}")
            self.assertIn("任务已生成并下发", confirm_reply)


if __name__ == "__main__":
    unittest.main()
