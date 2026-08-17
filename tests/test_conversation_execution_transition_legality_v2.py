"""tests/test_conversation_execution_transition_legality_v2.py

Unit, integration, and cross-domain audit tests for SEAgent G3.4-B Conversation / Execution Transition Legality.
Audits and verifies full connectivity of dialogue modes, fail-closed contract enforcement,
robot execution control constraints, and cross-domain audit for done phase control requests + task modifications.
"""

import copy
import unittest
from unittest.mock import patch, MagicMock

from src.dialogue_manager import DialogueManager
from src.session_state import (
    StateContractError,
    VALID_DIALOGUE_MODES,
    VALID_CONTROL_STATES,
    session_state_from_legacy_snapshot,
)
from src.slot_store import Slot
from tests.interaction_plan_support import ScriptedLLM, make_plan


class TestConversationExecutionTransitionLegalityV2(unittest.TestCase):

    def setUp(self) -> None:
        self.llm = ScriptedLLM()
        self.dm = DialogueManager(self.llm)

    # --------------------------------------------------------------------------
    # A. Conversation Transition Audit & Contract Tests
    # --------------------------------------------------------------------------

    # 1. All 9 dialogue mode transitions verified with call-chain evidence
    def test_01_conversation_inventory_evidence(self) -> None:
        modes = ["task_collection", "knowledge_qa", "emergency_intervention"]
        for m1 in modes:
            for m2 in modes:
                self.dm.dialogue_mode = m1
                self.dm._switch_dialogue_mode(m2, source="test", reason="inventory_check")
                self.assertEqual(self.dm.dialogue_mode, m2)

    # 2. task_collection -> knowledge_qa via DM process turn
    def test_02_task_collection_to_knowledge_qa(self) -> None:
        self.dm.dialogue_mode = "task_collection"
        self.llm.queue_plan(make_plan("READ", query_intent="GENERAL_CHAT"))
        reply = self.dm.process("今天天气怎么样？")
        self.assertEqual(self.dm.dialogue_mode, "knowledge_qa")
        self.assertTrue(len(reply) > 0)

    # 3. knowledge_qa -> task_collection via DM process turn
    def test_03_knowledge_qa_to_task_collection(self) -> None:
        self.dm.dialogue_mode = "knowledge_qa"
        self.llm.queue_plan(make_plan("WRITE"))
        self.dm.process("帮我创建一个管缆巡检任务")
        self.assertEqual(self.dm.dialogue_mode, "task_collection")

    # 4. task_collection -> emergency_intervention via DM process turn
    def test_04_task_collection_to_emergency_intervention(self) -> None:
        self.dm.dialogue_mode = "task_collection"
        self.dm.phase = "done"
        self.llm.queue_plan(make_plan("CONTROL", emergency_action="stop"))
        self.dm.process("紧急停止当前任务")
        self.assertEqual(self.dm.dialogue_mode, "emergency_intervention")

    # 6. Same mode transition does not duplicate history
    def test_06_same_mode_transition_does_not_append_history(self) -> None:
        self.dm._switch_dialogue_mode("knowledge_qa", source="test1", reason="first_switch")
        hist_len_1 = len(self.dm.mode_transition_history)

        self.dm._switch_dialogue_mode("knowledge_qa", source="test2", reason="second_switch")
        self.assertEqual(len(self.dm.mode_transition_history), hist_len_1)

    # 7. Invalid new mode fails closed when flag=true
    @patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=True)
    def test_07_invalid_new_mode_fails_closed(self, mock_flag) -> None:
        initial_mode = self.dm.dialogue_mode
        with self.assertRaises(StateContractError):
            self.dm._switch_dialogue_mode("invalid_mode_999")
        self.assertEqual(self.dm.dialogue_mode, initial_mode)

    # 8. Invalid old runtime mode fails closed when flag=true
    @patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=True)
    def test_08_invalid_old_mode_fails_closed(self, mock_flag) -> None:
        self.dm.dialogue_mode = "invalid_old_mode"
        with self.assertRaises(StateContractError):
            self.dm._switch_dialogue_mode("knowledge_qa")

    # 9. Runtime new_mode="uncertain" is rejected when flag=true
    @patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=True)
    def test_09_runtime_new_mode_uncertain_rejected(self, mock_flag) -> None:
        self.dm.dialogue_mode = "task_collection"
        with self.assertRaises(StateContractError):
            self.dm._switch_dialogue_mode("uncertain")
        self.assertEqual(self.dm.dialogue_mode, "task_collection")

    # 10. Legacy snapshot with "uncertain" in history compatibility preserved
    def test_10_legacy_snapshot_uncertain_compatibility_preserved(self) -> None:
        legacy_snap = {
            "version": 1,
            "dialogue_mode": "task_collection",
            "last_mode_transition": {
                "from": "uncertain",
                "to": "task_collection",
                "source": "legacy",
                "confidence": 1.0,
                "changed_at": "2026-08-01T00:00:00+00:00",
            },
            "mode_transition_history": [
                {
                    "from": "uncertain",
                    "to": "task_collection",
                    "source": "legacy",
                    "confidence": 1.0,
                    "changed_at": "2026-08-01T00:00:00+00:00",
                }
            ],
            "task_state": {},
            "slots": {},
        }
        # Should not raise exception
        state = session_state_from_legacy_snapshot(legacy_snap)
        self.assertEqual(state.conversation.dialogue_mode, "task_collection")

    # 11. Invalid confidence fails closed without partial mutation
    @patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=True)
    def test_11_invalid_confidence_fails_closed_zero_partial_mutation(self, mock_flag) -> None:
        initial_mode = self.dm.dialogue_mode
        initial_hist = copy.deepcopy(self.dm.mode_transition_history)
        with self.assertRaises(StateContractError):
            self.dm._switch_dialogue_mode("knowledge_qa", confidence=2.5)
        self.assertEqual(self.dm.dialogue_mode, initial_mode)
        self.assertEqual(self.dm.mode_transition_history, initial_hist)

    # 12. Knowledge QA turn does not mutate SlotStore task facts
    def test_12_knowledge_qa_does_not_mutate_slot_store(self) -> None:
        self.dm.slot_store.slots["task_type_key"] = Slot("task_type_key", value="pipeline_inspection", status="valid")
        task_state_before = copy.deepcopy(self.dm.slot_store.get_task_state())

        self.llm.queue_plan(make_plan("READ", query_intent="GENERAL_CHAT"))
        self.dm.process("今天天气怎么样？")
        self.assertEqual(self.dm.dialogue_mode, "knowledge_qa")
        self.assertEqual(self.dm.slot_store.get_task_state(), task_state_before)

    # --------------------------------------------------------------------------
    # B. Execution Transition & Safety Contract Tests
    # --------------------------------------------------------------------------

    # 13. idle -> stop_requested
    def test_13_idle_to_stop_requested(self) -> None:
        req = {"action": "stop", "status": "requested", "source": "interaction_plan", "confidence": 1.0, "reason": "test"}
        self.dm._set_execution_control_state("stop_requested", req)
        self.assertEqual(self.dm.control_state, "stop_requested")
        self.assertEqual(self.dm.last_control_request["action"], "stop")

    # 14. idle -> pause_requested
    def test_14_idle_to_pause_requested(self) -> None:
        req = {"action": "pause", "status": "requested", "source": "interaction_plan", "confidence": 1.0, "reason": "test"}
        self.dm._set_execution_control_state("pause_requested", req)
        self.assertEqual(self.dm.control_state, "pause_requested")

    # 15. idle -> abort_requested
    def test_15_idle_to_abort_requested(self) -> None:
        req = {"action": "abort", "status": "requested", "source": "interaction_plan", "confidence": 1.0, "reason": "test"}
        self.dm._set_execution_control_state("abort_requested", req)
        self.assertEqual(self.dm.control_state, "abort_requested")

    # 16. idle -> cancel_requested
    def test_16_idle_to_cancel_requested(self) -> None:
        req = {"action": "cancel", "status": "requested", "source": "interaction_plan", "confidence": 1.0, "reason": "test"}
        self.dm._set_execution_control_state("cancel_requested", req)
        self.assertEqual(self.dm.control_state, "cancel_requested")

    # 17. requested -> requested transition allowed in done phase
    def test_17_requested_to_requested_transition_allowed(self) -> None:
        req1 = {"action": "stop", "status": "requested", "source": "interaction_plan", "confidence": 1.0, "reason": "test1"}
        self.dm._set_execution_control_state("stop_requested", req1)

        req2 = {"action": "pause", "status": "requested", "source": "interaction_plan", "confidence": 1.0, "reason": "test2"}
        self.dm._set_execution_control_state("pause_requested", req2)
        self.assertEqual(self.dm.control_state, "pause_requested")

    # 18. Mismatched action and state fails closed when flag=true
    @patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=True)
    def test_18_mismatched_action_state_fails_closed(self, mock_flag) -> None:
        req = {"action": "stop", "status": "requested", "source": "interaction_plan", "confidence": 1.0, "reason": "test"}
        with self.assertRaises(StateContractError):
            self.dm._set_execution_control_state("pause_requested", req)

        self.assertEqual(self.dm.control_state, "idle")
        self.assertIsNone(self.dm.last_control_request)

    # 19. Non-requested status fails closed when flag=true
    @patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=True)
    def test_19_non_requested_status_fails_closed(self, mock_flag) -> None:
        req = {"action": "stop", "status": "executing", "source": "interaction_plan", "confidence": 1.0, "reason": "test"}
        with self.assertRaises(StateContractError):
            self.dm._set_execution_control_state("stop_requested", req)

        self.assertEqual(self.dm.control_state, "idle")
        self.assertIsNone(self.dm.last_control_request)

    # 20. Validation failure zero partial mutation
    @patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=True)
    def test_20_validation_failure_zero_partial_mutation(self, mock_flag) -> None:
        initial_control = self.dm.control_state
        initial_request = copy.deepcopy(self.dm.last_control_request)
        with self.assertRaises(StateContractError):
            self.dm._set_execution_control_state("invalid_state", None)

        self.assertEqual(self.dm.control_state, initial_control)
        self.assertEqual(self.dm.last_control_request, initial_request)

    # 21. Draft cancel resets control state to idle
    def test_21_draft_cancel_clears_control_state_to_idle(self) -> None:
        self.dm.slot_store.slots["task_type_key"] = Slot("task_type_key", value="pipeline_inspection", status="valid")
        self.dm.task_state = self.dm.slot_store.get_task_state()

        self.llm.queue_plan(make_plan("CONTROL", emergency_action="cancel"))
        self.dm.process("取消当前任务")
        self.assertEqual(self.dm.phase, "rejected")
        self.assertEqual(self.dm.control_state, "idle")
        self.assertIsNone(self.dm.last_control_request)

    # 22. Published cancel records cancel_requested
    def test_22_published_cancel_records_cancel_requested(self) -> None:
        self.dm._transition_phase("done", reason="test_setup")
        route_mock = MagicMock()
        route_mock.emergency_action = "cancel"
        route_mock.source = "interaction_plan"
        route_mock.confidence = 1.0
        route_mock.reason = "user_command"

        self.dm._handle_emergency_intervention("取消", route_mock)
        self.assertEqual(self.dm.control_state, "cancel_requested")
        self.assertIsNotNone(self.dm.last_control_request)

    # 23. Collecting phase stop/pause/abort does not create robot execution request
    def test_23_collecting_phase_stop_pause_abort_no_execution_request(self) -> None:
        self.dm.phase = "collecting"
        self.dm.slot_store.slots["task_type_key"] = Slot("task_type_key", value="pipeline_inspection", status="valid")
        self.dm.task_state = self.dm.slot_store.get_task_state()

        route_mock = MagicMock()
        route_mock.emergency_action = "stop"
        route_mock.source = "interaction_plan"
        route_mock.confidence = 1.0
        route_mock.reason = "user_command"

        reply = self.dm._handle_emergency_intervention("停止", route_mock)
        self.assertIn("可执行", reply)
        self.assertEqual(self.dm.control_state, "idle")
        self.assertIsNone(self.dm.last_control_request)

    # 24. Blocked phase stop/pause/abort does not create robot execution request
    def test_24_blocked_phase_stop_pause_abort_no_execution_request(self) -> None:
        for ph in ["blocked_soft", "blocked_hard"]:
            with self.subTest(phase=ph):
                self.dm.phase = ph
                self.dm.slot_store.slots["task_type_key"] = Slot("task_type_key", value="pipeline_inspection", status="valid")
                self.dm.task_state = self.dm.slot_store.get_task_state()

                route_mock = MagicMock()
                route_mock.emergency_action = "pause"
                route_mock.source = "interaction_plan"
                route_mock.confidence = 1.0
                route_mock.reason = "user_command"

                reply = self.dm._handle_emergency_intervention("暂停", route_mock)
                self.assertIn("可执行", reply)
                self.assertEqual(self.dm.control_state, "idle")
                self.assertIsNone(self.dm.last_control_request)

    # 25. Reset preserves execution control state reset
    def test_25_reset_preserves_execution_state_reset(self) -> None:
        req = {"action": "stop", "status": "requested", "source": "interaction_plan", "confidence": 1.0, "reason": "test"}
        self.dm._set_execution_control_state("stop_requested", req)
        self.dm.reset()
        self.assertEqual(self.dm.control_state, "idle")
        self.assertIsNone(self.dm.last_control_request)

    # 26. Snapshot restore preserves execution control state
    def test_26_snapshot_restore_preserves_execution_control_state(self) -> None:
        req = {"action": "pause", "status": "requested", "source": "interaction_plan", "confidence": 1.0, "reason": "test"}
        self.dm._set_execution_control_state("pause_requested", req)

        snap = self.dm.export_snapshot()
        dm2 = DialogueManager()
        dm2.load_snapshot(snap)
        self.assertEqual(dm2.control_state, "pause_requested")

    # --------------------------------------------------------------------------
    # C. Cross-Domain Audit (Test 27)
    # --------------------------------------------------------------------------

    # 27. done phase + control request + task modification audit
    def test_27_done_phase_control_request_plus_task_modification_audit(self) -> None:
        # Step 1: Task published and in done phase
        schema = self.dm.builder.get_schema("pipeline_inspection", self.dm.mode)
        self.dm.slot_store.init_task_slots(schema)
        slots = self.dm.slot_store.clone_slots()
        slots["task_type"] = Slot("task_type", value="管缆巡检", status="valid")
        slots["task_type_key"] = Slot("task_type_key", value="pipeline_inspection", status="valid")
        slots["water_depth"] = Slot("water_depth", value=300.0, status="valid")
        slots["task_id"] = Slot("task_id", value="PI-20260809-001", status="valid")
        slots["intent_id"] = Slot("intent_id", value="TI20260809001", status="valid")
        slots["internal_id"] = Slot("internal_id", value="123e4567-e89b-12d3-a456-426614174000", status="valid")
        self.dm.slot_store.commit_transaction(slots, [])
        self.dm.task_state = self.dm.slot_store.get_task_state()
        self.dm._transition_phase("done", reason="test_setup")

        # Step 2: Emergency control request issued for published task
        self.llm.queue_plan(make_plan("CONTROL", emergency_action="stop"))
        self.dm.process("停止当前任务")

        self.assertEqual(self.dm.phase, "done")
        self.assertEqual(self.dm.control_state, "stop_requested")
        self.assertIsNotNone(self.dm.last_control_request)
        self.assertEqual(self.dm.last_control_request["action"], "stop")
        self.assertEqual(self.dm.last_control_request["target_intent_id"], "TI20260809001")

        # Step 3: Real task parameter modification via process()
        self.llm.queue_plan(make_plan("WRITE"))
        self.llm.queue_extraction(
            {
                "slot_candidates": [
                    {
                        "raw_key": "水深",
                        "canonical_key": "water_depth",
                        "raw_value": "500 米",
                        "normalized_value": 500.0,
                        "confidence": 0.95,
                        "resolution_method": "llm_semantic",
                    }
                ],
                "list_mutations": [],
                "unresolved": [],
            }
        )
        reply = self.dm.process("修改水深为 500 米")

        # Audit phase, control_state, last_control_request, target_intent_id
        self.assertEqual(self.dm.phase, "done")
        self.assertEqual(self.dm.slot_store.get_task_state()["water_depth"], 300.0)
        self.assertIn("已正式确认发布", reply)
        self.assertIn("无法就地修改参数", reply)
        self.assertEqual(self.dm.control_state, "stop_requested")
        self.assertIsNotNone(self.dm.last_control_request)
        self.assertEqual(self.dm.last_control_request["action"], "stop")
        self.assertEqual(self.dm.last_control_request["target_intent_id"], "TI20260809001")


if __name__ == "__main__":
    unittest.main()
