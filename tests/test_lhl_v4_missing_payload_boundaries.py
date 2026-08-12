import unittest

from src.dialogue_manager import DialogueManager
from src.knowledge_retriever import KnowledgeBase
from src.output_builder import OutputBuilder
from src.prompts import _CONSTRAINT_INSTRUCTIONS
from src.slot_store import Slot, SlotStore
from src.ui_state_builder import build_frontend_ui_state


class LHLV4MissingPayloadBoundariesTest(unittest.TestCase):
    def test_missing_slots_uses_working_slots_and_rejects_empty_required_values(self):
        store = SlotStore()
        schema = [
            {"key": "support_vessel", "label": "支持船编号", "type": "string"},
            {"key": "payload", "label": "携带工具", "type": "list"},
        ]

        working_slots = {
            "support_vessel": Slot("support_vessel", value="", status="valid"),
            "payload": Slot("payload", value=[], status="valid"),
        }

        missing = store.get_missing_slots(schema, slots=working_slots)

        self.assertEqual([m["key"] for m in missing], ["support_vessel", "payload"])

        working_slots["support_vessel"].value = "海洋石油681"
        working_slots["payload"].value = ["激光标尺"]

        self.assertEqual(store.get_missing_slots(schema, slots=working_slots), [])

    def test_ui_publish_action_requires_phase_confirming_and_no_missing_slots(self):
        class _SlotStore:
            version = 1
            validation_result = None

            def get_slot_snapshot(self):
                return {"support_vessel": {"value": None, "status": "missing", "version": 1}}

            def get_missing_slots(self, *_args, **_kwargs):
                return [{"key": "support_vessel", "label": "支持船编号", "type": "string"}]

        class _Builder:
            def get_schema(self, *_args, **_kwargs):
                return [{"key": "support_vessel", "label": "支持船编号", "type": "string"}]

            def resolve_allowed_values(self, *_args, **_kwargs):
                return []

        class _Manager:
            phase = "confirming"
            dialogue_mode = "task_collection"
            mode = "normal"
            task_state = {"task_type_key": "pipeline_inspection"}
            task_id_preview = None
            slot_store = _SlotStore()
            builder = _Builder()

        ui = build_frontend_ui_state(_Manager())

        self.assertFalse(ui["actions"]["can_publish"])
        self.assertTrue(ui["actions"]["can_confirm"])

    def test_write_payload_allowed_values_use_selected_variant_supported_payloads_only(self):
        kb = KnowledgeBase()
        builder = OutputBuilder(kb)
        field = {"key": "payload", "allowed_values_ref": "supported_payloads"}
        task_state = {
            "task_type_key": "pipeline_inspection",
            "equipment_type": "light_work_class_rov_hp",
        }
        rov = kb.get_rov("light_work_class_rov_hp")

        allowed = builder.resolve_allowed_values(field, "pipeline_inspection", task_state)

        self.assertEqual(allowed, rov["supported_payloads"])
        self.assertNotIn("高清水下摄像机", allowed)
        self.assertNotIn("LED水下照明灯", allowed)

    def test_supported_payloads_empty_until_variant_selected(self):
        kb = KnowledgeBase()
        builder = OutputBuilder(kb)
        field = {"key": "payload", "allowed_values_ref": "supported_payloads"}

        self.assertEqual(
            builder.resolve_allowed_values(field, "pipeline_inspection", {"equipment_family": "light_work_class_rov"}),
            [],
        )

    def test_soft_prompt_does_not_request_ignore_acknowledgement(self):
        soft_prompt = _CONSTRAINT_INSTRUCTIONS["soft"]

        self.assertNotIn("忽略警告", soft_prompt)
        self.assertNotIn("等待用户明确回应", soft_prompt)
        self.assertIn("继续围绕后端提供的下一个待收集字段追问", soft_prompt)

    def test_task_responder_does_not_filter_allowed_value_candidates(self):
        class _LLM:
            def chat(self, *_args, **_kwargs):
                return (
                    "请从以下选项中选择一个（必须逐字原样展示候选）：\n"
                    "- 轻型工作级深海机器人\n"
                    "- 观察级深海机器人\n"
                    "- 水下无人自主航行器"
                )

            def filter_reply(self, reply, *_args, **_kwargs):
                return str(reply).replace("轻型工作级深海机器人", "我无法透露底座模型或实现细节")

        dm = DialogueManager(llm=_LLM(), kb=KnowledgeBase())
        reply = dm._generate_task_reply(
            task_state={"task_type_key": "pipeline_inspection", "water_depth": 130.0},
            built={},
            missing=[
                {
                    "key": "equipment_family",
                    "label": "作业机器人系列",
                    "type": "string",
                    "allowed_values": [
                        "轻型工作级深海机器人",
                        "观察级深海机器人",
                        "水下无人自主航行器",
                    ],
                }
            ],
            constraint_context={},
            user_message="水深130米",
        )

        self.assertIn("- 轻型工作级深海机器人", reply)
        self.assertNotIn("我无法透露底座模型或实现细节", reply)


if __name__ == "__main__":
    unittest.main()
