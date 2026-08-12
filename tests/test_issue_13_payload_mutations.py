"""Issue #13 payload list mutations: deterministic core and DM transactions."""

import unittest

from src.dialogue_manager import DialogueManager
from src.knowledge_retriever import KnowledgeBase
from src.output_builder import OutputBuilder
from src.slot_store import Slot, SlotStore
from tests.interaction_plan_support import (
    ScriptedLLM,
    extraction_result,
    make_plan,
    slot_candidate,
)


class TestIssue13PayloadMutations(unittest.TestCase):
    def setUp(self):
        self.kb = KnowledgeBase()
        self.slot_store = SlotStore(self.kb)
        self.output_builder = OutputBuilder(self.kb)

        self.cam = "高清水下摄像机"
        self.sonar = "成像声呐"
        self.arm = "多功能液压机械臂"
        self.unknown = "无中生有水下切割刀"

        self.pipeline_req = [
            {
                "key": "payload",
                "label": "携带工具",
                "type": "list",
                "allowed_values": [
                    self.cam,
                    "LED水下照明灯",
                    "激光标尺",
                    self.sonar,
                ],
            }
        ]

    @staticmethod
    def _mutation(
        operation,
        *,
        items=(),
        target_items=(),
        raw_text=None,
        confidence=0.95,
        source="user_input",
    ):
        """Build the complete list-mutation contract without parsing user text."""
        return {
            "field": "payload",
            "operation": operation,
            "items": list(items),
            "target_items": list(target_items),
            "raw_text": raw_text or f"scripted:{operation}",
            "confidence": confidence,
            "source": source,
        }

    def _payload_slots(self, values):
        return {
            "payload": Slot(
                "payload",
                value=list(values),
                value_type="list",
                status="valid" if values else "missing",
            )
        }

    def _seed_pipeline_dm(self, payload=()):
        llm = ScriptedLLM(default_reply="事务结果已处理。")
        dm = DialogueManager(llm=llm, kb=self.kb)
        schema = dm.builder.get_schema("pipeline_inspection", dm.mode)
        dm.slot_store.init_task_slots(schema)
        slots = dm.slot_store.clone_slots()
        slots["task_type_key"] = Slot(
            "task_type_key",
            value="pipeline_inspection",
            status="valid",
            source="test_seed",
        )
        slots["task_type"] = Slot(
            "task_type",
            value="管缆巡检",
            status="valid",
            source="test_seed",
        )
        if payload:
            slots["payload"] = Slot(
                "payload",
                value=list(payload),
                value_type="list",
                status="valid",
                source="test_seed",
            )
        dm.slot_store.commit_transaction(slots, [], request_id="seed-payload-test")
        dm._rebuild_cache()
        dm.phase = "collecting"
        return dm, llm

    def test_slot_store_add_is_idempotent(self):
        slots = self._payload_slots([self.cam])

        added = self.slot_store.apply_list_mutation(
            slots,
            self._mutation("add", items=[self.sonar]),
            required_schema=self.pipeline_req,
            payload_catalog={},
        )
        duplicate = self.slot_store.apply_list_mutation(
            slots,
            self._mutation("add", items=[self.sonar]),
            required_schema=self.pipeline_req,
            payload_catalog={},
        )

        self.assertTrue(added["success"])
        self.assertTrue(added["changed"])
        self.assertTrue(duplicate["success"])
        self.assertFalse(duplicate["changed"])
        self.assertEqual(slots["payload"].value, [self.cam, self.sonar])

    def test_slot_store_remove_and_missing_remove_preserve_state(self):
        slots = self._payload_slots([self.cam, self.sonar])

        removed = self.slot_store.apply_list_mutation(
            slots,
            self._mutation("remove", items=[self.sonar]),
            required_schema=self.pipeline_req,
            payload_catalog={},
        )
        missing = self.slot_store.apply_list_mutation(
            slots,
            self._mutation("remove", items=[self.sonar]),
            required_schema=self.pipeline_req,
            payload_catalog={},
        )

        self.assertTrue(removed["success"])
        self.assertEqual(removed["new_value"], [self.cam])
        self.assertFalse(missing["success"])
        self.assertEqual(slots["payload"].value, [self.cam])
        self.assertIn("不在当前列表中", slots["payload"].validation_error)

    def test_slot_store_replace_is_atomic(self):
        slots = self._payload_slots([self.cam])

        result = self.slot_store.apply_list_mutation(
            slots,
            self._mutation(
                "replace",
                items=[self.sonar],
                target_items=[self.cam],
            ),
            required_schema=self.pipeline_req,
            payload_catalog={},
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["old_value"], [self.cam])
        self.assertEqual(result["new_value"], [self.sonar])
        self.assertEqual(slots["payload"].value, [self.sonar])

    def test_slot_store_clear_marks_payload_missing(self):
        slots = self._payload_slots([self.cam, self.sonar])

        result = self.slot_store.apply_list_mutation(
            slots,
            self._mutation("clear"),
            required_schema=self.pipeline_req,
            payload_catalog={},
        )

        self.assertTrue(result["success"])
        self.assertEqual(slots["payload"].value, [])
        self.assertEqual(slots["payload"].status, "missing")
        self.assertIsNone(slots["payload"].validation_error)

    def test_slot_store_rejects_cross_task_and_unknown_payloads(self):
        payload_catalog = self.kb.assets.get("payload_catalog", {})
        for rejected_item in (self.arm, self.unknown):
            with self.subTest(rejected_item=rejected_item):
                slots = self._payload_slots([self.cam])
                result = self.slot_store.apply_list_mutation(
                    slots,
                    self._mutation("add", items=[rejected_item]),
                    required_schema=self.pipeline_req,
                    payload_catalog=payload_catalog,
                )
                self.assertFalse(result["success"])
                self.assertEqual(slots["payload"].value, [self.cam])
                self.assertIn(rejected_item, slots["payload"].validation_error)

        slots = self._payload_slots([self.cam])
        replacement = self.slot_store.apply_list_mutation(
            slots,
            self._mutation(
                "replace",
                items=[self.unknown],
                target_items=[self.cam],
            ),
            required_schema=self.pipeline_req,
            payload_catalog=payload_catalog,
        )
        self.assertFalse(replacement["success"])
        self.assertEqual(slots["payload"].value, [self.cam])

    def test_failed_mutation_preserves_value_and_records_audit_metadata(self):
        slots = self._payload_slots([self.cam])
        mutation = self._mutation(
            "add",
            items=[self.unknown],
            raw_text="audit:unknown-payload",
            confidence=0.81,
            source="scripted_model",
        )

        result = self.slot_store.apply_list_mutation(
            slots,
            mutation,
            required_schema=self.pipeline_req,
            payload_catalog={},
        )

        self.assertFalse(result["success"])
        slot = slots["payload"]
        self.assertEqual(slot.value, [self.cam])
        self.assertEqual(slot.raw_value, "audit:unknown-payload")
        self.assertEqual(slot.source, "scripted_model")
        self.assertEqual(slot.confidence, 0.81)
        self.assertIn(self.unknown, slot.validation_error)

    def test_output_builder_rejects_invalid_or_empty_payload_lists(self):
        field_def = {
            "key": "payload",
            "type": "list",
            "allowed_values": [self.cam, self.sonar],
        }

        valid = self.output_builder._extract_field(
            "payload",
            "list",
            field_def,
            {"payload": [self.cam, self.sonar]},
            "pipeline_inspection",
        )
        coerced = self.output_builder._extract_field(
            "payload",
            "list",
            field_def,
            {"payload": self.cam},
            "pipeline_inspection",
        )
        invalid = self.output_builder._extract_field(
            "payload",
            "list",
            field_def,
            {"payload": [self.cam, self.unknown]},
            "pipeline_inspection",
        )
        empty = self.output_builder._extract_field(
            "payload",
            "list",
            field_def,
            {"payload": []},
            "pipeline_inspection",
        )

        self.assertEqual(valid, [self.cam, self.sonar])
        self.assertEqual(coerced, [self.cam])
        self.assertIsNone(invalid)
        self.assertIsNone(empty)

    def test_dialogue_manager_applies_scripted_add_remove_clear_transactions(self):
        dm, llm = self._seed_pipeline_dm()
        steps = [
            (self._mutation("add", items=[self.cam]), [self.cam], "valid"),
            (
                self._mutation("add", items=[self.sonar]),
                [self.cam, self.sonar],
                "valid",
            ),
            (self._mutation("remove", items=[self.sonar]), [self.cam], "valid"),
            (self._mutation("clear"), [], "missing"),
        ]
        previous_version = dm.slot_store.version

        for mutation, expected_payload, expected_status in steps:
            llm.queue_plan(make_plan("WRITE"))
            llm.queue_extraction(extraction_result(list_mutations=[mutation]))
            dm.process("执行下一条预编排事务")

            payload_slot = dm.slot_store.slots["payload"]
            self.assertEqual(payload_slot.value, expected_payload)
            self.assertEqual(payload_slot.status, expected_status)
            self.assertGreater(dm.slot_store.version, previous_version)
            previous_version = dm.slot_store.version

        self.assertNotIn("payload", dm.slot_store.get_task_state())
        self.assertNotIn("payload", dm._last_built_json)
        self.assertEqual(len(llm.classify_calls), len(steps))
        self.assertEqual(len(llm.extract_calls), len(steps))

    def test_dialogue_manager_rejects_invalid_add_without_overwriting_payload(self):
        dm, llm = self._seed_pipeline_dm([self.cam])
        llm.queue_plan(make_plan("WRITE"))
        llm.queue_extraction(
            extraction_result(
                list_mutations=[
                    self._mutation(
                        "add",
                        items=[self.arm],
                        raw_text="scripted:cross-task-add",
                    )
                ]
            )
        )

        reply = dm.process("执行预编排事务")

        payload_slot = dm.slot_store.slots["payload"]
        self.assertIn("操作失败", reply)
        self.assertEqual(payload_slot.value, [self.cam])
        self.assertEqual(payload_slot.status, "valid")
        self.assertEqual(payload_slot.raw_value, "scripted:cross-task-add")
        self.assertIsNotNone(payload_slot.validation_error)
        self.assertEqual(dm.slot_store.get_task_state()["payload"], [self.cam])
        self.assertEqual(dm._last_built_json["payload"], [self.cam])

    def test_failed_payload_mutation_does_not_discard_other_valid_update(self):
        dm, llm = self._seed_pipeline_dm([self.cam])
        llm.queue_plan(make_plan("WRITE"))
        llm.queue_extraction(
            extraction_result(
                slot_candidate(
                    "water_depth",
                    350.0,
                    raw_value="350米",
                    confidence=0.98,
                ),
                list_mutations=[
                    self._mutation(
                        "add",
                        items=[self.arm],
                        raw_text="scripted:mixed-cross-task-add",
                    )
                ],
            )
        )

        reply = dm.process("执行预编排混合事务")

        state = dm.slot_store.get_task_state()
        payload_slot = dm.slot_store.slots["payload"]
        water_depth_slot = dm.slot_store.slots["water_depth"]
        self.assertEqual(state["water_depth"], 350.0)
        self.assertEqual(water_depth_slot.status, "valid")
        self.assertEqual(water_depth_slot.raw_value, "350米")
        self.assertEqual(payload_slot.value, [self.cam])
        self.assertEqual(payload_slot.status, "valid")
        self.assertIsNotNone(payload_slot.validation_error)
        self.assertIn("载荷操作失败", reply)


if __name__ == "__main__":
    unittest.main()
