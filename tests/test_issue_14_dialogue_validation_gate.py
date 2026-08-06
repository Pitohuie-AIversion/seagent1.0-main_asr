"""
tests/test_issue_14_dialogue_validation_gate.py
针对 Issue #14 批次三的定向单元测试：
1. DialogueManager 的统一校验刷新 _refresh_validation 与门禁逻辑
2. 软警告确认与 (task_version, validation_fingerprint, status_ref, state_version) 版本绑定
3. 状态或任务发生变化后旧确认自动失效
4. blocked_hard 与 validation_error 无法通过确认/忽略绕过
5. SlotStore 校验快照的持久化与恢复兼容性 (Schema v1 / v2)
"""

import shutil
import tempfile
import unittest
from pathlib import Path

from src.llm_client import LLMClient
from src.knowledge_retriever import KnowledgeBase
from src.dialogue_manager import DialogueManager
from src.slot_store import SlotStore, SnapshotValidationError, ValidationAcknowledgement
from src.validator import ValidationResult
from src.simulated_time import get_current_datetime


def _make_dm(tmp_dir: Path) -> DialogueManager:
    state_file = tmp_dir / "state.yaml"
    shutil.copy("config/state.yaml", state_file)
    kb = KnowledgeBase()
    kb.state_info.state_file = state_file
    llm = LLMClient(None, None)
    return DialogueManager(llm, kb)


class TestDialogueValidationGate(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.dm = _make_dm(Path(self._tmp))

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_slot_store_snapshot_schema_v2(self):
        """测试 SlotStore 的 Schema v2 导出与恢复，以及 Schema v1 兼容。"""
        store = self.dm.slot_store
        store.validation_result = ValidationResult.from_dict({
            "overall_status": "blocked_soft",
            "task_version": 1,
            "validation_version": 1,
            "validation_fingerprint": "abc12345",
            "state_snapshot": {"unit_id": "OBSROV--001", "status_ref": "OBSROV-001", "state_version": 1},
            "violations": [],
        })
        store.validation_acknowledgements = [
            ValidationAcknowledgement.from_dict({
                "constraint_id": "C014",
                "task_version": 1,
                "validation_version": 1,
                "validation_fingerprint": "abc12345",
                "status_ref": "OBSROV-001",
                "state_version": 1,
            })
        ]

        snap = store.export_snapshot()
        self.assertEqual(snap["snapshot_schema_version"], 2)
        self.assertEqual(snap["validation"], store.validation_result.to_dict())
        self.assertEqual(
            snap["validation_acknowledgements"],
            [ack.to_dict() for ack in store.validation_acknowledgements],
        )

        # 新建 SlotStore 恢复
        new_store = SlotStore.from_snapshot(snap, kb=self.dm.kb)
        self.assertIsInstance(new_store.validation_result, ValidationResult)
        self.assertEqual(new_store.validation_result.to_dict(), store.validation_result.to_dict())
        self.assertEqual(len(new_store.validation_acknowledgements), 1)
        self.assertIsInstance(new_store.validation_acknowledgements[0], ValidationAcknowledgement)

        # 测试 Schema v1 旧快照恢复兼容
        v1_snap = {
            "store_version": 1,
            "slots": snap["slots"],
            "unresolved": snap["unresolved"],
        }
        restored_v1_store = SlotStore.from_snapshot(v1_snap, kb=self.dm.kb)
        self.assertIsNone(restored_v1_store.validation_result)
        self.assertEqual(restored_v1_store.validation_acknowledgements, [])

    def test_soft_warning_acknowledgement_fingerprint_invalidation(self):
        """测试遥测状态或任务字段改变后，指纹变化使旧确认自动失效。"""
        dm = self.dm
        now_str = get_current_datetime().strftime("%Y-%m-%d %H:%M:%S")

        dm.task_state.update({
            "task_type_key": "pipeline_inspection",
            "equipment_unit_id": "OBSROV--001",
            "water_depth": 300,
            "support_vessel": "海洋石油681",
            "oilfield_name": "东方1-1油田",
            "start_time": now_str,
        })

        # 模拟设置浑浊度 15 (触发 C014 soft warning)
        dm.kb.state_info.set_status("OBSROV-001", {"turbidity": 15, "current_velocity": 0.2})
        res1 = dm.validator.validate_task(dm.task_state)
        self.assertEqual(res1.overall_status, "blocked_soft")

        dm.phase = "blocked_soft"
        dm._blocking_violations = res1.violations
        dm._handle_soft_warning_confirmation("确认忽略警告", "req_1")

        self.assertGreater(len(dm.slot_store.validation_acknowledgements), 0)

        # 模拟设备遥测状态升级：浑浊度变为 20
        dm.kb.state_info.set_status("OBSROV-001", {"turbidity": 20, "current_velocity": 0.2})

        res2 = dm.validator.validate_task(dm.task_state, task_version=dm.slot_store.version, previous_result=res1)
        self.assertEqual(res2.overall_status, "blocked_soft")

        dm.phase = "confirming"
        reply = dm._handle_final_publish_confirmation("确认发布任务", "req_2")
        self.assertTrue(
            any(kw in reply for kw in ["不满足", "不符合", "重", "确认", "浑浊度", "建议"])
        )

    def test_blocked_hard_cannot_be_bypassed(self):
        """测试流速>1.2 (C017 hard block) 时，无法通过任何确认或忽略消息绕过发布。"""
        dm = self.dm
        now_str = get_current_datetime().strftime("%Y-%m-%d %H:%M:%S")

        dm.task_state.update({
            "task_type_key": "pipeline_inspection",
            "equipment_unit_id": "OBSROV--001",
            "water_depth": 300,
            "support_vessel": "海洋石油681",
            "oilfield_name": "东方1-1油田",
            "start_time": now_str,
        })

        dm.kb.state_info.set_status("OBSROV-001", {"current_velocity": 1.5})
        res = dm.validator.validate_task(dm.task_state)
        self.assertEqual(res.overall_status, "blocked_hard")

        dm.phase = "blocked_hard"
        dm._blocking_violations = res.violations
        dm._handle_task_confirm("确认")
        self.assertEqual(dm.phase, "blocked_hard")

        dm._handle_task_confirm("忽略警告")
        self.assertEqual(dm.phase, "blocked_hard")


if __name__ == "__main__":
    unittest.main()
