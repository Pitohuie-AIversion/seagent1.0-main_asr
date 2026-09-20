"""
tests/test_execution_control.py

测试 ExecutionControlHandler 紧急干预与已发布任务控制指令生命周期处理逻辑。
"""

import unittest
from unittest.mock import patch

from src.dialogue_manager import DialogueManager
from src.handlers.execution_control import ExecutionControlHandler
from src.session.intent_router import IntentRouteResult
from src.session.session_state import StateContractError


class TestExecutionControlHandler(unittest.TestCase):
    def setUp(self):
        self.dm = DialogueManager()
        self.handler = self.dm.execution_control_handler

    def test_attachment(self):
        """验证 ExecutionControlHandler 挂载与委托正常。"""
        self.assertIsInstance(self.handler, ExecutionControlHandler)
        self.assertIs(self.handler.manager, self.dm)

    def test_set_execution_control_state_normal(self):
        """测试正常更新执行控制状态。"""
        req = {"action": "stop", "status": "requested", "target_intent_id": "TI202609010001"}
        self.handler.set_execution_control_state("stop_requested", req)
        self.assertEqual(self.dm.control_state, "stop_requested")
        self.assertEqual(self.dm.last_control_request["action"], "stop")

    @patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=True)
    def test_set_execution_control_state_v2_contract_fail_closed(self, mock_v2):
        """在 v2 契约下，非 done 阶段设置非 idle 控制状态必须 fail-closed。"""
        self.dm.phase = "collecting"
        req = {"action": "stop", "status": "requested"}
        with self.assertRaises(StateContractError):
            self.handler.set_execution_control_state("stop_requested", req)

    def test_handle_emergency_intervention_when_done(self):
        """在已发布 (done) 状态下接收紧急停止指令。"""
        self.dm.phase = "done"
        self.dm.task_state = {
            "intent_id": "TI202609010001",
            "task_id": "PI-20260901-001",
            "internal_id": "00000000-0000-4000-8000-000000000001",
        }
        route = IntentRouteResult(
            interaction_type="QUERY",
            confidence=0.95,
            reason="user emergency",
            dialogue_mode="emergency_intervention",
            emergency_action="stop",
        )
        reply = self.handler.handle_emergency_intervention("紧急停止作业", route)
        self.assertIn("已识别针对已发布任务的控制指令【停止】", reply)
        self.assertEqual(self.dm.control_state, "stop_requested")
        self.assertIsNotNone(self.dm.last_control_request)
        self.assertEqual(self.dm.last_control_request["action"], "stop")
        self.assertEqual(self.dm.last_control_request["target_intent_id"], "TI202609010001")

    @patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=True)
    def test_handle_emergency_intervention_when_done_invalid_intent_id(self, mock_v2):
        """在 v2 契约下，已发布状态如果缺少合法 intent_id 触发控制必须 fail-closed。"""
        self.dm.phase = "done"
        self.dm.task_state = {"intent_id": "INVALID_ID"}
        route = IntentRouteResult(
            interaction_type="QUERY",
            confidence=0.95,
            reason="user emergency",
            dialogue_mode="emergency_intervention",
            emergency_action="pause",
        )
        with self.assertRaises(StateContractError):
            self.handler.handle_emergency_intervention("暂停任务", route)

    def test_handle_emergency_intervention_draft_cancel(self):
        """在草稿阶段接收取消指令，清空草稿并流转至 rejected。"""
        self.dm.phase = "collecting"
        self.dm.task_state = {"task_type_key": "pipeline_inspection", "water_depth": 300.0}
        route = IntentRouteResult(
            interaction_type="QUERY",
            confidence=0.95,
            reason="user emergency",
            dialogue_mode="emergency_intervention",
            emergency_action="cancel",
        )
        reply = self.handler.handle_emergency_intervention("取消当前任务", route)
        self.assertIn("任务已取消", reply)
        self.assertEqual(self.dm.phase, "rejected")
        self.assertEqual(self.dm.control_state, "idle")
        self.assertIsNone(self.dm.last_control_request)

    def test_handle_emergency_intervention_draft_stop_warns(self):
        """在草稿阶段接收 stop 指令，提示尚未发布并保留草稿。"""
        self.dm.phase = "collecting"
        self.dm.task_state = {"task_type_key": "pipeline_inspection"}
        route = IntentRouteResult(
            interaction_type="QUERY",
            confidence=0.95,
            reason="user emergency",
            dialogue_mode="emergency_intervention",
            emergency_action="stop",
        )
        reply = self.handler.handle_emergency_intervention("停止机器人", route)
        self.assertIn("当前任务尚未发布", reply)
        self.assertIn("任务草稿已保留", reply)
        self.assertEqual(self.dm.phase, "collecting")

    def test_handle_emergency_intervention_no_active_task(self):
        """无活动任务时接收控制指令，返回相应提示。"""
        self.dm.phase = "collecting"
        self.dm.task_state = {}
        self.dm.slot_store.slots.clear()
        route = IntentRouteResult(
            interaction_type="QUERY",
            confidence=0.95,
            reason="user emergency",
            dialogue_mode="emergency_intervention",
            emergency_action="cancel",
        )
        reply = self.handler.handle_emergency_intervention("取消", route)
        self.assertIn("当前没有活动任务", reply)


if __name__ == "__main__":
    unittest.main()
