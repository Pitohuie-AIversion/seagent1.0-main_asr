"""tests/test_session_state_runtime_v2.py

Unit and integration tests for SEAgent G3.2 SessionState Runtime Integration (session_state_v2 feature flag seam).
Validates runtime behavior under flag=false (legacy) and flag=true (SessionState contract seam),
atomic restore guarantees, legacy snapshot compatibility, and governance regression.
"""

import copy
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from src.dialogue_manager import DialogueManager
from src.model_profile import is_session_state_v2_enabled
from src.session_state import (
    SessionState,
    StateContractError,
)
from src.slot_store import SlotStore


class TestSessionStateRuntimeV2(unittest.TestCase):

    def setUp(self) -> None:
        self.dm = DialogueManager()
        self.valid_transition = {
            "from": "task_collection",
            "to": "knowledge_qa",
            "source": "fast_path",
            "confidence": 0.95,
            "reason": "General Q&A",
            "changed_at": datetime.now(timezone.utc).isoformat(),
        }

    @staticmethod
    def _runtime_fingerprint(dm: DialogueManager) -> dict:
        """Capture all snapshot-owned runtime state for fail-closed assertions."""
        return {
            "session_id": dm.session_id,
            "conversation_history": copy.deepcopy(dm.conversation_history),
            "slot_store": copy.deepcopy(dm.slot_store.export_snapshot()),
            "task_state": copy.deepcopy(dm.task_state),
            "mode": dm.mode,
            "phase": dm.phase,
            "final_result": copy.deepcopy(dm.final_result),
            "awaiting_final_confirm": dm.awaiting_final_confirm,
            "task_start_now": dm.task_start_now,
            "blocking_violations": copy.deepcopy(dm._blocking_violations),
            "soft_whitelist": copy.deepcopy(dm._soft_whitelist),
            "hard_refusal_counts": copy.deepcopy(dm._hard_refusal_counts),
            "pending_rov_candidates": copy.deepcopy(dm._pending_rov_candidates),
            "last_built_json": copy.deepcopy(dm._last_built_json),
            "last_missing": copy.deepcopy(dm._last_missing),
            "control_state": dm.control_state,
            "last_control_request": copy.deepcopy(dm.last_control_request),
            "dialogue_mode": dm.dialogue_mode,
            "last_mode_transition": copy.deepcopy(dm.last_mode_transition),
            "mode_transition_history": copy.deepcopy(dm.mode_transition_history),
        }

    # 1. flag=false export snapshot legacy behavior unchanged
    @patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=False)
    def test_01_flag_false_export_snapshot_legacy_behavior_unchanged(self, mock_flag) -> None:
        snap = self.dm.export_snapshot()
        self.assertEqual(snap["snapshot_version"], 2)
        self.assertEqual(snap["phase"], "collecting")
        self.assertEqual(snap["dialogue_mode"], "task_collection")
        self.assertEqual(snap["control_state"], "idle")
        self.assertIn("slot_store", snap)
        self.assertIn("task_state", snap)

    # 2. flag=true valid current state can build SessionState
    @patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=True)
    def test_02_flag_true_valid_current_state_can_build_session_state(self, mock_flag) -> None:
        state = self.dm._build_session_state_contract()
        self.assertIsInstance(state, SessionState)
        self.assertEqual(state.schema_version, 2)
        self.assertEqual(state.task.phase, "collecting")
        self.assertEqual(state.conversation.dialogue_mode, "task_collection")
        self.assertEqual(state.execution.control_state, "idle")

    # 3. flag=true export snapshot remains legacy-compatible
    @patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=True)
    def test_03_flag_true_export_snapshot_remains_legacy_compatible(self, mock_flag) -> None:
        snap = self.dm.export_snapshot()
        self.assertEqual(snap["snapshot_version"], 2)
        self.assertEqual(snap["phase"], "collecting")
        self.assertEqual(snap["dialogue_mode"], "task_collection")
        self.assertEqual(snap["control_state"], "idle")
        self.assertIn("slot_store", snap)
        self.assertIn("task_state", snap)

    # 4. flag=true invalid Runtime phase fails closed
    @patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=True)
    def test_04_flag_true_invalid_runtime_phase_fails_closed(self, mock_flag) -> None:
        self.dm.phase = "invalid_phase_name"
        with self.assertRaises(StateContractError):
            self.dm.export_snapshot()

    # 5. flag=true invalid dialogue_mode fails closed
    @patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=True)
    def test_05_flag_true_invalid_dialogue_mode_fails_closed(self, mock_flag) -> None:
        self.dm.dialogue_mode = "invalid_mode_name"
        with self.assertRaises(StateContractError):
            self.dm.export_snapshot()

        with self.assertRaises(StateContractError):
            self.dm._switch_dialogue_mode("invalid_mode_name")

    # 6. flag=true invalid control_state / request combination fails closed
    @patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=True)
    def test_06_flag_true_invalid_control_state_combination_fails_closed(self, mock_flag) -> None:
        self.dm.control_state = "stop_requested"
        self.dm.last_control_request = None  # Mismatched control_state and last_control_request
        with self.assertRaises(StateContractError):
            self.dm.export_snapshot()

    # 7. flag=true valid snapshot load effect equals Legacy
    def test_07_flag_true_valid_snapshot_load_effect_equals_legacy(self) -> None:
        snap = self.dm.export_snapshot()

        dm_legacy = DialogueManager()
        dm_v2 = DialogueManager()

        with patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=False):
            dm_legacy.load_snapshot(snap)

        with patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=True):
            dm_v2.load_snapshot(snap)

        self.assertEqual(dm_legacy.phase, dm_v2.phase)
        self.assertEqual(dm_legacy.mode, dm_v2.mode)
        self.assertEqual(dm_legacy.dialogue_mode, dm_v2.dialogue_mode)
        self.assertEqual(dm_legacy.control_state, dm_v2.control_state)

    # 8. flag=true invalid SessionState snapshot does not partially mutate manager
    @patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=True)
    def test_08_flag_true_invalid_session_state_snapshot_does_not_partially_mutate_manager(self, mock_flag) -> None:
        initial_phase = self.dm.phase
        initial_mode = self.dm.mode
        initial_dm = self.dm.dialogue_mode
        initial_ctrl = self.dm.control_state
        initial_store_snap = copy.deepcopy(self.dm.slot_store.export_snapshot())
        initial_task_state = copy.deepcopy(self.dm.task_state)

        invalid_snapshot = {
            "snapshot_version": 2,
            "phase": "invalid_phase_name",  # Invalid phase!
            "mode": "normal",
            "dialogue_mode": "task_collection",
            "control_state": "idle",
            "slot_store": {"dummy": "value"},
        }

        with self.assertRaises(StateContractError):
            self.dm.load_snapshot(invalid_snapshot)

        # Assert zero partial mutation
        self.assertEqual(self.dm.phase, initial_phase)
        self.assertEqual(self.dm.mode, initial_mode)
        self.assertEqual(self.dm.dialogue_mode, initial_dm)
        self.assertEqual(self.dm.control_state, initial_ctrl)
        self.assertEqual(self.dm.slot_store.export_snapshot(), initial_store_snap)
        self.assertEqual(self.dm.task_state, initial_task_state)

    # 9. legacy "uncertain" dialogue_mode mapping still compatible
    @patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=True)
    def test_09_legacy_uncertain_dialogue_mode_mapping_still_compatible(self, mock_flag) -> None:
        snap = {
            "snapshot_version": 2,
            "phase": "collecting",
            "mode": "normal",
            "dialogue_mode": "uncertain",  # Legacy mode -> mapped to 'knowledge_qa'
            "control_state": "idle",
        }
        self.dm.load_snapshot(snap)
        self.assertEqual(self.dm.dialogue_mode, "knowledge_qa")

    # 10. done-phase restore still performs existing final-file validation
    @patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=True)
    def test_10_done_phase_restore_still_performs_existing_final_file_validation(self, mock_flag) -> None:
        snap = {
            "snapshot_version": 2,
            "phase": "done",
            "mode": "normal",
            "dialogue_mode": "task_collection",
            "control_state": "idle",
        }
        # Final file for intent_id does not exist, so done phase fallback to collecting
        self.dm.load_snapshot(snap)
        self.assertEqual(self.dm.phase, "collecting")

    # 11. SlotStore restore effect unchanged
    @patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=True)
    def test_11_slot_store_restore_effect_unchanged(self, mock_flag) -> None:
        store = SlotStore(self.dm.kb)
        store.init_task_slots(self.dm.builder.get_schema("pipeline_inspection", "normal"))
        if "task_type_key" in store.slots:
            store.slots["task_type_key"].value = "pipeline_inspection"
            store.slots["task_type_key"].status = "valid"
        store_snap = store.export_snapshot()

        snap = {
            "snapshot_version": 2,
            "phase": "collecting",
            "mode": "normal",
            "dialogue_mode": "task_collection",
            "control_state": "idle",
            "slot_store": store_snap,
        }
        self.dm.load_snapshot(snap)
        self.assertEqual(self.dm.task_state.get("task_type_key"), "pipeline_inspection")

    # 12. ordinary knowledge/general chat regression
    @patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=True)
    def test_12_ordinary_knowledge_general_chat_regression(self, mock_flag) -> None:
        resp = self.dm.process("你好")
        self.assertIsInstance(resp, str)
        self.assertIn("水下", resp)
        self.assertEqual(self.dm.dialogue_mode, "knowledge_qa")

    # 13. blocked_soft / blocked_hard phase regression
    @patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=True)
    def test_13_blocked_soft_blocked_hard_phase_regression(self, mock_flag) -> None:
        self.dm.phase = "blocked_soft"
        state = self.dm._build_session_state_contract()
        self.assertEqual(state.task.phase, "blocked_soft")

        self.dm.phase = "blocked_hard"
        state_hard = self.dm._build_session_state_contract()
        self.assertEqual(state_hard.task.phase, "blocked_hard")

    # 14. session_state_v2=false default verified
    def test_14_session_state_v2_false_default_verified(self) -> None:
        self.assertFalse(is_session_state_v2_enabled())

    # 15. flag=false invalid task phase fails closed without mutating runtime state
    @patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=False)
    def test_15_flag_false_invalid_phase_does_not_mutate_manager(self, mock_flag) -> None:
        before = self._runtime_fingerprint(self.dm)
        invalid_snapshot = self.dm.export_snapshot()
        invalid_snapshot["phase"] = "invalid_phase_name"

        with self.assertRaises(ValueError):
            self.dm.load_snapshot(invalid_snapshot)

        self.assertEqual(self._runtime_fingerprint(self.dm), before)

    # 16. flag=false invalid task mode fails closed without mutating runtime state
    @patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=False)
    def test_16_flag_false_invalid_task_mode_does_not_mutate_manager(self, mock_flag) -> None:
        before = self._runtime_fingerprint(self.dm)
        invalid_snapshot = self.dm.export_snapshot()
        invalid_snapshot["mode"] = "invalid_task_mode"

        with self.assertRaises(ValueError):
            self.dm.load_snapshot(invalid_snapshot)

        self.assertEqual(self._runtime_fingerprint(self.dm), before)

    # 17. a late field validation error cannot leak an earlier session_id assignment
    @patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=False)
    def test_17_failed_restore_does_not_partially_replace_session_id(self, mock_flag) -> None:
        self.dm.session_id = "original-session"
        self.dm.conversation_history = [{"role": "user", "content": "保留当前会话"}]
        before = self._runtime_fingerprint(self.dm)
        invalid_snapshot = self.dm.export_snapshot()
        invalid_snapshot["session_id"] = "replacement-session"
        invalid_snapshot["conversation_history"] = "not-a-list"

        with self.assertRaisesRegex(ValueError, "conversation_history must be a list"):
            self.dm.load_snapshot(invalid_snapshot)

        self.assertEqual(self._runtime_fingerprint(self.dm), before)


if __name__ == "__main__":
    unittest.main()
