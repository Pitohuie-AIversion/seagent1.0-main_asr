"""tests/test_task_phase_transition_matrix_v2.py

Unit, integration, and boundary tests for SEAgent G3.4-A Task Phase Transition Legality Matrix.
Validates TASK_PHASE_TRANSITIONS, validate_task_phase_transition, Fail Closed behavior in _transition_phase(),
and verifies that snapshot restoration and reset bypass runtime transition matrix restrictions.
"""

import copy
import unittest
from unittest.mock import patch, MagicMock

from src.dialogue_manager import DialogueManager
from src.session_state import (
    StateContractError,
    TASK_PHASE_TRANSITIONS,
    VALID_PHASES,
    validate_task_phase_transition,
)
from src.slot_store import Slot


class TestTaskPhaseTransitionMatrixV2(unittest.TestCase):

    def setUp(self) -> None:
        self.dm = DialogueManager()

    # --------------------------------------------------------------------------
    # A. Contract Matrix Tests
    # --------------------------------------------------------------------------

    # 1. Every VALID_PHASES has a clear TASK_PHASE_TRANSITIONS definition
    def test_01_matrix_completeness_all_valid_phases_defined(self) -> None:
        self.assertEqual(frozenset(TASK_PHASE_TRANSITIONS.keys()), VALID_PHASES)
        for phase, allowed_targets in TASK_PHASE_TRANSITIONS.items():
            self.assertTrue(isinstance(allowed_targets, frozenset))
            self.assertTrue(allowed_targets.issubset(VALID_PHASES))

    # 2. All Inventory confirmed runtime edges are allowed
    def test_02_all_inventory_edges_allowed(self) -> None:
        inventory_edges = [
            ("collecting", "collecting"),
            ("collecting", "confirming"),
            ("collecting", "blocked_soft"),
            ("collecting", "blocked_hard"),
            ("collecting", "rejected"),
            ("blocked_soft", "collecting"),
            ("blocked_soft", "confirming"),
            ("blocked_soft", "blocked_soft"),
            ("blocked_soft", "blocked_hard"),
            ("blocked_soft", "done"),
            ("blocked_soft", "rejected"),
            ("blocked_hard", "collecting"),
            ("blocked_hard", "confirming"),
            ("blocked_hard", "blocked_soft"),
            ("blocked_hard", "blocked_hard"),
            ("blocked_hard", "rejected"),
            ("confirming", "collecting"),
            ("confirming", "confirming"),
            ("confirming", "blocked_soft"),
            ("confirming", "blocked_hard"),
            ("confirming", "done"),
            ("confirming", "rejected"),
            ("done", "done"),
            ("done", "confirming"),
            ("done", "collecting"),
            ("rejected", "rejected"),
            ("rejected", "collecting"),
            ("rejected", "confirming"),
            ("rejected", "blocked_soft"),
            ("rejected", "blocked_hard"),
        ]
        for old_p, new_p in inventory_edges:
            with self.subTest(old_phase=old_p, new_phase=new_p):
                # Should not raise exception
                validate_task_phase_transition(old_p, new_p)

    # 3. Explicit illegal edges are rejected
    def test_03_illegal_edges_rejected(self) -> None:
        illegal_edges = [
            ("collecting", "done"),
            ("done", "blocked_soft"),
            ("done", "blocked_hard"),
            ("done", "rejected"),
            ("rejected", "done"),
        ]
        for old_p, new_p in illegal_edges:
            with self.subTest(old_phase=old_p, new_phase=new_p):
                with self.assertRaises(StateContractError):
                    validate_task_phase_transition(old_p, new_p)

    # 4. Invalid old_phase fails closed
    def test_04_invalid_old_phase_fails_closed(self) -> None:
        with self.assertRaises(StateContractError):
            validate_task_phase_transition("invalid_old_phase", "collecting")

    # 5. Invalid new_phase fails closed
    def test_05_invalid_new_phase_fails_closed(self) -> None:
        with self.assertRaises(StateContractError):
            validate_task_phase_transition("collecting", "invalid_new_phase")

    # 6. validate_task_phase_transition is pure with no side effects
    def test_06_validation_pure_no_side_effects(self) -> None:
        initial_matrix = copy.deepcopy(TASK_PHASE_TRANSITIONS)
        validate_task_phase_transition("collecting", "confirming")
        with self.assertRaises(StateContractError):
            validate_task_phase_transition("collecting", "done")
        self.assertEqual(TASK_PHASE_TRANSITIONS, initial_matrix)

    # --------------------------------------------------------------------------
    # B. DialogueManager Integration Tests
    # --------------------------------------------------------------------------

    # 7. flag=true valid transition succeeds
    @patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=True)
    def test_07_flag_true_valid_transition_succeeds(self, mock_flag) -> None:
        self.dm.phase = "collecting"
        self.dm._transition_phase("confirming", reason="test")
        self.assertEqual(self.dm.phase, "confirming")

    # 8. flag=true illegal edge fails closed with zero mutation
    @patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=True)
    def test_08_flag_true_illegal_edge_fails_closed_zero_mutation(self, mock_flag) -> None:
        self.dm.phase = "collecting"
        with self.assertRaises(StateContractError):
            self.dm._transition_phase("done", reason="test_illegal")

        # Phase remains collecting
        self.assertEqual(self.dm.phase, "collecting")

    # 9. flag=false legacy behavior preserved
    @patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=False)
    def test_09_flag_false_legacy_behavior_preserved(self, mock_flag) -> None:
        self.dm.phase = "collecting"
        self.dm._transition_phase("confirming", reason="test_legacy")
        self.assertEqual(self.dm.phase, "confirming")

    # 10. blocked_soft acknowledgement flow preserved
    def test_10_blocked_soft_acknowledgement_flow_preserved(self) -> None:
        self.dm.phase = "blocked_soft"
        self.dm.slot_store.slots["task_type_key"] = Slot("task_type_key", value="pipeline_inspection", status="valid")
        self.dm.task_state = self.dm.slot_store.get_task_state()

        # Simulate soft warning confirmation
        self.dm._handle_soft_warning_confirmation("确认忽略警告", "req_test_soft")
        self.assertIn(self.dm.phase, ("collecting", "confirming"))

    # 11. blocked_soft to blocked_hard preserved
    def test_11_blocked_soft_to_blocked_hard_preserved(self) -> None:
        self.dm.phase = "blocked_soft"
        self.dm._transition_phase("blocked_hard", reason="soft_upgraded_to_hard")
        self.assertEqual(self.dm.phase, "blocked_hard")

    # 12. blocked_hard resolution / downgrade preserved
    def test_12_blocked_hard_resolution_downgrade_preserved(self) -> None:
        self.dm.phase = "blocked_hard"
        self.dm._transition_phase("blocked_soft", reason="hard_downgraded_to_soft")
        self.assertEqual(self.dm.phase, "blocked_soft")

        self.dm.phase = "blocked_hard"
        self.dm._transition_phase("collecting", reason="hard_constraint_resolved")
        self.assertEqual(self.dm.phase, "collecting")

    # 13. hard refusal limit to rejected preserved
    def test_13_hard_refusal_limit_to_rejected_preserved(self) -> None:
        self.dm.phase = "blocked_hard"
        self.dm._transition_phase("rejected", reason="hard_refusal_limit_reached")
        self.assertEqual(self.dm.phase, "rejected")

    # 14. confirming to done publish path preserved
    def test_14_confirming_to_done_publish_path_preserved(self) -> None:
        self.dm.phase = "confirming"
        self.dm._transition_phase("done", reason="publish_success")
        self.assertEqual(self.dm.phase, "done")

    # 15. publish rollback restores phase preserved
    def test_15_publish_rollback_restores_phase_preserved(self) -> None:
        self.dm.phase = "confirming"
        prev_phase = self.dm.phase
        target_phase = self.dm.phase if self.dm.phase == "blocked_soft" else prev_phase
        self.dm._transition_phase(target_phase, reason="publish_rollback")
        self.assertEqual(self.dm.phase, "confirming")

    # 16. task modification phase transition preserved
    def test_16_task_modification_phase_transition_preserved(self) -> None:
        # Done task modified -> confirming or collecting
        self.dm.phase = "done"
        self.dm._transition_phase("confirming", reason="task_modified")
        self.assertEqual(self.dm.phase, "confirming")

        self.dm.phase = "done"
        self.dm._transition_phase("collecting", reason="task_modified")
        self.assertEqual(self.dm.phase, "collecting")

    # --------------------------------------------------------------------------
    # C. Boundary Tests
    # --------------------------------------------------------------------------

    # 17. reset bypasses matrix restrictions
    @patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=True)
    def test_17_reset_bypasses_matrix_restrictions(self, mock_flag) -> None:
        self.dm.phase = "done"
        self.dm.reset()
        self.assertEqual(self.dm.phase, "collecting")

    # 18. load_snapshot restore bypasses matrix restrictions
    @patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=True)
    def test_18_load_snapshot_restore_bypasses_matrix_restrictions(self, mock_flag) -> None:
        self.dm.phase = "rejected"
        snap = {
            "version": 1,
            "phase": "blocked_hard",
            "dialogue_mode": "task_collection",
            "task_state": {},
            "slots": {},
        }
        self.dm.load_snapshot(snap)
        self.assertEqual(self.dm.phase, "blocked_hard")

    # 19. done snapshot missing final file fallback preserved
    def test_19_done_snapshot_missing_final_fallback_preserved(self) -> None:
        snap = {
            "version": 1,
            "phase": "done",
            "dialogue_mode": "task_collection",
            "intent_id": "TI_NONEXISTENT_999",
            "task_state": {},
            "slots": {},
        }
        # In load_snapshot, missing final file causes done to fallback to collecting
        self.dm.load_snapshot(snap)
        self.assertEqual(self.dm.phase, "collecting")

    # 20. rejected snapshot restore preserved
    def test_20_rejected_snapshot_restore_preserved(self) -> None:
        snap = {
            "version": 1,
            "phase": "rejected",
            "dialogue_mode": "task_collection",
            "task_state": {},
            "slots": {},
        }
        self.dm.load_snapshot(snap)
        self.assertEqual(self.dm.phase, "rejected")

    # 21. ordinary knowledge QA does not mutate task phase
    def test_21_ordinary_knowledge_qa_does_not_mutate_task_phase(self) -> None:
        self.dm.phase = "collecting"
        self.dm.process("今天天气怎么样？")
        self.assertEqual(self.dm.phase, "collecting")


if __name__ == "__main__":
    unittest.main()
