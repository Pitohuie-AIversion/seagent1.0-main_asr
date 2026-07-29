"""
tests/test_robot_state_consistency.py — 机器人实时状态一致性闭环测试

验证流程：
1. 调用 StateInfoStore.set_status / KnowledgeBase.state_info.set_status 设置：
   robot: 金牛座一号机 (CRAWLER-1600-001)
   depth: 350
2. 询问: "金牛座一号机当前深度？"
3. 必须返回 350m / 350米，且 SlotStore 状态保持不变。
"""

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.dialogue_manager import DialogueManager
from src.knowledge_retriever import KnowledgeBase
from src.intent_router import IntentRouter


class FakeLLMForRobotState:
    def route_mock(self, user_message: str):
        if "深度" in user_message or "状态" in user_message:
            return {
                "interaction_type": "QUERY",
                "query_intent": "DEVICE_STATUS",
                "confidence": 0.98,
                "reason": "用户正在询问设备实时状态"
            }
        return {
            "interaction_type": "QUERY",
            "query_intent": "GENERAL_CHAT",
            "confidence": 0.90,
            "reason": "普通查询"
        }

    def classify_interaction(self, messages, max_tokens=260):
        last_msg = messages[-1]["content"]
        if "【最新用户输入】:" in last_msg:
            user_msg = last_msg.split("【最新用户输入】:")[1].strip().strip('"')
        else:
            user_msg = last_msg
        return self.route_mock(user_msg)

    def extract_json(self, messages, max_tokens=800):
        return {"slot_candidates": [], "unresolved": []}

    def chat(self, messages, **kwargs):
        # build_status_responder_messages 会把 status_evidence 填入 system prompt
        sys_content = messages[0]["content"]
        if "350" in sys_content or "350m" in sys_content:
            return "设备【金牛座一号机】当前深度为 350m。"
        return "设备【金牛座一号机】当前深度信息如下。"

    def filter_reply(self, reply):
        return reply


class RobotStateConsistencyTest(unittest.TestCase):
    def setUp(self):
        self.kb = KnowledgeBase()
        self._state_temp_dir = tempfile.TemporaryDirectory()
        self.kb.state_info.state_file = Path(self._state_temp_dir.name) / "state.yaml"
        self.llm = FakeLLMForRobotState()
        self.dm = DialogueManager(self.llm, self.kb)

    def tearDown(self):
        self._state_temp_dir.cleanup()

    def test_robot_state_update_and_query_closed_loop(self):
        # 1. 更新机器人状态
        target_robot = "CRAWLER-1600-001"  # 金牛座一号机 status_ref
        self.kb.state_info.set_status(target_robot, {
            "depth": 350,
            "overall_status": "available",
            "current_velocity": 0.45,
            "update_timestamp": None
        })

        # 2. 查询设备当前深度
        user_msg = "金牛座一号机当前深度？"
        ver_before = self.dm.slot_store.version
        reply = self.dm.process(user_msg)

        # 3. 校验返回结果包含最新 350m 状态
        self.assertIn("350", reply)

        # 4. 校验 QUERY 操作满足 SlotStore 状态不变性
        self.assertEqual(self.dm.slot_store.version, ver_before)

    def test_robot_state_by_display_name_lookup(self):
        # 同样支持按中文显示名称设置与查询
        self.kb.state_info.set_status("履带式海底重载作业机器人1600HP-001", {
            "depth": 350,
            "overall_status": "available",
            "update_timestamp": None
        })
        state_dict = self.kb.get_robot_state_dict("金牛座一号机")
        self.assertEqual(state_dict.get("depth"), 350)


if __name__ == "__main__":
    unittest.main()
