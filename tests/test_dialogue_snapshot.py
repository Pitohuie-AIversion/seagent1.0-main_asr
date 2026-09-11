"""
tests/test_dialogue_snapshot.py

验证 DialogueSnapshotManager 独立会话快照持久化、模式迁移与状态恢复逻辑。
"""

import copy
import unittest
from unittest.mock import patch

from src.dialogue_manager import DialogueManager
from src.dialogue_snapshot import DialogueSnapshotManager
from src.slot_store import Slot, SnapshotValidationError


class TestDialogueSnapshotManager(unittest.TestCase):
    def setUp(self):
        self.dm = DialogueManager()
        self.snapshot_mgr = self.dm.snapshot_manager

    def test_manager_snapshot_manager_attachment(self):
        """验证 DialogueManager 默认持有并绑定 DialogueSnapshotManager 实例。"""
        self.assertIsInstance(self.snapshot_mgr, DialogueSnapshotManager)
        self.assertIs(self.snapshot_mgr.manager, self.dm)

    def test_export_snapshot_direct_and_delegated(self):
        """验证通过 snapshot_manager 导出与 dm.export_snapshot() 一致。"""
        snap_dm = self.dm.export_snapshot()
        snap_mgr = self.snapshot_mgr.export_snapshot()

        self.assertEqual(snap_dm["snapshot_version"], snap_mgr["snapshot_version"])
        self.assertEqual(snap_dm["session_id"], snap_mgr["session_id"])
        self.assertEqual(snap_dm["phase"], snap_mgr["phase"])
        self.assertEqual(snap_dm["mode"], snap_mgr["mode"])
        self.assertEqual(snap_dm["dialogue_mode"], snap_mgr["dialogue_mode"])
        self.assertEqual(snap_dm["control_state"], snap_mgr["control_state"])
        self.assertEqual(snap_dm["slot_store"], snap_mgr["slot_store"])

    def test_load_snapshot_in_place_invalid_inputs(self):
        """验证 load_snapshot_in_place 对非法快照结构的防御。"""
        candidate = DialogueManager()

        with self.assertRaises(ValueError) as ctx1:
            self.snapshot_mgr.load_snapshot_in_place(
                candidate, "not_a_dict", session_state_v2_active=False
            )
        self.assertIn("History snapshot must be a dictionary", str(ctx1.exception))

        with self.assertRaises(ValueError) as ctx2:
            self.snapshot_mgr.load_snapshot_in_place(
                candidate, {"conversation_history": "not_a_list"}, session_state_v2_active=False
            )
        self.assertIn("conversation_history must be a list", str(ctx2.exception))

        with self.assertRaises(ValueError) as ctx3:
            self.snapshot_mgr.load_snapshot_in_place(
                candidate, {"mode": "invalid_mode"}, session_state_v2_active=False
            )
        self.assertIn("Invalid task mode in snapshot", str(ctx3.exception))

        with self.assertRaises(ValueError) as ctx4:
            self.snapshot_mgr.load_snapshot_in_place(
                candidate, {"phase": "invalid_phase"}, session_state_v2_active=False
            )
        self.assertIn("Invalid task phase in snapshot", str(ctx4.exception))

    def test_load_snapshot_valid_legacy_and_commit(self):
        """验证合法快照被隔离加载并原子提交到目标 manager。"""
        snap = {
            "snapshot_version": 2,
            "session_id": "test-session-123",
            "phase": "collecting",
            "mode": "normal",
            "dialogue_mode": "task_collection",
            "control_state": "idle",
            "last_control_request": None,
            "slot_store": {
                "version": 1,
                "slots": {
                    "task_type": {
                        "slot_name": "task_type",
                        "value": "管缆巡检",
                        "status": "valid",
                        "value_type": "string",
                    }
                },
                "unresolved": [],
            },
            "task_state": {"task_type": "管缆巡检"},
        }

        new_dm = DialogueManager()
        new_dm.load_snapshot(snap)

        self.assertEqual(new_dm.session_id, "test-session-123")
        self.assertEqual(new_dm.phase, "collecting")
        self.assertEqual(new_dm.slot_store.get_task_state().get("task_type"), "管缆巡检")

    def test_commit_snapshot_runtime_state_atomic_transfer(self):
        """验证 commit_snapshot_runtime_state 运行时字段的原子转移。"""
        target = DialogueManager()
        candidate = DialogueManager()

        candidate.session_id = "candidate-id-999"
        candidate.phase = "confirming"
        candidate.mode = "expert"
        candidate.final_result = {"status": "success"}

        DialogueSnapshotManager.commit_snapshot_runtime_state(target, candidate)

        self.assertEqual(target.session_id, "candidate-id-999")
        self.assertEqual(target.phase, "confirming")
        self.assertEqual(target.mode, "expert")
        self.assertEqual(target.final_result, {"status": "success"})

    def test_dynamic_v2_active_resolution(self):
        """验证 _is_v2_active 能感知对 src.dialogue_manager.is_session_state_v2_enabled 的 patch。"""
        with patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=True):
            self.assertTrue(self.snapshot_mgr._is_v2_active())

        with patch("src.dialogue_manager.is_session_state_v2_enabled", return_value=False):
            self.assertFalse(self.snapshot_mgr._is_v2_active())


if __name__ == "__main__":
    unittest.main()
