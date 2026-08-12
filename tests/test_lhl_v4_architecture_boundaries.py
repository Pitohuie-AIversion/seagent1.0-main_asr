import unittest
from unittest.mock import MagicMock, patch

from src.dialogue_manager import DialogueManager
from src.intent_router import IntentRouter
from src.interaction_plan import validate_interaction_plan
from src.knowledge_retriever import KnowledgeBase
from src.slot_store import Slot
from src.validator import TaskValidator, ValidationResult, Violation
from web_backend import app, _sessions_manager


class _FailingRouterLLM:
    def extract_json(self, *_args, **_kwargs):
        raise RuntimeError("router unavailable")


class _StaticRouterLLM:
    def __init__(self, payload):
        self.payload = payload

    def extract_json(self, *_args, **_kwargs):
        return dict(self.payload)


class LHLV4ArchitectureBoundaryTest(unittest.TestCase):
    def test_llm_router_failure_returns_clarify_without_rule_guessing(self):
        router = IntentRouter(_FailingRouterLLM(), KnowledgeBase())

        res = router.route(
            "水深300米",
            [],
            {"task_type_key": "pipeline_inspection"},
            phase="collecting",
            expected_slots=["water_depth"],
        )

        self.assertEqual(res.dialogue_mode, "knowledge_qa")
        self.assertEqual(res.query_intent, "CLARIFICATION")
        self.assertFalse(res.should_update_slots)

    def test_interaction_plan_protocol_invalid_operation_does_not_infer_write(self):
        plan = validate_interaction_plan(
            {
                "schema_version": 1,
                "operation": "BOGUS",
                "dialogue_mode": "task_collection",
                "confidence": 0.9,
                "subject_type": "task",
                "relation": "describe",
                "source_policy": "session_state",
            },
            user_message="水深300米",
            context={"expected_slots": ["water_depth"], "task_state": {"task_type_key": "pipeline_inspection"}},
        )

        self.assertEqual(plan.operation, "CLARIFY")
        self.assertEqual(plan.dialogue_mode, "knowledge_qa")
        self.assertTrue(plan.needs_clarification)

    def test_llm_read_plan_keeps_llm_subject_relation_without_keyword_reclassification(self):
        router = IntentRouter(
            _StaticRouterLLM(
                {
                    "schema_version": 1,
                    "operation": "READ",
                    "dialogue_mode": "knowledge_qa",
                    "query_intent": "KNOWLEDGE_QA",
                    "subject_type": "general_concept",
                    "subject_text": "金牛座",
                    "relation": "describe",
                    "source_policy": "project_kb",
                    "needs_clarification": False,
                    "emergency_action": None,
                    "confidence": 0.91,
                    "reason_code": "LLM_READ",
                }
            ),
            KnowledgeBase(),
        )

        res = router.route("金牛座有哪些能力？", [], {}, phase="collecting")

        self.assertEqual(res.query_intent, "KNOWLEDGE_QA")
        self.assertEqual(res.interaction_plan.subject_type, "general_concept")
        self.assertEqual(res.interaction_plan.relation, "describe")

    def test_soft_validator_status_is_warning_not_blocked_soft(self):
        validator = TaskValidator(KnowledgeBase())
        original = validator._run_checks
        try:
            validator._resolve_single_unit_snapshot = lambda *_args, **_kwargs: (None, None)
            validator._run_checks = lambda *_args, **_kwargs: [
                Violation("SOFT_TEST", "软警告", "仅提示", "soft")
            ]

            res = validator.validate_task({"task_type_key": "pipeline_inspection"})
        finally:
            validator._run_checks = original

        self.assertEqual(res.overall_status, "warning")
        self.assertEqual([v.severity for v in res.violations], ["soft"])

    def test_soft_violations_do_not_move_dialogue_manager_to_blocked_soft(self):
        dm = DialogueManager(kb=KnowledgeBase())
        dm.phase = "confirming"
        dm.task_state = {"task_type_key": "pipeline_inspection"}

        soft = Violation("SOFT_TEST", "软警告", "仅提示", "soft")
        dm._refresh_validation = MagicMock(
            return_value=ValidationResult(
                overall_status="warning",
                validated_at="2026-08-12T00:00:00",
                task_version=1,
                validation_version=1,
                validation_fingerprint="softfp",
                state_snapshot=None,
                violations=[soft],
            )
        )

        ctx = dm._run_constraint_check({"water_depth"})

        self.assertEqual(dm.phase, "confirming")
        self.assertEqual(ctx["type"], "none")
        self.assertEqual(dm._blocking_violations, [])

    def test_publish_success_final_result_is_task_intent_artifact(self):
        dm = DialogueManager(kb=KnowledgeBase())
        dm.phase = "confirming"
        dm.task_state = {
            "task_type_key": "pipeline_inspection",
            "intent_id": "TI20260812001",
            "equipment_family": "observation_rov",
            "equipment_type": "observation_rov_hp",
            "equipment_unit_id": "OBSROV--001",
        }
        dm._last_built_json = {
            "intent_id": "TI20260812001",
            "task_type_key": "pipeline_inspection",
            "equipment_unit_id": "OBSROV--001",
        }
        dm.slot_store.slots["intent_id"] = Slot("intent_id", value="TI20260812001", status="valid")
        dm.slot_store.slots["task_type_key"] = Slot("task_type_key", value="pipeline_inspection", status="valid")
        dm.slot_store.slots["equipment_unit_id"] = Slot("equipment_unit_id", value="OBSROV--001", status="valid")
        dm.builder.reserve_task_id = MagicMock(return_value="PI-20260812-001")
        dm.slot_store.get_missing_slots = MagicMock(return_value=[])
        dm.kb.state_info.check_runtime_availability = MagicMock(return_value={"available": True})
        dm._refresh_validation = MagicMock(
            return_value=ValidationResult(
                overall_status="valid",
                validated_at="2026-08-12T00:00:00",
                task_version=1,
                validation_version=1,
                validation_fingerprint="validfp",
                state_snapshot=None,
                violations=[],
            )
        )
        task_intent = {
            "schema_version": 2,
            "task_type": "pipeline_inspection",
            "task": {"type": "pipeline_inspection", "details": {}},
            "equipment": {"robot_unit_id": "OBSROV--001", "payload": []},
        }

        with patch("src.dialogue_manager.TaskIntentBuilder") as builder_cls:
            builder = builder_cls.return_value
            builder.prepare.return_value = task_intent
            builder.create_staging.return_value = "/tmp/task_intent.json.staging"
            builder.publish_staging.return_value = None

            dm._handle_final_publish_confirmation("确认发布", request_id="req_lhl_v4_boundary")

        self.assertEqual(dm.phase, "done")
        self.assertEqual(dm.final_result, task_intent)
        self.assertNotEqual(dm.final_result, dm._last_built_json)

    def test_publish_ignores_runtime_availability_gate_outside_constraints_yaml(self):
        dm = DialogueManager(kb=KnowledgeBase())
        dm.phase = "confirming"
        dm.task_state = {
            "task_type_key": "pipeline_inspection",
            "intent_id": "TI20260812002",
            "equipment_unit_id": "LROV--001",
        }
        dm._last_built_json = dict(dm.task_state)
        dm.slot_store.slots["intent_id"] = Slot("intent_id", value="TI20260812002", status="valid")
        dm.slot_store.slots["task_type_key"] = Slot("task_type_key", value="pipeline_inspection", status="valid")
        dm.slot_store.slots["equipment_unit_id"] = Slot("equipment_unit_id", value="LROV--001", status="valid")
        dm.builder.reserve_task_id = MagicMock(return_value="PI-20260812-002")
        dm.slot_store.get_missing_slots = MagicMock(return_value=[])
        dm.kb.state_info.check_runtime_availability = MagicMock(
            return_value={
                "available": False,
                "reason_code": "STATE_EXPIRED",
                "message": "无法发布任务：机器人 LROV--001 状态信息已过期，无法确认当前可用性。",
            }
        )
        dm._refresh_validation = MagicMock(
            return_value=ValidationResult(
                overall_status="valid",
                validated_at="2026-08-12T00:00:00",
                task_version=1,
                validation_version=1,
                validation_fingerprint="validfp2",
                state_snapshot=None,
                violations=[],
            )
        )
        task_intent = {
            "schema_version": 2,
            "task_type": "pipeline_inspection",
            "task": {"type": "pipeline_inspection", "details": {}},
            "equipment": {"robot_unit_id": "LROV--001", "payload": []},
        }

        with patch("src.dialogue_manager.TaskIntentBuilder") as builder_cls:
            builder = builder_cls.return_value
            builder.prepare.return_value = task_intent
            builder.create_staging.return_value = "/tmp/task_intent.json.staging"
            builder.publish_staging.return_value = None

            reply = dm._handle_final_publish_confirmation("确认发布", request_id="req_no_runtime_gate")

        self.assertEqual(dm.phase, "done")
        self.assertEqual(dm.final_result, task_intent)
        self.assertNotIn("状态信息已过期", reply)

    def test_session_state_api_uses_final_result_for_final_json(self):
        dm = DialogueManager(kb=KnowledgeBase())
        dm.phase = "done"
        dm._last_built_json = {"flat": "built-json"}
        dm.final_result = {"schema_version": 2, "task": {"type": "pipeline_inspection"}}
        _sessions_manager.clear()
        _sessions_manager["lhl_v4_final"] = dm

        try:
            with app.test_client() as client:
                resp = client.get("/api/session/state?session_id=lhl_v4_final")
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertEqual(data["final_json"], dm.final_result)
            self.assertEqual(data["collected"], dm._last_built_json)
        finally:
            _sessions_manager.clear()


if __name__ == "__main__":
    unittest.main()
