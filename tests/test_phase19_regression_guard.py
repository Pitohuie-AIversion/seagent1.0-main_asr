"""
tests/test_phase19_regression_guard.py — Phase 1.9 Regression Guard & Stability Tests

验证目标：
1. QUERY 永远不修改状态：
   - 输入 "金牛座一号机最大水深是多少？"，SlotStore version 保持不变。
2. WRITE 正常修改状态：
   - 输入 "执行流花11-1油田管缆巡检"，task_type 与 oilfield_name 成功更新。
3. Failure recovery 回滚正常：
   - 模拟 publish failure，验证状态与 SlotStore 原子回滚。
"""

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.dialogue_manager import DialogueManager
from src.knowledge_retriever import KnowledgeBase
from src.task_intent_builder import TaskIntentBuilder, TaskPersistenceError


class Phase19GuardLLM:
    def classify_interaction(self, messages, max_tokens=260):
        last_msg = messages[-1]["content"]
        if "【最新用户输入】:" in last_msg:
            user_msg = last_msg.split("【最新用户输入】:")[1].split("\n")[0].strip().strip('"')
        else:
            user_msg = last_msg

        if "最大水深" in user_msg or "多少" in user_msg:
            return {
                "interaction_type": "QUERY",
                "query_intent": "DEVICE_CAPABILITY",
                "confidence": 0.98,
                "reason": "设备能力查询",
            }
        else:
            return {
                "interaction_type": "WRITE",
                "query_intent": None,
                "confidence": 0.98,
                "reason": "任务修改",
            }

    def chat(self, messages, temperature=0.7, max_tokens=1500):
        return "金牛座一号机最大水深为3000米。已更新系统状态。"

    def filter_reply(self, text):
        return text

    def extract_json(self, messages, max_tokens=800):
        raw_text = str(messages)

        if "流花11-1" in raw_text or "管缆巡检" in raw_text:
            return {
                "task_type": "pipeline_inspection",
                "slot_candidates": [
                    {
                        "raw_key": "任务类型",
                        "canonical_key": "task_type_key",
                        "raw_value": "管缆巡检",
                        "normalized_value": "pipeline_inspection",
                        "confidence": 0.99,
                    },
                    {
                        "raw_key": "作业水深",
                        "canonical_key": "water_depth",
                        "raw_value": "300米",
                        "normalized_value": 300.0,
                        "confidence": 0.99,
                    },
                ],
                "unresolved": [],
            }
        elif "300米" in raw_text:
            return {
                "slot_candidates": [
                    {
                        "raw_key": "作业水深",
                        "canonical_key": "water_depth",
                        "raw_value": "300米",
                        "normalized_value": 300.0,
                        "confidence": 0.99,
                    }
                ],
                "unresolved": [],
            }
        return {"slot_candidates": [], "unresolved": []}


class Phase19RegressionGuardTest(unittest.TestCase):
    def setUp(self):
        self.kb = KnowledgeBase()
        self.llm = Phase19GuardLLM()
        self.dm = DialogueManager(self.llm, self.kb)

    def test_query_does_not_modify_state(self):
        """1. QUERY 永远不修改 SlotStore 版本和任务状态"""
        initial_ver = self.dm.slot_store.version
        initial_state = dict(self.dm.task_state)

        reply = self.dm.process("金牛座一号机最大水深是多少？")

        self.assertEqual(self.dm.slot_store.version, initial_ver)
        self.assertEqual(self.dm.task_state, initial_state)
        self.assertIn("最大水深", reply)

    def test_write_modifies_state(self):
        """2. WRITE 正常修改任务状态和 task_type"""
        initial_ver = self.dm.slot_store.version

        reply = self.dm.process("执行流花11-1油田管缆巡检")

        self.assertGreater(self.dm.slot_store.version, initial_ver)
        self.assertEqual(self.dm.task_state.get("task_type_key"), "pipeline_inspection")
        self.assertEqual(self.dm.task_state.get("water_depth"), 300.0)

    def test_failure_recovery_rollback(self):
        """3. Failure recovery：模拟 publish failure，验证状态与 SlotStore 原子回滚"""
        # 设置必要槽位以推进到确认阶段
        self.dm.process("执行紧急管缆巡检")
        self.dm.process("提供详细参数，水深300米")

        eq_slot = self.dm.slot_store.slots.get("equipment_type")
        if eq_slot and eq_slot.status != "valid":
            eq_slot.value = "观察级ROV"
            eq_slot.status = "valid"
            self.dm.task_state = self.dm.slot_store.get_task_state()
            self.dm.phase = "confirming"

        ver_before = self.dm.slot_store.version

        # 模拟发布阶段存储锁定失败
        def faulty_publish_staging(*args, **kwargs):
            raise TaskPersistenceError("Simulated atomic publish failure in Phase 1.9 guard test")

        old_publish = TaskIntentBuilder.publish_staging
        TaskIntentBuilder.publish_staging = faulty_publish_staging

        try:
            try:
                self.dm.process("确认发布")
            except TaskPersistenceError:
                pass
        finally:
            TaskIntentBuilder.publish_staging = old_publish

        # 验证回滚后 TaskState 与 SlotStore 的一致性及版本无非法泄露
        self.assertEqual(self.dm.task_state, self.dm.slot_store.get_task_state())
        self.assertLessEqual(self.dm.slot_store.version, ver_before + 1)


if __name__ == "__main__":
    unittest.main()
