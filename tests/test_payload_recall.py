"""Unit tests for payload card re-call / re-selection on user modification request."""

import unittest
from unittest.mock import MagicMock
from src.dialogue_manager import DialogueManager
from src.knowledge_retriever import KnowledgeBase
from src.slots.slot_store import Slot
from src.session.ui_state_builder import build_frontend_ui_state


def seed_valid_task_with_payload(dm: DialogueManager, kb: KnowledgeBase):
    """Seed DialogueManager with an active task that has a valid payload slot."""
    dm._switch_dialogue_mode("task_collection")
    dm._transition_phase("confirming")
    dm.slot_store.slots["task_type_key"] = Slot(
        slot_name="task_type_key",
        value="pipeline_inspection",
        value_type="string",
        status="valid",
    )
    dm.slot_store.slots["equipment_type"] = Slot(
        slot_name="equipment_type",
        value="观察级深海机器人 75HP",
        value_type="string",
        status="valid",
    )
    dm.slot_store.slots["payload"] = Slot(
        slot_name="payload",
        value=["高清水下摄像机"],
        value_type="list",
        status="valid",
        source="user",
    )
    dm.task_state = {
        "task_type_key": "pipeline_inspection",
        "task_type": "管缆巡检",
        "equipment_type": "观察级深海机器人 75HP",
        "payload": ["高清水下摄像机"],
    }


class TestPayloadCardRecall(unittest.TestCase):
    def setUp(self):
        self.kb = KnowledgeBase()
        self.dm = DialogueManager(MagicMock(), self.kb)

    def test_user_payload_modification_preserves_committed_slot_and_opens_editor(self):
        """Opening the editor must not erase the configured task payload."""
        seed_valid_task_with_payload(self.dm, self.kb)
        before = self.dm.slot_store.export_snapshot()

        reply = self.dm.process("修改载荷")

        self.assertIn("重新调出载荷配置卡片", reply)
        payload_slot = self.dm.slot_store.slots["payload"]
        self.assertEqual(payload_slot.status, "valid")
        self.assertEqual(payload_slot.value, ["高清水下摄像机"])
        self.assertEqual(self.dm.slot_store.export_snapshot(), before)
        self.assertEqual(self.dm.phase, "collecting")

        ui_state = build_frontend_ui_state(self.dm)
        self.assertIn("slots", ui_state)
        payload_ui = next((s for s in ui_state["slots"] if s["key"] == "payload"), None)
        self.assertIsNotNone(payload_ui)
        self.assertEqual(payload_ui["status"], "valid")
        self.assertEqual(payload_ui["value"], ["高清水下摄像机"])
        self.assertEqual(ui_state["editing_slot"], "payload")

    def test_reselect_payload_synonym_triggers_recall(self):
        """Synonyms such as '重新选择载荷' or '修改payload' also trigger payload card recall."""
        seed_valid_task_with_payload(self.dm, self.kb)
        reply = self.dm.process("重新选择载荷")
        self.assertIn("重新调出载荷配置卡片", reply)
        self.assertEqual(self.dm.slot_store.slots["payload"].status, "valid")
        self.assertEqual(build_frontend_ui_state(self.dm)["editing_slot"], "payload")

    def test_targeted_cancellation_does_not_trigger_recall(self):
        """Input containing negation like '取消载荷修改' should not be misidentified as a card recall request."""
        seed_valid_task_with_payload(self.dm, self.kb)
        self.assertFalse(self.dm._is_payload_modification_request("取消载荷修改"))
        self.assertFalse(self.dm._is_payload_modification_request("放弃修改"))
        self.assertTrue(self.dm._is_payload_modification_request("修改载荷"))
        self.assertTrue(self.dm._is_payload_modification_request("重新选择载荷"))

    def test_payload_modification_rejected_if_phase_done(self):
        """If task phase is done (archived/published), in-place modification is rejected."""
        seed_valid_task_with_payload(self.dm, self.kb)
        self.dm.phase = "done"

        reply = self.dm.process("修改载荷")
        self.assertIn("无法就地修改参数", reply)


if __name__ == "__main__":
    unittest.main()
