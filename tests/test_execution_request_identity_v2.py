"""tests/test_execution_request_identity_v2.py

Unit, integration, and cross-domain tests for SEAgent G3.4-B Execution Request Identity & Lifecycle Repair.
Verifies target identity binding (target_intent_id, target_task_id, target_internal_id),
runtime context legality enforcement, and identity preservation across task modifications and snapshot restores.
"""

import copy
import unittest
from unittest.mock import patch, MagicMock

from src.dialogue_manager import DialogueManager
from src.session_state import (
    ExecutionControlState,
    StateContractError,
    session_state_from_legacy_snapshot,
)
from src.slot_store import Slot


class TestExecutionRequestIdentityV2(unittest.TestCase):

    def setUp(self) -> None:
        self.dm = DialogueManager()
        self.valid_req = {
            "action": "stop",
            "status": "requested",
            "target_intent_id": "TI20260809001",
            "target_task_id": "PI-20260809-001",
            "target_internal_id": "123e4567-e89b-42d3-a456-426614174000",
            "source": "rule",
            "confidence": 1.0,
            "reason": "test",
        }

    def _setup_published_task(
        self,
        intent_id: str = "TI20260809001",
        task_id: str = "PI-20260809-001",
        internal_id: str = "123e4567-e89b-42d3-a456-426614174000",
    ) -> None:
        schema = self.dm.builder.get_schema("pipeline_inspection", self.dm.mode)
        self.dm.slot_store.init_task_slots(schema)
        slots = self.dm.slot_store.clone_slots()
        slots["task_type"] = Slot("task_type", value="管缆巡检", status="valid")
        slots["task_type_key"] = Slot("task_type_key", value="pipeline_inspection", status="valid")
        slots["water_depth"] = Slot("water_depth", value=300.0, status="valid")
        slots["task_id"] = Slot("task_id", value=task_id, status="valid")
        slots["intent_id"] = Slot("intent_id", value=intent_id, status="valid")
        slots["internal_id"] = Slot("internal_id", value=internal_id, status="valid")
        self.dm.slot_store.commit_transaction(slots, [])
        self.dm.task_state = self.dm.slot_store.get_task_state()
        self.dm._transition_phase("confirming", reason="test_setup")
        self.dm._transition_phase("done", reason="test_setup")

    def _modify_published_task(self) -> None:
        slots_mod = self.dm.slot_store.clone_slots()
        slots_mod["water_depth"] = Slot("water_depth", value=500.0, status="valid")
        slots_mod["intent_id"] = Slot("intent_id", value=None, status="unfilled")
        self.dm.slot_store.commit_transaction(slots_mod, [])
        self.dm.task_state = self.dm.slot_store.get_task_state()
        self.dm.final_result = None
        self.dm._transition_phase("confirming", reason="task_modified")

    # --------------------------------------------------------------------------
    # A. Identity Contract
    # --------------------------------------------------------------------------

    # 1. published stop request contains target identity
    def test_01_published_stop_request_contains_target_identity(self) -> None:
        ecs = ExecutionControlState("stop_requested", self.valid_req)
        self.assertEqual(ecs.last_control_request["target_intent_id"], "TI20260809001")

    # 2. pause request contains target identity
    def test_02_pause_request_contains_target_identity(self) -> None:
        req = copy.deepcopy(self.valid_req)
        req["action"] = "pause"
        ecs = ExecutionControlState("pause_requested", req)
        self.assertEqual(ecs.last_control_request["target_intent_id"], "TI20260809001")

    # 3. abort request contains target identity
    def test_03_abort_request_contains_target_identity(self) -> None:
        req = copy.deepcopy(self.valid_req)
        req["action"] = "abort"
        ecs = ExecutionControlState("abort_requested", req)
        self.assertEqual(ecs.last_control_request["target_intent_id"], "TI20260809001")

    # 4. cancel request contains target identity
    def test_04_cancel_request_contains_target_identity(self) -> None:
        req = copy.deepcopy(self.valid_req)
        req["action"] = "cancel"
        ecs = ExecutionControlState("cancel_requested", req)
        self.assertEqual(ecs.last_control_request["target_intent_id"], "TI20260809001")

    # 5. non-idle request missing target identity fails closed
    def test_05_non_idle_request_missing_target_identity_fails_closed(self) -> None:
        req = copy.deepcopy(self.valid_req)
        del req["target_intent_id"]
        with self.assertRaises(StateContractError):
            ExecutionControlState("stop_requested", req)

    # 6. target identity invalid format fails closed
    def test_06_target_identity_invalid_format_fails_closed(self) -> None:
        req = copy.deepcopy(self.valid_req)
        req["target_intent_id"] = "INVALID_INTENT_ID"
        with self.assertRaises(StateContractError):
            ExecutionControlState("stop_requested", req)

    # 7. action/state mismatch fails closed
    def test_07_action_state_mismatch_fails_closed(self) -> None:
        with self.assertRaises(StateContractError):
            ExecutionControlState("pause_requested", self.valid_req)

    # 8. validation failure zero partial mutation
    @patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=True)
    def test_08_validation_failure_zero_partial_mutation(self, mock_flag) -> None:
        self._setup_published_task()
        initial_ctrl = self.dm.control_state
        initial_req = copy.deepcopy(self.dm.last_control_request)

        req_invalid = copy.deepcopy(self.valid_req)
        req_invalid["target_intent_id"] = "BAD_ID"

        with self.assertRaises(StateContractError):
            self.dm._set_execution_control_state("stop_requested", req_invalid)

        self.assertEqual(self.dm.control_state, initial_ctrl)
        self.assertEqual(self.dm.last_control_request, initial_req)

    # --------------------------------------------------------------------------
    # B. Runtime Context Legality
    # --------------------------------------------------------------------------

    # 9. collecting phase execution owner fails closed when flag=true
    @patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=True)
    def test_09_collecting_phase_execution_owner_fails_closed(self, mock_flag) -> None:
        self.dm.phase = "collecting"
        with self.assertRaises(StateContractError):
            self.dm._set_execution_control_state("stop_requested", self.valid_req)
        self.assertEqual(self.dm.control_state, "idle")

    # 10. confirming phase execution owner fails closed when flag=true
    @patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=True)
    def test_10_confirming_phase_execution_owner_fails_closed(self, mock_flag) -> None:
        self.dm.phase = "confirming"
        with self.assertRaises(StateContractError):
            self.dm._set_execution_control_state("stop_requested", self.valid_req)
        self.assertEqual(self.dm.control_state, "idle")

    # 11. blocked_soft phase execution owner fails closed when flag=true
    @patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=True)
    def test_11_blocked_soft_phase_execution_owner_fails_closed(self, mock_flag) -> None:
        self.dm.phase = "blocked_soft"
        with self.assertRaises(StateContractError):
            self.dm._set_execution_control_state("stop_requested", self.valid_req)
        self.assertEqual(self.dm.control_state, "idle")

    # 12. blocked_hard phase execution owner fails closed when flag=true
    @patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=True)
    def test_12_blocked_hard_phase_execution_owner_fails_closed(self, mock_flag) -> None:
        self.dm.phase = "blocked_hard"
        with self.assertRaises(StateContractError):
            self.dm._set_execution_control_state("stop_requested", self.valid_req)
        self.assertEqual(self.dm.control_state, "idle")

    # 13. rejected phase execution owner fails closed when flag=true
    @patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=True)
    def test_13_rejected_phase_execution_owner_fails_closed(self, mock_flag) -> None:
        self.dm.phase = "rejected"
        with self.assertRaises(StateContractError):
            self.dm._set_execution_control_state("stop_requested", self.valid_req)
        self.assertEqual(self.dm.control_state, "idle")

    # 14. done phase valid request succeeds
    @patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=True)
    def test_14_done_phase_valid_request_succeeds(self, mock_flag) -> None:
        self._setup_published_task()
        self.dm._set_execution_control_state("stop_requested", self.valid_req)
        self.assertEqual(self.dm.control_state, "stop_requested")
        self.assertEqual(self.dm.last_control_request["target_intent_id"], "TI20260809001")

    # --------------------------------------------------------------------------
    # C. Real Handler Flow
    # --------------------------------------------------------------------------

    # 15. real done task stop binds target identity
    def test_15_real_done_task_stop_binds_target_identity(self) -> None:
        self._setup_published_task("TI20260809001", "PI-20260809-001", "123e4567-e89b-42d3-a456-426614174000")
        route_mock = MagicMock()
        route_mock.emergency_action = "stop"
        route_mock.source = "rule"
        route_mock.confidence = 1.0
        route_mock.reason = "user_command"

        self.dm._handle_emergency_intervention("停止当前任务", route_mock)
        self.assertEqual(self.dm.control_state, "stop_requested")
        self.assertEqual(self.dm.last_control_request["target_intent_id"], "TI20260809001")
        self.assertEqual(self.dm.last_control_request["target_task_id"], "PI-20260809-001")

    # 16. real done task pause binds target identity
    def test_16_real_done_task_pause_binds_target_identity(self) -> None:
        self._setup_published_task("TI20260809001", "PI-20260809-001", "123e4567-e89b-42d3-a456-426614174000")
        route_mock = MagicMock()
        route_mock.emergency_action = "pause"
        route_mock.source = "rule"
        route_mock.confidence = 1.0
        route_mock.reason = "user_command"

        self.dm._handle_emergency_intervention("暂停当前任务", route_mock)
        self.assertEqual(self.dm.control_state, "pause_requested")
        self.assertEqual(self.dm.last_control_request["target_intent_id"], "TI20260809001")

    # 17. real done task abort binds target identity
    def test_17_real_done_task_abort_binds_target_identity(self) -> None:
        self._setup_published_task("TI20260809001", "PI-20260809-001", "123e4567-e89b-42d3-a456-426614174000")
        route_mock = MagicMock()
        route_mock.emergency_action = "abort"
        route_mock.source = "rule"
        route_mock.confidence = 1.0
        route_mock.reason = "user_command"

        self.dm._handle_emergency_intervention("终止当前任务", route_mock)
        self.assertEqual(self.dm.control_state, "abort_requested")
        self.assertEqual(self.dm.last_control_request["target_intent_id"], "TI20260809001")

    # 18. real done task cancel binds target identity
    def test_18_real_done_task_cancel_binds_target_identity(self) -> None:
        self._setup_published_task("TI20260809001", "PI-20260809-001", "123e4567-e89b-42d3-a456-426614174000")
        route_mock = MagicMock()
        route_mock.emergency_action = "cancel"
        route_mock.source = "rule"
        route_mock.confidence = 1.0
        route_mock.reason = "user_command"

        self.dm._handle_emergency_intervention("取消已发布任务", route_mock)
        self.assertEqual(self.dm.control_state, "cancel_requested")
        self.assertEqual(self.dm.last_control_request["target_intent_id"], "TI20260809001")

    # --------------------------------------------------------------------------
    # D. Cross-Domain Identity
    # --------------------------------------------------------------------------

    # 19. Publish task A obtains identity A
    def test_19_publish_task_A_obtains_identity_A(self) -> None:
        self._setup_published_task("TI20260809001", "PI-20260809-001", "123e4567-e89b-42d3-a456-426614174000")
        self.assertEqual(self.dm.task_state.get("intent_id"), "TI20260809001")

    # 20. Task A stop request binds target identity A
    def test_20_task_A_stop_request_binds_target_identity_A(self) -> None:
        self._setup_published_task("TI20260809001", "PI-20260809-001", "123e4567-e89b-42d3-a456-426614174000")
        route_mock = MagicMock()
        route_mock.emergency_action = "stop"
        route_mock.source = "rule"
        route_mock.confidence = 1.0
        route_mock.reason = "user_command"
        self.dm._handle_emergency_intervention("停止当前任务", route_mock)

        self.assertEqual(self.dm.last_control_request["target_intent_id"], "TI20260809001")

    # 21. Modify task A creates new draft B
    def test_21_modify_task_A_creates_new_draft_B(self) -> None:
        self._setup_published_task("TI20260809001", "PI-20260809-001", "123e4567-e89b-42d3-a456-426614174000")
        self._modify_published_task()
        self.assertIn(self.dm.phase, ("confirming", "collecting"))

    # 22. New draft B intent_id is not yet published intent_id A
    def test_22_new_draft_B_identity_distinct_from_A(self) -> None:
        self._setup_published_task("TI20260809001", "PI-20260809-001", "123e4567-e89b-42d3-a456-426614174000")
        self._modify_published_task()
        current_intent = self.dm.task_state.get("intent_id")
        self.assertNotEqual(current_intent, "TI20260809001")

    # 23. Control request target identity remains A
    def test_23_control_request_target_identity_remains_A(self) -> None:
        self._setup_published_task("TI20260809001", "PI-20260809-001", "123e4567-e89b-42d3-a456-426614174000")
        route_mock = MagicMock()
        route_mock.emergency_action = "stop"
        route_mock.source = "rule"
        route_mock.confidence = 1.0
        route_mock.reason = "user_command"
        self.dm._handle_emergency_intervention("停止当前任务", route_mock)

        self._modify_published_task()
        self.assertEqual(self.dm.control_state, "stop_requested")
        self.assertEqual(self.dm.last_control_request["target_intent_id"], "TI20260809001")

    # 24. Control request target identity does not equal draft B identity
    def test_24_control_request_target_identity_does_not_equal_B(self) -> None:
        self._setup_published_task("TI20260809001", "PI-20260809-001", "123e4567-e89b-42d3-a456-426614174000")
        route_mock = MagicMock()
        route_mock.emergency_action = "stop"
        route_mock.source = "rule"
        route_mock.confidence = 1.0
        route_mock.reason = "user_command"
        self.dm._handle_emergency_intervention("停止当前任务", route_mock)

        self._modify_published_task()
        draft_intent = self.dm.task_state.get("intent_id")
        target_intent = self.dm.last_control_request["target_intent_id"]
        self.assertNotEqual(target_intent, draft_intent)

    # 25. Current phase becomes confirming or collecting
    def test_25_current_phase_becomes_confirming_or_collecting(self) -> None:
        self._setup_published_task("TI20260809001", "PI-20260809-001", "123e4567-e89b-42d3-a456-426614174000")
        self._modify_published_task()
        self.assertIn(self.dm.phase, ("confirming", "collecting"))

    # 26. Control request target identity preserved without loss
    def test_26_control_request_target_identity_preserved_without_loss(self) -> None:
        self._setup_published_task("TI20260809001", "PI-20260809-001", "123e4567-e89b-42d3-a456-426614174000")
        route_mock = MagicMock()
        route_mock.emergency_action = "pause"
        route_mock.source = "rule"
        route_mock.confidence = 1.0
        route_mock.reason = "user_command"
        self.dm._handle_emergency_intervention("暂停当前任务", route_mock)

        self._modify_published_task()
        self.assertEqual(self.dm.last_control_request["target_intent_id"], "TI20260809001")

    # --------------------------------------------------------------------------
    # E. Snapshot & Reset
    # --------------------------------------------------------------------------

    # 27. Target identity export correct
    def test_27_target_identity_export_correct(self) -> None:
        self._setup_published_task("TI20260809001", "PI-20260809-001", "123e4567-e89b-42d3-a456-426614174000")
        route_mock = MagicMock()
        route_mock.emergency_action = "stop"
        route_mock.source = "rule"
        route_mock.confidence = 1.0
        route_mock.reason = "user_command"
        self.dm._handle_emergency_intervention("停止当前任务", route_mock)

        snap = self.dm.export_snapshot()
        req_snap = snap["last_control_request"]
        self.assertEqual(req_snap["target_intent_id"], "TI20260809001")

    # 28. Snapshot restore preserves target identity
    def test_28_snapshot_restore_preserves_target_identity(self) -> None:
        self._setup_published_task("TI20260809001", "PI-20260809-001", "123e4567-e89b-42d3-a456-426614174000")
        route_mock = MagicMock()
        route_mock.emergency_action = "stop"
        route_mock.source = "rule"
        route_mock.confidence = 1.0
        route_mock.reason = "user_command"
        self.dm._handle_emergency_intervention("停止当前任务", route_mock)

        snap = self.dm.export_snapshot()
        dm2 = DialogueManager()
        dm2.load_snapshot(snap)
        self.assertEqual(dm2.last_control_request["target_intent_id"], "TI20260809001")

    # 29. Legacy snapshot without target identity handling
    def test_29_legacy_snapshot_without_target_identity_handling(self) -> None:
        legacy_snap = {
            "snapshot_version": 2,
            "phase": "done",
            "mode": "normal",
            "control_state": "stop_requested",
            "last_control_request": {
                "action": "stop",
                "status": "requested",
                "source": "rule",
                "confidence": 1.0,
                "reason": "legacy",
            },
            "task_state": {
                "intent_id": "TI20260809001",
                "task_id": "PI-20260809-001",
            },
            "slots": {},
        }
        state = session_state_from_legacy_snapshot(legacy_snap)
        self.assertEqual(state.execution.last_control_request["target_intent_id"], "TI20260809001")

    # 30. Reset resets execution control state to idle
    def test_30_reset_resets_execution_control_state_to_idle(self) -> None:
        self._setup_published_task()
        route_mock = MagicMock()
        route_mock.emergency_action = "stop"
        route_mock.source = "rule"
        route_mock.confidence = 1.0
        route_mock.reason = "user_command"
        self.dm._handle_emergency_intervention("停止当前任务", route_mock)

        self.dm.reset()
        self.assertEqual(self.dm.control_state, "idle")
        self.assertIsNone(self.dm.last_control_request)

    # --------------------------------------------------------------------------
    # F. Legacy Snapshot Migration Policy (G3.4-B Repair)
    # --------------------------------------------------------------------------

    # 31. Safe legacy done migration
    def test_31_safe_legacy_done_migration(self) -> None:
        snap = {
            "snapshot_version": 2,
            "phase": "done",
            "mode": "normal",
            "control_state": "stop_requested",
            "last_control_request": {
                "action": "stop",
                "status": "requested",
                "source": "rule",
                "confidence": 1.0,
                "reason": "legacy",
            },
            "task_state": {
                "intent_id": "TI20260809001",
                "task_id": "PI-20260809-001",
                "internal_id": "123e4567-e89b-42d3-a456-426614174000",
            },
        }
        state = session_state_from_legacy_snapshot(snap)
        self.assertEqual(state.execution.last_control_request["target_intent_id"], "TI20260809001")

    # 32. Confirming ambiguous migration fails closed
    def test_32_confirming_ambiguous_migration_fails_closed(self) -> None:
        snap = {
            "snapshot_version": 2,
            "phase": "confirming",
            "mode": "normal",
            "control_state": "stop_requested",
            "last_control_request": {
                "action": "stop",
                "status": "requested",
                "source": "rule",
                "confidence": 1.0,
                "reason": "legacy",
            },
            "task_state": {
                "intent_id": "TI20260809002",
            },
        }
        with self.assertRaises(StateContractError) as ctx:
            session_state_from_legacy_snapshot(snap)
        self.assertIn("confirming", str(ctx.exception))

    # 33. Collecting ambiguous migration fails closed
    def test_33_collecting_ambiguous_migration_fails_closed(self) -> None:
        snap = {
            "snapshot_version": 2,
            "phase": "collecting",
            "mode": "normal",
            "control_state": "stop_requested",
            "last_control_request": {
                "action": "stop",
                "status": "requested",
            },
            "task_state": {"intent_id": "TI20260809002"},
        }
        with self.assertRaises(StateContractError):
            session_state_from_legacy_snapshot(snap)

    # 34. Blocked_soft ambiguous migration fails closed
    def test_34_blocked_soft_ambiguous_migration_fails_closed(self) -> None:
        snap = {
            "snapshot_version": 2,
            "phase": "blocked_soft",
            "mode": "normal",
            "control_state": "pause_requested",
            "last_control_request": {
                "action": "pause",
                "status": "requested",
            },
            "task_state": {"intent_id": "TI20260809002"},
        }
        with self.assertRaises(StateContractError):
            session_state_from_legacy_snapshot(snap)

    # 35. Blocked_hard ambiguous migration fails closed
    def test_35_blocked_hard_ambiguous_migration_fails_closed(self) -> None:
        snap = {
            "snapshot_version": 2,
            "phase": "blocked_hard",
            "mode": "normal",
            "control_state": "abort_requested",
            "last_control_request": {
                "action": "abort",
                "status": "requested",
            },
            "task_state": {"intent_id": "TI20260809002"},
        }
        with self.assertRaises(StateContractError):
            session_state_from_legacy_snapshot(snap)

    # 36. Rejected ambiguous migration fails closed
    def test_36_rejected_ambiguous_migration_fails_closed(self) -> None:
        snap = {
            "snapshot_version": 2,
            "phase": "rejected",
            "mode": "normal",
            "control_state": "cancel_requested",
            "last_control_request": {
                "action": "cancel",
                "status": "requested",
            },
            "task_state": {"intent_id": "TI20260809002"},
        }
        with self.assertRaises(StateContractError):
            session_state_from_legacy_snapshot(snap)

    # 37. Done phase missing or invalid intent_id fails closed
    def test_37_done_phase_missing_or_invalid_intent_id_fails_closed(self) -> None:
        snap = {
            "snapshot_version": 2,
            "phase": "done",
            "mode": "normal",
            "control_state": "stop_requested",
            "last_control_request": {
                "action": "stop",
                "status": "requested",
            },
            "task_state": {"intent_id": "INVALID_INTENT_ID"},
        }
        with self.assertRaises(StateContractError):
            session_state_from_legacy_snapshot(snap)

    # 38. Existing target wins over task_state in confirming phase
    def test_38_existing_target_wins_over_task_state(self) -> None:
        snap = {
            "snapshot_version": 2,
            "phase": "confirming",
            "mode": "normal",
            "control_state": "stop_requested",
            "last_control_request": {
                "action": "stop",
                "status": "requested",
                "target_intent_id": "TI20260809001",
            },
            "task_state": {"intent_id": "TI20260809002"},
        }
        state = session_state_from_legacy_snapshot(snap)
        self.assertEqual(state.execution.last_control_request["target_intent_id"], "TI20260809001")

    # 39. Existing target wins in done phase
    def test_39_existing_target_wins_in_done_phase(self) -> None:
        snap = {
            "snapshot_version": 2,
            "phase": "done",
            "mode": "normal",
            "control_state": "stop_requested",
            "last_control_request": {
                "action": "stop",
                "status": "requested",
                "target_intent_id": "TI20260809001",
            },
            "task_state": {"intent_id": "TI20260809002"},
        }
        state = session_state_from_legacy_snapshot(snap)
        self.assertEqual(state.execution.last_control_request["target_intent_id"], "TI20260809001")

    # 40. Optional audit IDs handled safely
    def test_40_optional_audit_ids_handled_safely(self) -> None:
        snap = {
            "snapshot_version": 2,
            "phase": "done",
            "mode": "normal",
            "control_state": "stop_requested",
            "last_control_request": {
                "action": "stop",
                "status": "requested",
            },
            "task_state": {
                "intent_id": "TI20260809001",
                "task_id": "PI-20260809-001",
                "internal_id": "123e4567-e89b-42d3-a456-426614174000",
            },
        }
        state = session_state_from_legacy_snapshot(snap)
        req = state.execution.last_control_request
        self.assertEqual(req["target_intent_id"], "TI20260809001")
        self.assertEqual(req["target_task_id"], "PI-20260809-001")
        self.assertEqual(req["target_internal_id"], "123e4567-e89b-42d3-a456-426614174000")


if __name__ == "__main__":
    unittest.main()
