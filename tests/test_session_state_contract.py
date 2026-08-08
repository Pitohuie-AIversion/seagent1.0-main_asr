"""tests/test_session_state_contract.py

Unit tests for SEAgent G3.1 State Contract (src/session_state.py).
Validates immutability, deep immutability of nested mappings, fail-closed type/value checking,
schema_version fail-closed rules, legacy snapshot adapter, and isolation from runtime components.
"""

import copy
import dataclasses
import json
import unittest
from datetime import datetime, timezone
from types import MappingProxyType

from src.session_state import (
    ConversationState,
    ExecutionControlState,
    SessionState,
    StateContractError,
    TaskLifecycleState,
    session_state_from_legacy_snapshot,
    session_state_to_legacy_fields,
)


class TestSessionStateContract(unittest.TestCase):

    def setUp(self) -> None:
        self.valid_transition = {
            "from": "task_collection",
            "to": "knowledge_qa",
            "source": "fast_path",
            "confidence": 0.95,
            "reason": "General Q&A",
            "changed_at": datetime.now(timezone.utc).isoformat(),
        }

    # 1. Default legal ConversationState
    def test_01_default_legal_conversation_state(self) -> None:
        cs = ConversationState(dialogue_mode="task_collection")
        self.assertEqual(cs.dialogue_mode, "task_collection")
        self.assertIsNone(cs.last_mode_transition)
        self.assertEqual(cs.mode_transition_history, ())

        cs_with_hist = ConversationState(
            dialogue_mode="knowledge_qa",
            last_mode_transition=self.valid_transition,
            mode_transition_history=[self.valid_transition],
        )
        self.assertEqual(cs_with_hist.dialogue_mode, "knowledge_qa")
        self.assertEqual(cs_with_hist.last_mode_transition["from"], "task_collection")
        self.assertEqual(len(cs_with_hist.mode_transition_history), 1)

    # 2. Default legal TaskLifecycleState
    def test_02_default_legal_task_lifecycle_state(self) -> None:
        tls = TaskLifecycleState(
            phase="collecting",
            mode="normal",
            awaiting_final_confirm=False,
        )
        self.assertEqual(tls.phase, "collecting")
        self.assertEqual(tls.mode, "normal")
        self.assertFalse(tls.awaiting_final_confirm)

    # 3. Default legal ExecutionControlState
    def test_03_default_legal_execution_control_state(self) -> None:
        ecs_idle = ExecutionControlState(control_state="idle")
        self.assertEqual(ecs_idle.control_state, "idle")
        self.assertIsNone(ecs_idle.last_control_request)

        req = {"action": "stop", "status": "requested"}
        ecs_stop = ExecutionControlState(
            control_state="stop_requested",
            last_control_request=req,
        )
        self.assertEqual(ecs_stop.control_state, "stop_requested")
        self.assertEqual(ecs_stop.last_control_request["action"], "stop")

    # 4. SessionState immutable
    def test_04_session_state_immutability(self) -> None:
        cs = ConversationState(dialogue_mode="task_collection")
        tls = TaskLifecycleState(phase="collecting", mode="normal", awaiting_final_confirm=False)
        ecs = ExecutionControlState(control_state="idle")
        ss = SessionState(schema_version=2, conversation=cs, task=tls, execution=ecs)

        with self.assertRaises((dataclasses.FrozenInstanceError, AttributeError)):
            ss.schema_version = 3  # type: ignore[misc]

        with self.assertRaises((dataclasses.FrozenInstanceError, AttributeError)):
            cs.dialogue_mode = "knowledge_qa"  # type: ignore[misc]

        with self.assertRaises((dataclasses.FrozenInstanceError, AttributeError)):
            tls.phase = "done"  # type: ignore[misc]

        with self.assertRaises((dataclasses.FrozenInstanceError, AttributeError)):
            ecs.control_state = "stop_requested"  # type: ignore[misc]

    # 5. Illegal phase fail closed
    def test_05_illegal_phase_fail_closed(self) -> None:
        illegal_phases = ["running", "executing", "completed", "failed", "paused", "", None, 123]
        for p in illegal_phases:
            with self.subTest(phase=p):
                with self.assertRaises(StateContractError):
                    TaskLifecycleState(phase=p, mode="normal", awaiting_final_confirm=False)  # type: ignore[arg-type]

    # 6. Illegal dialogue_mode fail closed
    def test_06_illegal_dialogue_mode_fail_closed(self) -> None:
        illegal_modes = ["invalid_mode", "uncertain", "chat", "", None, 1]
        for dm in illegal_modes:
            with self.subTest(dialogue_mode=dm):
                with self.assertRaises(StateContractError):
                    ConversationState(dialogue_mode=dm)  # type: ignore[arg-type]

    # 7. Illegal control_state fail closed
    def test_07_illegal_control_state_fail_closed(self) -> None:
        illegal_controls = ["running", "stopped", "unknown", "", None, True]
        for ctrl in illegal_controls:
            with self.subTest(control_state=ctrl):
                with self.assertRaises(StateContractError):
                    ExecutionControlState(control_state=ctrl)  # type: ignore[arg-type]

        # Mismatched control_state and last_control_request
        with self.assertRaises(StateContractError):
            ExecutionControlState(control_state="stop_requested", last_control_request=None)

        with self.assertRaises(StateContractError):
            ExecutionControlState(control_state="idle", last_control_request={"action": "stop", "status": "requested"})

        with self.assertRaises(StateContractError):
            ExecutionControlState(control_state="pause_requested", last_control_request={"action": "stop", "status": "requested"})

    # 8. Non-bool awaiting_final_confirm fail closed
    def test_08_non_bool_awaiting_final_confirm_fail_closed(self) -> None:
        non_bools = [1, 0, "true", "false", "True", "False", None, [], {}]
        for nb in non_bools:
            with self.subTest(awaiting_final_confirm=nb):
                with self.assertRaises(StateContractError):
                    TaskLifecycleState(phase="collecting", mode="normal", awaiting_final_confirm=nb)  # type: ignore[arg-type]

    # 9. Transition history validation
    def test_09_transition_history_validation(self) -> None:
        # Invalid item type
        with self.assertRaises(StateContractError):
            ConversationState(dialogue_mode="task_collection", mode_transition_history=["not_a_dict"])  # type: ignore[list-item]

        # Invalid from/to
        bad_trans_mode = dict(self.valid_transition, **{"from": "invalid_mode"})
        with self.assertRaises(StateContractError):
            ConversationState(dialogue_mode="task_collection", mode_transition_history=[bad_trans_mode])

        # Invalid confidence (bool or out of range)
        bad_trans_conf = dict(self.valid_transition, confidence=True)
        with self.assertRaises(StateContractError):
            ConversationState(dialogue_mode="task_collection", mode_transition_history=[bad_trans_conf])

        bad_trans_conf_range = dict(self.valid_transition, confidence=1.5)
        with self.assertRaises(StateContractError):
            ConversationState(dialogue_mode="task_collection", mode_transition_history=[bad_trans_conf_range])

        # Missing or invalid timestamp
        bad_trans_time = dict(self.valid_transition, changed_at="2026-08-08 12:00:00")  # naive timestamp, no timezone
        with self.assertRaises(StateContractError):
            ConversationState(dialogue_mode="task_collection", mode_transition_history=[bad_trans_time])

    # 10. Legacy snapshot -> SessionState
    def test_10_legacy_snapshot_to_session_state(self) -> None:
        legacy_snap = {
            "snapshot_version": 2,
            "phase": "blocked_soft",
            "mode": "emergency",
            "dialogue_mode": "emergency_intervention",
            "last_mode_transition": self.valid_transition,
            "mode_transition_history": [self.valid_transition],
            "control_state": "pause_requested",
            "last_control_request": {"action": "pause", "status": "requested"},
            "slot_store": {"dummy": "data"},  # should be ignored by contract
            "task_state": {"dummy": "data"},  # should be ignored by contract
        }

        ss = session_state_from_legacy_snapshot(legacy_snap)
        self.assertEqual(ss.schema_version, 2)
        self.assertEqual(ss.task.phase, "blocked_soft")
        self.assertEqual(ss.task.mode, "emergency")
        self.assertFalse(ss.task.awaiting_final_confirm)  # default False
        self.assertEqual(ss.conversation.dialogue_mode, "emergency_intervention")
        self.assertEqual(ss.execution.control_state, "pause_requested")
        self.assertEqual(ss.execution.last_control_request["action"], "pause")

        # Test snapshot with 'uncertain' dialogue_mode -> mapped to 'knowledge_qa'
        legacy_uncertain = {"dialogue_mode": "uncertain", "phase": "collecting", "mode": "normal", "control_state": "idle"}
        ss_unc = session_state_from_legacy_snapshot(legacy_uncertain)
        self.assertEqual(ss_unc.conversation.dialogue_mode, "knowledge_qa")

    # 11. SessionState -> legacy-compatible fields
    def test_11_session_state_to_legacy_fields(self) -> None:
        cs = ConversationState(
            dialogue_mode="knowledge_qa",
            last_mode_transition=self.valid_transition,
            mode_transition_history=(self.valid_transition,),
        )
        tls = TaskLifecycleState(phase="confirming", mode="normal", awaiting_final_confirm=True)
        ecs = ExecutionControlState(
            control_state="cancel_requested",
            last_control_request={"action": "cancel", "status": "requested"},
        )
        ss = SessionState(schema_version=2, conversation=cs, task=tls, execution=ecs)

        fields = session_state_to_legacy_fields(ss)
        self.assertEqual(fields["snapshot_version"], 2)
        self.assertEqual(fields["phase"], "confirming")
        self.assertEqual(fields["mode"], "normal")
        self.assertTrue(fields["awaiting_final_confirm"])
        self.assertEqual(fields["dialogue_mode"], "knowledge_qa")
        self.assertEqual(fields["last_mode_transition"], self.valid_transition)
        self.assertEqual(fields["mode_transition_history"], [self.valid_transition])
        self.assertEqual(fields["control_state"], "cancel_requested")
        self.assertEqual(fields["last_control_request"], {"action": "cancel", "status": "requested"})

        # Assert output contains plain serializable dict/list
        self.assertIsInstance(fields["last_mode_transition"], dict)
        self.assertNotIsInstance(fields["last_mode_transition"], MappingProxyType)
        self.assertIsInstance(fields["mode_transition_history"][0], dict)
        self.assertNotIsInstance(fields["mode_transition_history"][0], MappingProxyType)
        self.assertIsInstance(fields["last_control_request"], dict)
        self.assertNotIsInstance(fields["last_control_request"], MappingProxyType)

        # JSON serialization sanity check
        json_str = json.dumps(fields)
        self.assertIn("knowledge_qa", json_str)

    # 12. Round-trip
    def test_12_round_trip(self) -> None:
        legacy_snap = {
            "snapshot_version": 2,
            "phase": "blocked_hard",
            "mode": "normal",
            "awaiting_final_confirm": False,
            "dialogue_mode": "task_collection",
            "last_mode_transition": self.valid_transition,
            "mode_transition_history": [self.valid_transition],
            "control_state": "abort_requested",
            "last_control_request": {"action": "abort", "status": "requested"},
        }

        st1 = session_state_from_legacy_snapshot(legacy_snap)
        fields = session_state_to_legacy_fields(st1)
        st2 = session_state_from_legacy_snapshot(fields)

        self.assertEqual(st1, st2)

    # 13. Contract does not depend on SlotStore or DialogueManager
    def test_13_contract_module_independence(self) -> None:
        import sys
        import src.session_state as ss_mod

        mod_globals = dir(ss_mod)
        self.assertNotIn("SlotStore", mod_globals)
        self.assertNotIn("DialogueManager", mod_globals)

    # 14. Contract creation does not modify DialogueManager / SlotStore
    def test_14_contract_creation_no_runtime_side_effects(self) -> None:
        from src.dialogue_manager import DialogueManager

        dm = DialogueManager()
        dm.phase = "collecting"
        dm.dialogue_mode = "task_collection"
        dm.control_state = "idle"

        initial_dm_dict = copy.deepcopy(dm.export_snapshot())

        cs = ConversationState(dialogue_mode="knowledge_qa")
        tls = TaskLifecycleState(phase="confirming", mode="emergency", awaiting_final_confirm=True)
        ecs = ExecutionControlState(control_state="idle")
        ss = SessionState(schema_version=2, conversation=cs, task=tls, execution=ecs)

        _ = session_state_to_legacy_fields(ss)
        _ = session_state_from_legacy_snapshot(initial_dm_dict)

        post_dm_dict = copy.deepcopy(dm.export_snapshot())
        self.assertEqual(initial_dm_dict, post_dm_dict)

    # 15. Deep Immutability Tests
    def test_15_nested_last_mode_transition_is_immutable(self) -> None:
        cs = ConversationState(
            dialogue_mode="knowledge_qa",
            last_mode_transition=self.valid_transition,
        )
        self.assertIsInstance(cs.last_mode_transition, MappingProxyType)
        with self.assertRaises(TypeError):
            cs.last_mode_transition["to"] = "task_collection"  # type: ignore[index]

    def test_16_nested_transition_history_is_immutable(self) -> None:
        cs = ConversationState(
            dialogue_mode="knowledge_qa",
            mode_transition_history=[self.valid_transition],
        )
        self.assertIsInstance(cs.mode_transition_history[0], MappingProxyType)
        with self.assertRaises(TypeError):
            cs.mode_transition_history[0]["confidence"] = 0.5  # type: ignore[index]

    def test_17_nested_control_request_is_immutable(self) -> None:
        ecs = ExecutionControlState(
            control_state="stop_requested",
            last_control_request={"action": "stop", "status": "requested"},
        )
        self.assertIsInstance(ecs.last_control_request, MappingProxyType)
        with self.assertRaises(TypeError):
            ecs.last_control_request["action"] = "pause"  # type: ignore[index]

    # 18. Schema Version Fail Closed Tests
    def test_18_invalid_snapshot_version_fails_closed(self) -> None:
        invalid_versions = ["2", None, [], True, {}]
        for ver in invalid_versions:
            with self.subTest(version=ver):
                snap = {"snapshot_version": ver, "dialogue_mode": "task_collection"}
                with self.assertRaises(StateContractError):
                    session_state_from_legacy_snapshot(snap)

    def test_19_unsupported_schema_version_fails_closed(self) -> None:
        unsupported = [999, 1, -1, 0, 3]
        for ver in unsupported:
            with self.subTest(version=ver):
                snap = {"snapshot_version": ver, "dialogue_mode": "task_collection"}
                with self.assertRaises(StateContractError):
                    session_state_from_legacy_snapshot(snap)

                cs = ConversationState(dialogue_mode="task_collection")
                tls = TaskLifecycleState(phase="collecting", mode="normal", awaiting_final_confirm=False)
                ecs = ExecutionControlState(control_state="idle")
                with self.assertRaises(StateContractError):
                    SessionState(schema_version=ver, conversation=cs, task=tls, execution=ecs)


if __name__ == "__main__":
    unittest.main()
