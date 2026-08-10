"""
tests/test_task_guidance_final_confirmation.py

SEAgent G6 Closeout — Final Confirmation Semantics Fix 专项测试。
验证：
1. 用户必填字段全部完成 -> phase = confirming，不自动发布；
2. confirming + "好的" -> phase 仍为 confirming，不调用 publish；
3. confirming + "确认" -> phase 仍为 confirming，不调用 publish；
4. confirming + "确认发布" -> 进入正式 publish 处理并下发任务；
5. blocked_soft + "确认发布" -> 不生成 soft acknowledgement，保持 blocked_soft；
6. blocked_soft + "忽略警告" -> 生成 ValidationAcknowledgement；
7. 忽略软警告后，missing 不包含 auto/fixed 字段（如 task_id, intent_id, internal_id）；
8. confirming 阶段的 responder prompt 明确提示“确认发布”及“任务尚未发布”。
"""

import unittest
from unittest.mock import patch, MagicMock

from src.dialogue_manager import DialogueManager
from src.knowledge_retriever import KnowledgeBase
from src.llm_client import LLMClient
from src.prompts import build_responder_messages, _CONSTRAINT_INSTRUCTIONS


class TestTaskGuidanceFinalConfirmation(unittest.TestCase):
    def setUp(self):
        self.llm = LLMClient(None, None)
        self.kb = KnowledgeBase()
        self.dm = DialogueManager(self.llm, self.kb, session_id="test_sess_confirm")

    def _fill_all_required_slots(self, dm):
        schema = dm.builder.get_schema("pipeline_inspection", dm.mode)
        dm.slot_store.init_task_slots(schema)

        new_slots = dm.slot_store.clone_slots()
        new_slots["task_type"].value = "管缆巡检"
        new_slots["task_type"].status = "valid"
        new_slots["water_depth"].value = 300.0
        new_slots["water_depth"].status = "valid"
        new_slots["equipment_class"].value = "observation_rov"
        new_slots["equipment_class"].status = "valid"
        new_slots["equipment_family"].value = "观察级深海机器人"
        new_slots["equipment_family"].status = "valid"
        new_slots["equipment_type"].value = "观察级深海机器人"
        new_slots["equipment_type"].status = "valid"
        new_slots["equipment_unit_id"].value = "OBSROV--001"
        new_slots["equipment_unit_id"].status = "valid"
        new_slots["cable_type"].value = "海底油气管道"
        new_slots["cable_type"].status = "valid"
        new_slots["support_vessel"].value = "海洋石油681"
        new_slots["support_vessel"].status = "valid"
        new_slots["payload"].value = ["高清水下摄像机"]
        new_slots["payload"].status = "valid"
        new_slots["start_time"].value = "2026-08-11T10:00:00"
        new_slots["start_time"].status = "valid"
        new_slots["end_time"].value = "2026-08-11T18:00:00"
        new_slots["end_time"].status = "valid"
        new_slots["start_point"].value = {"lat": 20.0, "lon": 110.0}
        new_slots["start_point"].status = "valid"
        new_slots["end_point"].value = {"lat": 20.1, "lon": 110.1}
        new_slots["end_point"].status = "valid"
        dm.slot_store.commit_transaction(new_slots, [])
        dm.task_state = dm.slot_store.get_task_state()
        dm.task_state["task_type_key"] = "pipeline_inspection"

    def test_01_all_required_user_slots_complete_enters_confirming_without_auto_publish(self):
        """1. 用户必填字段全部完成 -> phase = confirming，不自动发布"""
        self._fill_all_required_slots(self.dm)
        self.dm.phase = "confirming"

        self.assertEqual(self.dm.phase, "confirming")
        self.assertNotEqual(self.dm.phase, "done")

    def test_02_confirming_phase_with_haode_does_not_publish(self):
        """2. confirming + “好的” -> phase 仍为 confirming，不调用 publish"""
        self.dm.phase = "confirming"
        with patch.object(self.dm, "_handle_final_publish_confirmation", side_effect=AssertionError("Publish should not be called")):
            reply = self.dm.process("好的")
        self.assertEqual(self.dm.phase, "confirming")
        self.assertIn("当前任务尚未发布", reply)
        self.assertIn("确认发布", reply)

    def test_03_confirming_phase_with_queren_does_not_publish(self):
        """3. confirming + “确认” -> phase 仍为 confirming，不调用 publish"""
        self.dm.phase = "confirming"
        with patch.object(self.dm, "_handle_final_publish_confirmation", side_effect=AssertionError("Publish should not be called")):
            reply = self.dm.process("确认")
        self.assertEqual(self.dm.phase, "confirming")
        self.assertIn("当前任务尚未发布", reply)

    def test_04_confirming_phase_with_queren_fabu_triggers_publish(self):
        """4. confirming + “确认发布” -> 进入正式 publish handler"""
        self.dm.phase = "confirming"
        with patch.object(self.dm, "_handle_final_publish_confirmation", return_value="任务下发成功") as mock_pub:
            reply = self.dm.process("确认发布")
        mock_pub.assert_called_once()
        self.assertEqual(reply, "任务下发成功")

    def test_05_blocked_soft_phase_with_queren_fabu_does_not_ignore_warning(self):
        """5. blocked_soft + “确认发布” -> 不生成 soft acknowledgement，保持 blocked_soft"""
        self.dm.phase = "blocked_soft"
        reply = self.dm.process("确认发布")
        self.assertEqual(self.dm.phase, "blocked_soft")
        self.assertIn("当前仍存在软警告", reply)
        self.assertIn("忽略警告", reply)

    def test_06_blocked_soft_phase_with_hulve_jianguo_creates_acknowledgement(self):
        """6. blocked_soft + “忽略警告” -> 触发 soft warning 确认逻辑"""
        self.dm.phase = "blocked_soft"
        with patch.object(self.dm, "_handle_soft_warning_confirmation", return_value="已忽略警告") as mock_ack:
            reply = self.dm.process("忽略警告")
        mock_ack.assert_called_once()
        self.assertEqual(reply, "已忽略警告")

    def test_07_soft_ack_missing_slots_never_contain_auto_or_fixed_fields(self):
        """7. blocked_soft -> 忽略警告 -> missing 为空 -> confirming -> missing 中无 task_id / intent_id / internal_id"""
        self._fill_all_required_slots(self.dm)
        res = self.dm._refresh_validation()
        self.dm.phase = "blocked_soft"
        self.dm._blocking_violations = res.violations
        self.dm._handle_soft_warning_confirmation("忽略警告", "req_test_07")

        self.assertEqual(self.dm.phase, "confirming")
        missing_keys = [m["key"] if isinstance(m, dict) else str(m) for m in self.dm._last_missing]
        self.assertNotIn("task_id", missing_keys)
        self.assertNotIn("intent_id", missing_keys)
        self.assertNotIn("internal_id", missing_keys)
        self.assertEqual(len(missing_keys), 0)

    def test_08_confirming_responder_prompt_contains_explicit_publish_guidance(self):
        """8. confirming 阶段的 responder prompt 包含‘确认发布’与‘任务尚未发布’指引"""
        messages = build_responder_messages(
            task_state={"task_type_key": "pipeline_inspection", "task_type": "管缆巡检"},
            built_json={"water_depth": 300.0},
            missing_fields=[],
            mode="normal",
            phase="confirming",
            knowledge_context="",
            constraint_context={"type": "none", "violations": []},
            conversation_history=[],
            latest_user_message="好的",
            ROV2type={},
            support_task=["管缆巡检"],
        )
        system_prompt = messages[0]["content"]
        self.assertIn("确认发布", system_prompt)
        self.assertIn("当前任务尚未发布", system_prompt)


if __name__ == "__main__":
    unittest.main()
