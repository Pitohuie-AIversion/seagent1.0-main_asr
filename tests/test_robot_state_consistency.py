"""机器人实时状态查询必须使用真实证据且保持任务状态只读。"""

import copy
import tempfile
import unittest
from pathlib import Path

from src.dialogue_manager import DialogueManager
from src.knowledge_retriever import KnowledgeBase
from tests.interaction_plan_support import ScriptedLLM, make_plan


class RobotStateConsistencyTest(unittest.TestCase):
    def setUp(self):
        self.kb = KnowledgeBase()
        self._state_temp_dir = tempfile.TemporaryDirectory()
        self.kb.state_info.state_file = Path(self._state_temp_dir.name) / "state.yaml"
        self.llm = ScriptedLLM(
            default_reply="设备【金牛座一号机】当前深度为 350m。"
        )
        self.dm = DialogueManager(self.llm, self.kb)

    def tearDown(self):
        self._state_temp_dir.cleanup()

    def test_robot_state_update_and_query_closed_loop(self):
        """DEVICE_STATUS READ 使用实时证据且不修改完整 SlotStore 快照。"""
        self.kb.state_info.set_status(
            "CRAWLER-1600-001",
            {
                "depth": 350,
                "overall_status": "available",
                "current_velocity": 0.45,
                "update_timestamp": None,
            },
        )
        self.llm.queue_plan(
            make_plan(
                "READ",
                query_intent="DEVICE_STATUS",
                subject_type="device",
                subject_text="金牛座一号机",
                relation="status",
                source_policy="realtime_state",
            )
        )

        before_version = self.dm.slot_store.version
        before_snapshot = copy.deepcopy(self.dm.slot_store.export_snapshot())
        before_task_state = copy.deepcopy(self.dm.task_state)
        before_built_json = copy.deepcopy(self.dm._last_built_json)
        before_missing = copy.deepcopy(self.dm._last_missing)
        before_phase = self.dm.phase

        reply = self.dm.process(
            "金牛座一号机当前深度？",
            request_id="req_robot_status",
        )

        self.assertIn("350", reply)
        self.assertEqual(len(self.llm.classify_calls), 1)
        self.assertEqual(self.llm.extract_calls, [])
        self.assertEqual(len(self.llm.chat_calls), 1)

        responder_evidence = "\n".join(
            str(message.get("content", ""))
            for call in self.llm.chat_calls
            for message in call
        )
        self.assertIn('"state_data"', responder_evidence)
        self.assertIn('"depth": 350', responder_evidence)

        self.assertEqual(self.dm.slot_store.version, before_version)
        self.assertEqual(self.dm.slot_store.export_snapshot(), before_snapshot)
        self.assertEqual(self.dm.task_state, before_task_state)
        self.assertEqual(self.dm._last_built_json, before_built_json)
        self.assertEqual(self.dm._last_missing, before_missing)
        self.assertEqual(self.dm.phase, before_phase)

    def test_robot_state_by_display_name_lookup(self):
        """状态存储仍支持设备展示名与真实单元 ID 的映射。"""
        self.kb.state_info.set_status(
            "履带式海底重载作业机器人1600HP-001",
            {
                "depth": 350,
                "overall_status": "available",
                "update_timestamp": None,
            },
        )

        state_dict = self.kb.get_robot_state_dict("金牛座一号机")

        self.assertEqual(state_dict.get("depth"), 350)


if __name__ == "__main__":
    unittest.main()
