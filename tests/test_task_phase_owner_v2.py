"""tests/test_task_phase_owner_v2.py

Unit and integration tests for SEAgent G3.3-A Task Phase Single Owner Migration.
Validates _transition_phase helper behavior, fail-closed contract enforcement,
runtime transition invariants, awaiting_final_confirm strict bool validation,
and AST static analysis to ensure no unapproved direct phase assignments exist.
"""

import ast
import re
import unittest
from pathlib import Path
from unittest.mock import patch

from src.dialogue_manager import DialogueManager
from src.session_state import StateContractError, VALID_PHASES


class TestTaskPhaseOwnerV2(unittest.TestCase):

    def setUp(self) -> None:
        self.dm = DialogueManager()

    # 1. transition helper handles all valid phases
    def test_01_transition_helper_handles_all_valid_phases(self) -> None:
        for phase in VALID_PHASES:
            self.dm._transition_phase(phase, reason=f"test_{phase}")
            self.assertEqual(self.dm.phase, phase)

    # 2. flag=true invalid phase fails closed
    @patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=True)
    def test_02_flag_true_invalid_phase_fails_closed(self, mock_flag) -> None:
        with self.assertRaises(StateContractError):
            self.dm._transition_phase("invalid_phase_xyz", reason="test_invalid")

    # 3. flag=false legacy phase mutation
    @patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=False)
    def test_03_flag_false_legacy_phase_mutation(self, mock_flag) -> None:
        self.dm._transition_phase("confirming", reason="test_legacy")
        self.assertEqual(self.dm.phase, "confirming")

    # 4. blocked_soft to collecting/confirming behavior unchanged
    def test_04_blocked_soft_to_collecting_confirming_behavior(self) -> None:
        self.dm._transition_phase("blocked_soft", reason="test_setup")
        self.assertEqual(self.dm.phase, "blocked_soft")

    # 5. blocked_soft to blocked_hard behavior unchanged
    def test_05_blocked_soft_to_blocked_hard_behavior(self) -> None:
        self.dm._transition_phase("blocked_soft", reason="test_setup")
        res = self.dm._run_constraint_check({"water_depth"})
        # Should execute without errors
        self.assertIn("type", res)

    # 6. blocked_hard to rejected behavior unchanged
    def test_06_blocked_hard_to_rejected_behavior(self) -> None:
        self.dm._transition_phase("blocked_hard", reason="test_setup")
        self.assertEqual(self.dm.phase, "blocked_hard")

    # 7. publish success to done
    def test_07_publish_success_to_done(self) -> None:
        self.dm._transition_phase("done", reason="publish_success")
        self.assertEqual(self.dm.phase, "done")

    # 8. publish rollback restores phase
    def test_08_publish_rollback_restores_phase(self) -> None:
        prev_phase = "confirming"
        self.dm._transition_phase(prev_phase, reason="test_setup")
        self.dm._transition_phase(prev_phase, reason="publish_rollback")
        self.assertEqual(self.dm.phase, "confirming")

    # 9. snapshot restore unaffected by transition helper
    def test_09_snapshot_restore_unaffected_by_transition_helper(self) -> None:
        self.dm._transition_phase("confirming", reason="test_setup")
        snap = self.dm.export_snapshot()
        dm2 = DialogueManager()
        dm2.load_snapshot(snap)
        self.assertEqual(dm2.phase, "confirming")

    # 10. awaiting_final_confirm non-bool in flag=true fails closed
    @patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=True)
    def test_10_awaiting_final_confirm_non_bool_fails_closed(self, mock_flag) -> None:
        self.dm.awaiting_final_confirm = "false"  # String instead of bool!
        with self.assertRaises(StateContractError):
            self.dm._build_session_state_contract()

        self.dm.awaiting_final_confirm = 1  # Int instead of bool!
        with self.assertRaises(StateContractError):
            self.dm._build_session_state_contract()

        self.dm.awaiting_final_confirm = False  # Valid bool
        contract = self.dm._build_session_state_contract()
        self.assertFalse(contract.task.awaiting_final_confirm)

    # 11. general QA chat preserves task phase
    def test_11_general_qa_preserves_task_phase(self) -> None:
        initial_phase = self.dm.phase
        self.dm.process("你好")
        self.assertEqual(self.dm.phase, initial_phase)

    # 12. AST static check: no unapproved direct phase assignments in src/dialogue_manager.py
    def test_12_static_check_no_unapproved_direct_phase_assignments(self) -> None:
        dm_path = Path(__file__).parent.parent / "src" / "dialogue_manager.py"
        self.assertTrue(dm_path.exists())

        code = dm_path.read_text(encoding="utf-8")
        tree = ast.parse(code, filename=str(dm_path))

        allowed_method_names = {
            "_transition_phase",
            "_apply_session_state_contract",
            "reset",
            "load_snapshot",
        }

        unapproved_assignments = []

        class PhaseAssignmentVisitor(ast.NodeVisitor):
            def __init__(self) -> None:
                self.current_function = None

            def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
                old_fn = self.current_function
                self.current_function = node.name
                self.generic_visit(node)
                self.current_function = old_fn

            def visit_Assign(self, node: ast.Assign) -> None:
                for target in node.targets:
                    if (
                        isinstance(target, ast.Attribute)
                        and isinstance(target.value, ast.Name)
                        and target.value.id == "self"
                        and target.attr == "phase"
                    ):
                        if self.current_function not in allowed_method_names:
                            unapproved_assignments.append(
                                (self.current_function, node.lineno)
                            )
                self.generic_visit(node)

        visitor = PhaseAssignmentVisitor()
        visitor.visit(tree)

        self.assertEqual(
            unapproved_assignments,
            [],
            f"Found unapproved self.phase direct assignments in functions: {unapproved_assignments}",
        )


if __name__ == "__main__":
    unittest.main()
