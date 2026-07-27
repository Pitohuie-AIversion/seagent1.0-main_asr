from pathlib import Path
from datetime import timedelta
import sys
import unittest
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.knowledge_retriever import KnowledgeBase
from src.dialogue_manager import DialogueManager
from src.llm_client import LLMClient
from src.output_builder import OutputBuilder
from src.prompts import build_responder_messages
from src.task_intent_builder import TaskIntentBuilder
from src.intent_router import IntentRouter
from src.simulated_time import get_current_datetime


class FixedInteractionLLM:
    def __init__(self, result):
        self.result = result

    def classify_interaction(self, messages, max_tokens=260):
        return dict(self.result)


class DialogueManagerROVTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.kb = KnowledgeBase()
        cls.llm = MagicMock(spec=LLMClient)
        cls.llm.generate.return_value = "null"

    def test_interaction_router_prioritizes_write_over_entity_keyword_query(self):
        router = IntentRouter(FixedInteractionLLM({
            "interaction_type": "WRITE",
            "query_intent": None,
            "confidence": 0.95,
            "reason": "用户提交任务信息",
        }))
        route = router.route(
            "我想做管缆巡检，开始时间现在，结束时间五小时后，管缆类型海底油气管道",
            conversation_history=[],
            task_state={},
            phase="collecting",
            expected_slots=[],
        )

        self.assertEqual(route.interaction_type, "WRITE")
        self.assertIsNone(route.query_intent)

    def test_interaction_router_keeps_real_cable_type_question_as_query(self):
        router = IntentRouter(FixedInteractionLLM({
            "interaction_type": "QUERY",
            "query_intent": "KNOWLEDGE_QA",
            "confidence": 0.95,
            "reason": "用户询问业务知识",
        }))
        route = router.route(
            "管缆类型有哪些？",
            conversation_history=[],
            task_state={},
            phase="collecting",
            expected_slots=[],
        )

        self.assertEqual(route.interaction_type, "QUERY")
        self.assertEqual(route.query_intent, "KNOWLEDGE_QA")

    def test_interaction_router_treats_expected_slot_answer_as_write(self):
        router = IntentRouter(FixedInteractionLLM({
            "interaction_type": "WRITE",
            "query_intent": None,
            "confidence": 0.95,
            "reason": "用户回答 expected_slots",
        }))
        route = router.route(
            "海底油气管道",
            conversation_history=[],
            task_state={"task_type_key": "pipeline_inspection"},
            phase="collecting",
            expected_slots=["cable_type"],
        )

        self.assertEqual(route.interaction_type, "WRITE")
        self.assertIsNone(route.query_intent)

    def test_dialogue_manager_writes_compound_create_message_slots(self):
        class CompoundExtractor:
            def __init__(self):
                self.start_time = get_current_datetime().replace(microsecond=0)
                self.end_time = self.start_time + timedelta(hours=5)

            @staticmethod
            def _candidate(key, value, raw):
                return {
                    "canonical_key": key,
                    "normalized_value": value,
                    "raw_value": raw,
                    "confidence": 1.0,
                }

            def extract_updates(self, user_message, current_state, **kwargs):
                return {
                    "slot_candidates": [
                        self._candidate("task_type", "管缆巡检", user_message),
                        self._candidate("task_type_key", "pipeline_inspection", user_message),
                        self._candidate("start_time", self.start_time.strftime("%Y-%m-%dT%H:%M:%S"), user_message),
                        self._candidate("end_time", self.end_time.strftime("%Y-%m-%dT%H:%M:%S"), user_message),
                        self._candidate("cable_type", "海底油气管道", user_message),
                    ],
                    "unresolved": [],
                }

        dm = DialogueManager(LLMClient(None, None), self.kb)
        dm.extractor = CompoundExtractor()
        dm.process(
            "我想做管缆巡检，开始时间现在，结束时间五小时后，管缆类型海底油气管道",
            request_id="compound_create_test",
        )

        state = dm.slot_store.get_task_state()
        self.assertEqual(state.get("task_type"), "管缆巡检")
        self.assertEqual(state.get("task_type_key"), "pipeline_inspection")
        self.assertEqual(state.get("start_time"), dm.extractor.start_time.strftime("%Y-%m-%dT%H:%M:%S"))
        self.assertEqual(state.get("end_time"), dm.extractor.end_time.strftime("%Y-%m-%dT%H:%M:%S"))
        self.assertEqual(state.get("cable_type"), "海底油气管道")

    def test_write_route_without_extracted_candidates_does_not_mutate_slots(self):
        class EmptyExtractor:
            def extract_updates(self, user_message, current_state, **kwargs):
                return {"slot_candidates": [], "unresolved": []}

        dm = DialogueManager(LLMClient(None, None), self.kb)
        dm.extractor = EmptyExtractor()
        before_version = dm.slot_store.version
        reply = dm.process("请规划一个巡检作业", request_id="empty_write_test")

        state = dm.slot_store.get_task_state()
        self.assertEqual(dm.slot_store.version, before_version)
        self.assertIsNone(state.get("task_type"))
        self.assertIn("没有提取到可写入的合法字段", reply)

    def _commit_equipment_update(
        self,
        task_type_key,
        task_type,
        updates,
    ):
        dm, slots = self._normal_slots(task_type_key)
        slots["task_type"].value = task_type
        slots["task_type"].status = "valid"
        dm._apply_updates_in_transaction(updates, slots)
        dm._normalize_and_validate_in_transaction(slots, task_type_key)
        dm.slot_store.commit_transaction(slots, [])
        dm.task_state = dm.slot_store.get_task_state()
        return dm

    def test_equipment_transaction_with_rov_alias_observation(self):
        dm = self._commit_equipment_update(
            "pipeline_inspection",
            "管缆巡检",
            {"equipment_name": "观察级"},
        )
        self.assertEqual(dm.task_state.get("equipment_name"), "观察级深海机器人 HP")
        self.assertEqual(dm.task_state.get("equipment_family"), "观察级深海机器人")
        self.assertEqual(dm.task_state.get("equipment_type"), "观察级深海机器人 HP")

    def test_equipment_transaction_with_rov_alias_work(self):
        dm = self._commit_equipment_update(
            "tree_valve_operation",
            "采油树控制面板插入",
            {"equipment_name": "工作级"},
        )
        self.assertEqual(dm.task_state.get("equipment_name"), "通用工作级深海机器人 250HP")
        self.assertEqual(dm.task_state.get("equipment_family"), "通用工作级深海机器人")
        self.assertEqual(dm.task_state.get("equipment_type"), "通用工作级深海机器人 250HP")

    def test_equipment_transaction_with_rov_alias_tractor(self):
        dm = self._commit_equipment_update(
            "pipeline_burial",
            "管缆埋设",
            {"equipment_name": "金牛座"},
        )
        self.assertEqual(dm.task_state.get("equipment_name"), "履带式海底重载作业机器人 1600HP")
        self.assertEqual(dm.task_state.get("equipment_family"), "履带式海底重载作业机器人")
        self.assertEqual(dm.task_state.get("equipment_type"), "履带式海底重载作业机器人 1600HP")

    def test_family_and_variant_candidate_interfaces(self):
        builder = OutputBuilder(self.kb)

        family_field = {
            "type": "string",
            "allowed_values_ref": "robot_family_full_names",
        }
        families = builder.resolve_allowed_values(
            family_field,
            "pipeline_inspection",
            {},
        )
        self.assertEqual(
            families,
            [
                "轻型工作级深海机器人",
                "观察级深海机器人",
                "水下无人自主航行器",
            ],
        )

        task_state = {"equipment_family": "观察级深海机器人"}
        variant_field = {
            "type": "string",
            "allowed_values_ref": "robot_variant_full_names",
        }
        legacy_variant_field = {
            "type": "string",
            "allowed_values_ref": "robot_full_names",
        }
        expected = ["观察级深海机器人 HP"]
        self.assertEqual(
            builder.resolve_allowed_values(
                variant_field,
                "pipeline_inspection",
                task_state,
            ),
            expected,
        )
        self.assertEqual(
            builder.resolve_allowed_values(
                legacy_variant_field,
                "pipeline_inspection",
                task_state,
            ),
            expected,
        )

        invalid_state = {"equipment_family": "不存在的机器人族"}
        self.assertEqual(
            builder.resolve_allowed_values(
                variant_field,
                "pipeline_inspection",
                invalid_state,
            ),
            [],
        )


    def test_normal_schema_asks_family_before_variant_and_unit(self):
        builder = OutputBuilder(self.kb)
        for task_type_key in (
            "pipeline_inspection",
            "pipeline_burial",
            "tree_valve_operation",
        ):
            schema = builder.get_schema(task_type_key, "normal")
            keys = [field["key"] for field in schema]
            self.assertLess(keys.index("equipment_family"), keys.index("equipment_type"))
            self.assertLess(keys.index("equipment_type"), keys.index("equipment_unit_id"))

            fields = {field["key"]: field for field in schema}
            self.assertEqual(
                fields["equipment_family"]["allowed_values_ref"],
                "robot_family_full_names",
            )
            self.assertEqual(
                fields["equipment_type"]["allowed_values_ref"],
                "robot_variant_full_names",
            )

            emergency_keys = [
                field["key"]
                for field in builder.get_schema(task_type_key, "emergency")
            ]
            self.assertNotIn("equipment_family", emergency_keys)


    def test_variant_alias_is_available_to_backend_lookup(self):
        rov = self.kb.get_rov("巡检ROV HP")
        self.assertIsNotNone(rov)
        self.assertEqual(rov["full_name"], "观察级深海机器人 HP")
        self.assertIn("巡检ROV HP", rov["aliases"])

    def test_prompt_enforces_family_variant_unit_dependency(self):
        common = dict(
            mode="normal",
            phase="collecting",
            knowledge_context="",
            constraint_context={"type": "none"},
            conversation_history=[],
            latest_user_message="继续",
            ROV2type={},
            support_task=["管缆巡检"],
        )
        missing = [
            {"key": "equipment_family", "label": "作业机器人系列", "type": "string", "allowed_values": ["观察级深海机器人"]},
            {"key": "equipment_type", "label": "作业设备型号", "type": "string", "allowed_values": ["观察级深海机器人 HP"]},
            {"key": "equipment_unit_id", "label": "具体机器人编号", "type": "string", "allowed_values": []},
        ]
        system = build_responder_messages(
            task_state={}, built_json={}, missing_fields=missing, **common
        )[0]["content"]
        self.assertIn("本轮只询问作业机器人系列", system)
        self.assertIn("不得询问或展示作业设备型号", system)

        system = build_responder_messages(
            task_state={"equipment_family": "观察级深海机器人"},
            built_json={"equipment_family": "观察级深海机器人"},
            missing_fields=missing[1:],
            **common,
        )[0]["content"]
        self.assertIn("本轮只询问作业设备型号", system)
        self.assertIn("不得询问具体机器人编号", system)

    def test_prompt_requires_allowed_values_to_be_rendered_verbatim_for_all_fields(self):
        messages = build_responder_messages(
            task_state={"equipment_family": "轻型工作级深海机器人"},
            built_json={"equipment_family": "轻型工作级深海机器人"},
            missing_fields=[
                {
                    "key": "cable_type",
                    "label": "管缆类型",
                    "type": "string",
                    "allowed_values": ["海底油气管道", "电力电缆"],
                },
                {
                    "key": "support_vessel",
                    "label": "支持船编号",
                    "type": "string",
                    "allowed_values": ["海洋石油681"],
                },
            ],
            mode="normal",
            phase="collecting",
            knowledge_context="",
            constraint_context={"type": "none"},
            conversation_history=[],
            latest_user_message="继续",
            ROV2type={},
            support_task=["管缆巡检"],
        )

        system = messages[0]["content"]
        self.assertIn("海底油气管道", system)
        self.assertIn("电力电缆", system)
        self.assertIn("海洋石油681", system)
        self.assertIn("任意待收集字段包含 allowed_values", system)
        self.assertIn("逐字原样复制 allowed_values", system)
        self.assertIn("用户看到的每一个候选项", system)
        self.assertIn("完全字符串匹配", system)
        self.assertIn("不得把其他字段的已收集值", system)

    def test_responder_uses_committed_update_instead_of_raw_alias(self):
        messages = build_responder_messages(
            task_state={"equipment_family": "轻型工作级深海机器人"},
            built_json={"equipment_family": "轻型工作级深海机器人"},
            missing_fields=[
                {
                    "key": "equipment_type",
                    "label": "作业设备型号",
                    "type": "string",
                    "allowed_values": ["轻型工作级深海机器人 HP"],
                }
            ],
            mode="normal",
            phase="collecting",
            knowledge_context="",
            constraint_context={"type": "none"},
            conversation_history=[],
            latest_user_message="使用天鹰座",
            accepted_updates={
                "equipment_family": "轻型工作级深海机器人",
            },
            unresolved_inputs=[],
            ROV2type={},
            support_task=["管缆巡检"],
        )

        turn_message = messages[-1]["content"]
        self.assertNotIn("使用天鹰座", turn_message)
        self.assertIn("equipment_family", turn_message)
        self.assertIn("轻型工作级深海机器人", turn_message)
        self.assertIn("已提交", turn_message)

    def test_responder_keeps_only_unresolved_question_after_committed_update(self):
        messages = build_responder_messages(
            task_state={"equipment_family": "轻型工作级深海机器人"},
            built_json={"equipment_family": "轻型工作级深海机器人"},
            missing_fields=[],
            mode="normal",
            phase="collecting",
            knowledge_context="",
            constraint_context={"type": "none"},
            conversation_history=[],
            latest_user_message="使用天鹰座，它最大水深是多少？",
            accepted_updates={
                "equipment_family": "轻型工作级深海机器人",
            },
            unresolved_inputs=["它最大水深是多少？"],
            ROV2type={},
            support_task=["管缆巡检"],
        )

        turn_message = messages[-1]["content"]
        self.assertNotIn("使用天鹰座", turn_message)
        self.assertIn("它最大水深是多少？", turn_message)
        self.assertIn("轻型工作级深海机器人", turn_message)

    def test_process_passes_committed_slot_delta_to_responder(self):
        llm = MagicMock(spec=LLMClient)
        llm.extract_json.return_value = {
            "intent": "task_update",
            "slot_candidates": [
                {
                    "raw_key": "天鹰座",
                    "canonical_key": "equipment_family",
                    "raw_value": "天鹰座",
                    "normalized_value": "轻型工作级深海机器人",
                    "confidence": 0.95,
                }
            ],
            "unresolved": [],
        }
        llm.chat.return_value = "已记录机器人系列。"
        llm.filter_reply.return_value = "已记录机器人系列。"

        dm = DialogueManager(llm, self.kb)
        schema = dm.builder.get_schema("pipeline_inspection", "normal")
        dm.slot_store.init_task_slots(schema)
        slots = dm.slot_store.clone_slots()
        slots["task_type_key"].value = "pipeline_inspection"
        slots["task_type_key"].status = "valid"
        slots["task_type"].value = "管缆巡检"
        slots["task_type"].status = "valid"
        dm.slot_store.commit_transaction(slots, [])
        dm.task_state = dm.slot_store.get_task_state()

        dm.process("使用天鹰座")

        messages = llm.chat.call_args.args[0]
        turn_message = messages[-1]["content"]
        self.assertNotIn("使用天鹰座", turn_message)
        self.assertIn("equipment_family", turn_message)
        self.assertIn("轻型工作级深海机器人", turn_message)
        self.assertEqual(
            dm.task_state.get("equipment_family"),
            "轻型工作级深海机器人",
        )


    def _normal_slots(self, task_type_key="pipeline_inspection"):
        dm = DialogueManager(self.llm, self.kb)
        schema = dm.builder.get_schema(task_type_key, "normal")
        dm.slot_store.init_task_slots(schema)
        slots = dm.slot_store.clone_slots()
        slots["task_type_key"].value = task_type_key
        slots["task_type_key"].status = "valid"
        return dm, slots

    def test_model_selection_auto_fills_family(self):
        dm, slots = self._normal_slots()
        dm._apply_updates_in_transaction(
            {"equipment_type": "巡检ROV HP"},
            slots,
        )
        dm._normalize_and_validate_in_transaction(slots, "pipeline_inspection")
        self.assertEqual(slots["equipment_family"].value, "观察级深海机器人")
        self.assertEqual(slots["equipment_family"].status, "valid")
        self.assertEqual(slots["equipment_type"].value, "观察级深海机器人 HP")
        self.assertEqual(slots["equipment_type"].status, "valid")

    def test_explicit_family_rejects_variant_from_another_family(self):
        dm, slots = self._normal_slots()
        dm._apply_updates_in_transaction(
            {
                "equipment_family": "观察级深海机器人",
                "equipment_type": "AUV HP",
            },
            slots,
        )
        dm._normalize_and_validate_in_transaction(slots, "pipeline_inspection")
        self.assertEqual(slots["equipment_family"].value, "观察级深海机器人")
        self.assertEqual(slots["equipment_family"].status, "valid")
        self.assertEqual(slots["equipment_type"].status, "invalid")

    def test_changing_family_clears_stale_variant_and_unit(self):
        dm, slots = self._normal_slots()
        for key, value in {
            "equipment_family": "观察级深海机器人",
            "equipment_type": "观察级深海机器人 HP",
            "equipment_unit_id": "OROV-HP-001",
            "equipment_name": "观察级深海机器人 HP",
        }.items():
            slots[key].value = value
            slots[key].status = "valid"

        dm._apply_updates_in_transaction(
            {"equipment_family": "轻型工作级深海机器人"},
            slots,
            allow_overwrite=True,
        )
        self.assertEqual(slots["equipment_family"].status, "candidate")
        for key in ("equipment_type", "equipment_unit_id", "equipment_name"):
            self.assertIsNone(slots[key].value)
            self.assertEqual(slots[key].status, "missing")


    def test_task_intent_robot_type_comes_from_selected_variant(self):
        builder = TaskIntentBuilder(self.kb)
        cases = {
            "观察级深海机器人 HP": "observation_rov",
            "通用工作级深海机器人 250HP": "work_class_rov",
            "水下无人自主航行器 HP": "auv",
            "履带式海底重载作业机器人 1600HP": "work_class_rov",
        }
        for variant, expected in cases.items():
            with self.subTest(variant=variant):
                self.assertEqual(
                    builder._resolve_robot_type(
                        {"equipment_type": variant},
                        {},
                    ),
                    expected,
                )


    def test_model_change_updates_family_and_clears_old_unit_via_slot_store(self):
        dm, slots = self._normal_slots()
        for key, value in {
            "equipment_family": "观察级深海机器人",
            "equipment_type": "观察级深海机器人 HP",
            "equipment_name": "观察级深海机器人 HP",
            "equipment_unit_id": "OBSROV-HP-001",
        }.items():
            slots[key].value = value
            slots[key].status = "valid"
        dm.slot_store.commit_transaction(slots, [])

        new_slots = dm.slot_store.clone_slots()
        dm._apply_updates_in_transaction(
            {"equipment_type": "AUV HP"},
            new_slots,
            allow_overwrite=True,
        )
        dm._normalize_and_validate_in_transaction(new_slots, "pipeline_inspection")
        dm.slot_store.commit_transaction(new_slots, [])
        dm.task_state = dm.slot_store.get_task_state()

        self.assertEqual(dm.task_state["equipment_family"], "水下无人自主航行器")
        self.assertEqual(dm.task_state["equipment_type"], "水下无人自主航行器 HP")
        self.assertNotIn("equipment_unit_id", dm.task_state)
        self.assertIsNone(dm.slot_store.slots["equipment_unit_id"].value)

    def test_equipment_updates_have_no_direct_task_state_legacy_entry(self):
        self.assertFalse(hasattr(DialogueManager, "_apply_updates"))
        self.assertTrue(
            hasattr(DialogueManager, "_handle_equipment_updates_in_transaction")
        )

    def test_frontend_has_equipment_family_label(self):
        js = (PROJECT_ROOT / "frontend" / "js" / "index.js").read_text()
        self.assertIn(
            'equipment_family: { zh: "作业机器人系列", en: "Robot Family" }',
            js,
        )


if __name__ == "__main__":
    unittest.main()
