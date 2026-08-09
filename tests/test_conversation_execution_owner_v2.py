"""tests/test_conversation_execution_owner_v2.py

Unit and integration tests for SEAgent G3.3-B Conversation / Execution State Owner Migration.
Validates _switch_dialogue_mode and _set_execution_control_state helpers,
candidate validation, zero partial mutation on failure, draft vs execution cancel distinction,
and AST static analysis ensuring no unapproved direct field assignments exist.
"""

import ast
import copy
import unittest
from pathlib import Path
from unittest.mock import patch

from src.dialogue_manager import DialogueManager
from src.session_state import StateContractError
from src.slot_store import Slot, SlotStore


class TestConversationExecutionOwnerV2(unittest.TestCase):

    def setUp(self) -> None:
        self.dm = DialogueManager()

    # 1. Legal dialogue_mode transition works normally
    def test_01_valid_dialogue_mode_transition_normal(self) -> None:
        self.dm._switch_dialogue_mode("knowledge_qa", source="test", reason="unit_test")
        self.assertEqual(self.dm.dialogue_mode, "knowledge_qa")
        self.assertIsNotNone(self.dm.last_mode_transition)
        self.assertEqual(self.dm.last_mode_transition["from"], "task_collection")
        self.assertEqual(self.dm.last_mode_transition["to"], "knowledge_qa")
        self.assertEqual(len(self.dm.mode_transition_history), 1)

    # 2. Same mode transition does not duplicate history
    def test_02_same_mode_no_duplicate_history(self) -> None:
        self.dm._switch_dialogue_mode("knowledge_qa", source="test1", reason="first_switch")
        self.assertEqual(len(self.dm.mode_transition_history), 1)

        # Same mode transition
        self.dm._switch_dialogue_mode("knowledge_qa", source="test2", reason="same_switch")
        self.assertEqual(len(self.dm.mode_transition_history), 1)

    # 3. Transition history keeps max 50 items
    def test_03_history_max_50_items(self) -> None:
        modes = ["knowledge_qa", "task_collection"]
        for i in range(60):
            self.dm._switch_dialogue_mode(modes[i % 2], source="loop", reason=f"step_{i}")
        self.assertLessEqual(len(self.dm.mode_transition_history), 50)

    # 4. flag=true invalid new dialogue_mode fails closed
    @patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=True)
    def test_04_flag_true_invalid_new_dialogue_mode_fails_closed(self, mock_flag) -> None:
        initial_mode = self.dm.dialogue_mode
        initial_last = copy.deepcopy(self.dm.last_mode_transition)
        initial_hist = copy.deepcopy(self.dm.mode_transition_history)

        with self.assertRaises(StateContractError):
            self.dm._switch_dialogue_mode("invalid_mode_xyz")

        # Zero partial mutation
        self.assertEqual(self.dm.dialogue_mode, initial_mode)
        self.assertEqual(self.dm.last_mode_transition, initial_last)
        self.assertEqual(self.dm.mode_transition_history, initial_hist)

    # 5. flag=true invalid confidence fails closed with zero partial mutation
    @patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=True)
    def test_05_flag_true_invalid_confidence_fails_closed_zero_partial_mutation(self, mock_flag) -> None:
        initial_mode = self.dm.dialogue_mode
        initial_last = copy.deepcopy(self.dm.last_mode_transition)
        initial_hist = copy.deepcopy(self.dm.mode_transition_history)

        with self.assertRaises(StateContractError):
            self.dm._switch_dialogue_mode("knowledge_qa", confidence=1.5)

        self.assertEqual(self.dm.dialogue_mode, initial_mode)
        self.assertEqual(self.dm.last_mode_transition, initial_last)
        self.assertEqual(self.dm.mode_transition_history, initial_hist)

    # 6. Ordinary knowledge QA mode switch preserves SlotStore Task Facts
    def test_06_knowledge_qa_mode_switch_preserves_task_facts(self) -> None:
        self.dm.process("在井口 A 进行采油树阀门替换任务")
        state_before = copy.deepcopy(self.dm.slot_store.get_task_state())

        self.dm.process("你觉得今天天气怎么样？")
        self.assertEqual(self.dm.dialogue_mode, "knowledge_qa")
        self.assertEqual(self.dm.slot_store.get_task_state(), state_before)

    # 7. Execution stop request state valid
    def test_07_execution_stop_request_valid(self) -> None:
        req = {
            "action": "stop",
            "status": "requested",
            "source": "rule",
            "confidence": 1.0,
            "reason": "emergency_stop",
        }
        self.dm._set_execution_control_state("stop_requested", req)
        self.assertEqual(self.dm.control_state, "stop_requested")
        self.assertEqual(self.dm.last_control_request["action"], "stop")

    # 8. Execution pause request state valid
    def test_08_execution_pause_request_valid(self) -> None:
        req = {
            "action": "pause",
            "status": "requested",
            "source": "rule",
            "confidence": 1.0,
            "reason": "emergency_pause",
        }
        self.dm._set_execution_control_state("pause_requested", req)
        self.assertEqual(self.dm.control_state, "pause_requested")
        self.assertEqual(self.dm.last_control_request["action"], "pause")

    # 9. Execution abort / cancel request valid
    def test_09_execution_abort_cancel_request_valid(self) -> None:
        req_abort = {
            "action": "abort",
            "status": "requested",
            "source": "rule",
            "confidence": 1.0,
            "reason": "abort_task",
        }
        self.dm._set_execution_control_state("abort_requested", req_abort)
        self.assertEqual(self.dm.control_state, "abort_requested")

        req_cancel = {
            "action": "cancel",
            "status": "requested",
            "source": "rule",
            "confidence": 1.0,
            "reason": "cancel_task",
        }
        self.dm._set_execution_control_state("cancel_requested", req_cancel)
        self.assertEqual(self.dm.control_state, "cancel_requested")

    # 10. Invalid execution state/request combination fails closed
    @patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=True)
    def test_10_invalid_execution_combination_fails_closed(self, mock_flag) -> None:
        # stop_requested with None last_control_request
        with self.assertRaises(StateContractError):
            self.dm._set_execution_control_state("stop_requested", None)

        # idle with non-None last_control_request
        with self.assertRaises(StateContractError):
            self.dm._set_execution_control_state("idle", {"action": "stop", "status": "requested"})

        # Mismatched action and state
        with self.assertRaises(StateContractError):
            self.dm._set_execution_control_state("stop_requested", {"action": "pause", "status": "requested"})

    # 11. Execution validation failure keeps both fields unchanged
    @patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=True)
    def test_11_execution_validation_failure_zero_partial_mutation(self, mock_flag) -> None:
        initial_control = self.dm.control_state
        initial_request = copy.deepcopy(self.dm.last_control_request)

        with self.assertRaises(StateContractError):
            self.dm._set_execution_control_state("invalid_control_state", None)

        self.assertEqual(self.dm.control_state, initial_control)
        self.assertEqual(self.dm.last_control_request, initial_request)

    # 12. Draft cancel and execution cancel distinct semantics
    def test_12_draft_cancel_and_execution_cancel_distinct_semantics(self) -> None:
        # Draft cancel during active draft collection
        self.dm.slot_store.slots["task_type_key"] = Slot("task_type_key", value="pipeline_inspection", status="valid")
        self.dm.task_state = self.dm.slot_store.get_task_state()

        self.dm.process("取消当前任务")
        self.assertEqual(self.dm.phase, "rejected")
        self.assertEqual(self.dm.control_state, "idle")
        self.assertIsNone(self.dm.last_control_request)

        # Execution cancel on published (done) task
        self.dm._transition_phase("done", reason="test_setup")
        route_mock = unittest.mock.MagicMock()
        route_mock.emergency_action = "cancel"
        route_mock.source = "rule"
        route_mock.confidence = 1.0
        route_mock.reason = "user_command"
        self.dm._handle_emergency_intervention("取消", route_mock)
        self.assertEqual(self.dm.control_state, "cancel_requested")
        self.assertIsNotNone(self.dm.last_control_request)

    # 13. Reset behavior preserved
    def test_13_reset_behavior_preserved(self) -> None:
        self.dm._switch_dialogue_mode("knowledge_qa")
        req = {"action": "stop", "status": "requested"}
        self.dm._set_execution_control_state("stop_requested", req)

        self.dm.reset()
        self.assertEqual(self.dm.dialogue_mode, "task_collection")
        self.assertEqual(self.dm.control_state, "idle")
        self.assertIsNone(self.dm.last_control_request)

    # 14. Snapshot restore behavior preserved
    def test_14_snapshot_restore_behavior_preserved(self) -> None:
        self.dm._switch_dialogue_mode("knowledge_qa")
        req = {"action": "pause", "status": "requested", "source": "rule", "confidence": 1.0, "reason": "test"}
        self.dm._set_execution_control_state("pause_requested", req)

        snap = self.dm.export_snapshot()
        dm2 = DialogueManager()
        dm2.load_snapshot(snap)

        self.assertEqual(dm2.dialogue_mode, "knowledge_qa")
        self.assertEqual(dm2.control_state, "pause_requested")

    # 15. flag=false legacy behavior preserved
    @patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=False)
    def test_15_flag_false_legacy_behavior_preserved(self, mock_flag) -> None:
        self.dm._switch_dialogue_mode("knowledge_qa")
        self.assertEqual(self.dm.dialogue_mode, "knowledge_qa")

        req = {"action": "stop", "status": "requested"}
        self.dm._set_execution_control_state("stop_requested", req)
        self.assertEqual(self.dm.control_state, "stop_requested")

    # 16. AST static check: no unapproved direct field assignments for conversation / execution state
    def test_16_static_check_no_unapproved_direct_field_assignments(self) -> None:
        dm_path = Path(__file__).parent.parent / "src" / "dialogue_manager.py"
        self.assertTrue(dm_path.exists())

        code = dm_path.read_text(encoding="utf-8")
        tree = ast.parse(code, filename=str(dm_path))

        target_fields = {
            "dialogue_mode",
            "last_mode_transition",
            "mode_transition_history",
            "control_state",
            "last_control_request",
        }

        allowed_method_names = {
            "_switch_dialogue_mode",
            "_set_execution_control_state",
            "_apply_session_state_contract",
            "reset",
            "load_snapshot",
            "__init__",
        }

        unapproved_assignments = []

        class FieldAssignmentVisitor(ast.NodeVisitor):
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
                        and target.attr in target_fields
                    ):
                        if self.current_function not in allowed_method_names:
                            unapproved_assignments.append(
                                (self.current_function, target.attr, node.lineno)
                            )
                self.generic_visit(node)

            def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
                if (
                    isinstance(node.target, ast.Attribute)
                    and isinstance(node.target.value, ast.Name)
                    and node.target.value.id == "self"
                    and node.target.attr in target_fields
                ):
                    if self.current_function not in allowed_method_names:
                        unapproved_assignments.append(
                            (self.current_function, node.target.attr, node.lineno)
                        )
                self.generic_visit(node)

        visitor = FieldAssignmentVisitor()
        visitor.visit(tree)

        self.assertEqual(
            unapproved_assignments,
            [],
            f"Found unapproved direct field assignments: {unapproved_assignments}",
        )


if __name__ == "__main__":
    unittest.main()
