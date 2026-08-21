import unittest
from unittest.mock import MagicMock

from src.dialogue_manager import DialogueManager
from src.knowledge_retriever import KnowledgeBase
from src.validator import Violation, ValidationResult


class FakeLLM:
    def extract_json(self, messages, max_tokens=800):
        return {"slot_candidates": [], "unresolved": []}

    def chat(self, messages, **kwargs):
        return "收到"

    def filter_reply(self, reply):
        return reply


class BlockerPriorityTransitionsTest(unittest.TestCase):
    def setUp(self):
        self.kb = KnowledgeBase()
        self.llm = FakeLLM()
        self.dm = DialogueManager(self.llm, self.kb)

    def _create_violation(self, constraint_id: str, severity: str, name: str = "Test Constraint") -> Violation:
        return Violation(
            constraint_id=constraint_id,
            constraint_name=name,
            check_type="test_check",
            severity=severity,
            message=f"Test violation {constraint_id}",
            related_fields=["water_depth"],
        )

    def _mock_validation_violations(self, violations: list[Violation]):
        val_res = ValidationResult(
            overall_status="invalid" if violations else "valid",
            validated_at="2026-08-07T12:00:00Z",
            task_version=1,
            validation_version=1,
            validation_fingerprint="test_fp",
            state_snapshot={},
            violations=violations,
        )
        self.dm.validator.validate_task = MagicMock(return_value=val_res)

    # 1. collecting + soft + interactive -> 维持 collecting (软警告在槽位收集阶段不中断对话)
    def test_01_collecting_plus_soft_transitions_to_blocked_soft(self):
        self.dm.phase = "collecting"
        v_soft = self._create_violation("C001", "soft")
        self._mock_validation_violations([v_soft])

        res = self.dm._run_constraint_check({"water_depth"}, purpose="interactive")
        self.assertEqual(self.dm.phase, "collecting")
        self.assertEqual(res["type"], "none")

        # 当所有槽位收集完毕 (purpose="preview") 时，软警告统一触发 blocked_soft
        res_preview = self.dm._run_constraint_check({"water_depth"}, purpose="preview")
        self.assertEqual(self.dm.phase, "blocked_soft")
        self.assertEqual(res_preview["type"], "soft")

    # 2. collecting + hard -> blocked_hard (硬约束即时阻断)
    def test_02_collecting_plus_hard_transitions_to_blocked_hard(self):
        self.dm.phase = "collecting"
        v_hard = self._create_violation("C002", "hard")
        self._mock_validation_violations([v_hard])

        res = self.dm._run_constraint_check({"water_depth"})
        self.assertEqual(self.dm.phase, "blocked_hard")
        self.assertEqual(res["type"], "hard")

    # 3. collecting + hard + soft -> blocked_hard
    def test_03_collecting_plus_hard_and_soft_transitions_to_blocked_hard(self):
        self.dm.phase = "collecting"
        v_soft = self._create_violation("C001", "soft")
        v_hard = self._create_violation("C002", "hard")
        self._mock_validation_violations([v_soft, v_hard])

        res = self.dm._run_constraint_check({"water_depth"})
        self.assertEqual(self.dm.phase, "blocked_hard")
        self.assertEqual(res["type"], "hard")

    # 4. blocked_soft + soft -> blocked_soft
    def test_04_blocked_soft_plus_soft_remains_blocked_soft(self):
        self.dm.phase = "blocked_soft"
        v_soft = self._create_violation("C001", "soft")
        self._mock_validation_violations([v_soft])

        res = self.dm._run_constraint_check({"water_depth"})
        self.assertEqual(self.dm.phase, "blocked_soft")
        self.assertEqual(res["type"], "soft")

    # 5. blocked_soft + hard -> blocked_hard
    def test_05_blocked_soft_plus_hard_transitions_to_blocked_hard(self):
        self.dm.phase = "blocked_soft"
        v_hard = self._create_violation("C002", "hard")
        self._mock_validation_violations([v_hard])

        res = self.dm._run_constraint_check({"water_depth"})
        self.assertEqual(self.dm.phase, "blocked_hard")
        self.assertEqual(res["type"], "hard")

    # 6. blocked_soft + hard + soft -> blocked_hard
    def test_06_blocked_soft_plus_hard_and_soft_transitions_to_blocked_hard(self):
        self.dm.phase = "blocked_soft"
        v_soft = self._create_violation("C001", "soft")
        v_hard = self._create_violation("C002", "hard")
        self._mock_validation_violations([v_soft, v_hard])

        res = self.dm._run_constraint_check({"water_depth"})
        self.assertEqual(self.dm.phase, "blocked_hard")
        self.assertEqual(res["type"], "hard")

    # 7. blocked_hard + hard -> blocked_hard
    def test_07_blocked_hard_plus_hard_remains_blocked_hard(self):
        self.dm.phase = "blocked_hard"
        v_hard = self._create_violation("C002", "hard")
        self._mock_validation_violations([v_hard])

        res = self.dm._run_constraint_check({"water_depth"})
        self.assertEqual(self.dm.phase, "blocked_hard")
        self.assertEqual(res["type"], "hard")

    # 8. blocked_hard + hard解除 + soft -> 在 interactive 下回到 collecting，在 preview 下转为 blocked_soft
    def test_08_blocked_hard_resolving_hard_but_has_soft_transitions_to_blocked_soft(self):
        self.dm.phase = "blocked_hard"
        v_soft = self._create_violation("C001", "soft")
        self._mock_validation_violations([v_soft])

        res = self.dm._run_constraint_check({"water_depth"}, purpose="interactive")
        self.assertEqual(self.dm.phase, "collecting")
        self.assertEqual(res["type"], "none")

        self.dm.phase = "blocked_hard"
        res_preview = self.dm._run_constraint_check({"water_depth"}, purpose="preview")
        self.assertEqual(self.dm.phase, "blocked_soft")
        self.assertEqual(res_preview["type"], "soft")

    # 9. blocked_hard + 全部解除 -> collecting
    def test_09_blocked_hard_resolving_all_transitions_to_collecting(self):
        self.dm.phase = "blocked_hard"
        self._mock_validation_violations([])

        res = self.dm._run_constraint_check({"water_depth"})
        self.assertEqual(self.dm.phase, "collecting")
        self.assertEqual(res["type"], "none")

    # 10. blocked_hard + "忽略警告" -> 不允许绕过 hard
    def test_10_blocked_hard_ignore_warning_disallowed(self):
        self.dm.phase = "blocked_hard"
        v_hard = self._create_violation("C002", "hard")
        self.dm._blocking_violations = [v_hard]
        self._mock_validation_violations([v_hard])

        reply = self.dm.process("忽略警告")
        self.assertEqual(self.dm.phase, "blocked_hard")
        self.assertNotEqual(self.dm.phase, "done")

    # 11. C028 单独触发 -> 在 collecting + interactive 维持 collecting，在 preview 触发 blocked_soft
    def test_11_real_oilfield_c028_triggers_blocked_soft(self):
        dm = DialogueManager(self.llm, self.kb)
        dm.task_state["task_type_key"] = "pipeline_inspection"
        dm.task_state["equipment_class"] = "observation_rov"
        dm.task_state["equipment_family"] = "light_work_class_rov"
        dm.task_state["equipment_type"] = "轻型工作级深海机器人 150HP"
        dm.task_state["equipment_unit_id"] = "LROV-150-001"
        dm.task_state["oilfield_entity_id"] = "liuhua_11_1"
        dm.task_state["oilfield_name"] = "流花11-1油田"
        dm.task_state["start_point"] = {"lat": 10.0, "lon": 10.0}
        dm.task_state["water_depth"] = 300
        dm.phase = "collecting"

        dm._run_constraint_check({"start_point", "water_depth"}, purpose="interactive")
        self.assertEqual(dm.phase, "collecting")

        dm._run_constraint_check({"start_point", "water_depth"}, purpose="preview")
        self.assertEqual(dm.phase, "blocked_soft")
        violations = dm._blocking_violations
        self.assertTrue(any(v.severity == "soft" for v in violations))

    # 12. C028 + C029 同时触发 -> blocked_hard (真实 oilfield context 接入)
    def test_12_real_oilfield_c028_and_c029_triggers_blocked_hard(self):
        dm = DialogueManager(self.llm, self.kb)
        dm.task_state["oilfield_entity_id"] = "lingshui_17_2"
        dm.task_state["oilfield_name"] = "陵水17-2气田"
        dm.task_state["start_point"] = {"lat": 10.0, "lon": 10.0}
        dm.task_state["water_depth"] = 3000
        dm.phase = "collecting"

        dm._run_constraint_check({"start_point", "water_depth"})
        self.assertEqual(dm.phase, "blocked_hard")
        violations = dm._blocking_violations
        self.assertTrue(any(v.constraint_id == "C029" for v in violations))


if __name__ == "__main__":
    unittest.main()
