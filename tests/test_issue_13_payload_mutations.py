"""tests/test_issue_13_payload_mutations.py — Issue #13 payload list incremental mutation unit tests."""

import unittest
from unittest.mock import MagicMock, patch

from src.dialogue_manager import DialogueManager
from src.extractor import ParameterExtractor
from src.knowledge_retriever import KnowledgeBase
from src.output_builder import OutputBuilder
from src.slot_store import Slot, SlotStore


class FakeLLMForMutation:
    def __init__(self):
        self.chat_count = 0

    def extract_json(self, messages, max_tokens=800):
        system_text = ""
        last_msg = ""
        if isinstance(messages, list) and messages:
            for m in messages:
                if isinstance(m, dict) and m.get("role") == "system":
                    system_text += str(m.get("content", ""))
            last_msg = str(messages[-1].get("content", ""))
        if "dialogue_mode" in system_text or "意图分流" in system_text or "IntentRouter" in system_text:
            return {
                "dialogue_mode": "task_collection",
                "intent_category": "TASK_UPDATE",
                "confidence": 0.95,
                "reason": "task parameter update",
            }
        if "天鹰座" in last_msg:
            return {
                "slot_candidates": [
                    {"raw_key": "使用设备", "canonical_key": "equipment_type", "raw_value": "天鹰座一号机", "normalized_value": "轻型工作级深海机器人", "confidence": 0.95}
                ],
                "unresolved": []
            }
        return {"slot_candidates": [], "unresolved": []}

    def chat(self, messages, **kwargs):
        self.chat_count += 1
        return "收到，参数已处理。"

    def filter_reply(self, reply):
        return reply


class TestIssue13PayloadMutations(unittest.TestCase):
    def setUp(self):
        self.llm = FakeLLMForMutation()
        self.kb = KnowledgeBase()
        self.extractor = ParameterExtractor(self.llm)
        self.slot_store = SlotStore()
        self.output_builder = OutputBuilder(self.kb)

        self.cam = "高清水下摄像机"
        self.sonar = "前视声呐"
        self.light = "LED水下照明灯"
        self.arm = "多功能液压机械臂"

        self.pipeline_req = [
            {
                "key": "payload",
                "label": "携带工具",
                "type": "list",
                "allowed_values": [
                    "高清水下摄像机",
                    "LED水下照明灯",
                    "激光标尺",
                    "前视声呐",
                    "INS惯性导航系统",
                    "DVL多普勒测速仪",
                    "USBL定位设备",
                    "深度传感器",
                ],
            }
        ]

        self.tree_req = [
            {
                "key": "payload",
                "label": "携带工具",
                "type": "list",
                "allowed_values": [
                    "多功能液压机械臂",
                    "高清水下摄像机",
                    "前视声呐",
                    "专有飞天抓手",
                ],
            }
        ]

    def test_01_extractor_detect_add(self):
        res = self.extractor.extract_updates(
            user_message="再加一个前视声呐",
            current_state={"payload": [self.cam]},
            task_type_key="pipeline_inspection",
            required=self.pipeline_req,
        )
        muts = res.get("list_mutations", [])
        self.assertEqual(len(muts), 1)
        self.assertEqual(muts[0]["operation"], "add")
        self.assertIn(self.sonar, muts[0]["items"])

    def test_02_extractor_detect_remove(self):
        res = self.extractor.extract_updates(
            user_message="去掉前视声呐",
            current_state={"payload": [self.cam, self.sonar]},
            task_type_key="pipeline_inspection",
            required=self.pipeline_req,
        )
        muts = res.get("list_mutations", [])
        self.assertEqual(len(muts), 1)
        self.assertEqual(muts[0]["operation"], "remove")
        self.assertIn(self.sonar, muts[0]["items"])

    def test_03_extractor_detect_replace(self):
        res = self.extractor.extract_updates(
            user_message="把高清水下摄像机换成前视声呐",
            current_state={"payload": [self.cam]},
            task_type_key="pipeline_inspection",
            required=self.pipeline_req,
        )
        muts = res.get("list_mutations", [])
        self.assertEqual(len(muts), 1)
        self.assertEqual(muts[0]["operation"], "replace")
        self.assertIn(self.cam, muts[0]["target_items"])
        self.assertIn(self.sonar, muts[0]["items"])

    def test_04_extractor_detect_clear(self):
        res = self.extractor.extract_updates(
            user_message="所有载荷都不要了",
            current_state={"payload": [self.cam]},
            task_type_key="pipeline_inspection",
            required=self.pipeline_req,
        )
        muts = res.get("list_mutations", [])
        self.assertEqual(len(muts), 1)
        self.assertEqual(muts[0]["operation"], "clear")

    def test_05_extractor_non_explicit_clear_rejected(self):
        res = self.extractor.extract_updates(
            user_message="全部清空",
            current_state={"payload": [self.cam]},
            task_type_key="pipeline_inspection",
            required=self.pipeline_req,
        )
        muts = res.get("list_mutations", [])
        self.assertEqual(len(muts), 0)

    def test_06_extractor_ambiguous_logged(self):
        res = self.extractor.extract_updates(
            user_message="前视声呐",
            current_state={"payload": [self.cam]},
            task_type_key="pipeline_inspection",
            required=self.pipeline_req,
        )
        muts = res.get("list_mutations", [])
        self.assertEqual(len(muts), 0)
        unres = res.get("unresolved", [])
        self.assertTrue(any("缺乏明确的增删改指令" in u for u in unres))

    def test_07_slot_store_add_success(self):
        slots = {"payload": Slot("payload", value=[self.cam], status="valid")}
        mutation = {"field": "payload", "operation": "add", "items": ["前视声呐"], "raw_text": "加个前视声呐"}
        res = self.slot_store.apply_list_mutation(slots, mutation, required_schema=self.pipeline_req)
        self.assertTrue(res["success"])
        self.assertIn(self.cam, slots["payload"].value)
        self.assertIn(self.sonar, slots["payload"].value)

    def test_08_slot_store_add_duplicate(self):
        slots = {"payload": Slot("payload", value=[self.cam], status="valid")}
        mutation = {"field": "payload", "operation": "add", "items": ["摄像机"], "raw_text": "加个摄像机"}
        res = self.slot_store.apply_list_mutation(slots, mutation, required_schema=self.pipeline_req)
        self.assertTrue(res["success"])
        self.assertEqual(slots["payload"].value, [self.cam])

    def test_09_slot_store_remove_missing(self):
        slots = {"payload": Slot("payload", value=[self.cam], status="valid")}
        mutation = {"field": "payload", "operation": "remove", "items": ["前视声呐"], "raw_text": "去掉前视声呐"}
        res = self.slot_store.apply_list_mutation(slots, mutation, required_schema=self.pipeline_req)
        self.assertFalse(res["success"])
        self.assertEqual(slots["payload"].value, [self.cam])
        self.assertIsNotNone(slots["payload"].validation_error)

    def test_10_slot_store_replace_success(self):
        slots = {"payload": Slot("payload", value=[self.cam], status="valid")}
        mutation = {"field": "payload", "operation": "replace", "target_items": ["高清水下摄像机"], "items": ["前视声呐"], "raw_text": "换成前视声呐"}
        res = self.slot_store.apply_list_mutation(slots, mutation, required_schema=self.pipeline_req)
        self.assertTrue(res["success"])
        self.assertEqual(slots["payload"].value, [self.sonar])

    def test_11_slot_store_clear_success(self):
        slots = {"payload": Slot("payload", value=[self.cam], status="valid")}
        mutation = {"field": "payload", "operation": "clear", "raw_text": "清空载荷"}
        res = self.slot_store.apply_list_mutation(slots, mutation, required_schema=self.pipeline_req)
        self.assertTrue(res["success"])
        self.assertEqual(slots["payload"].value, [])
        self.assertEqual(slots["payload"].status, "missing")
        self.assertIsNone(slots["payload"].validation_error)

    def test_12_add_catalog_payload_not_allowed_in_current_task(self):
        slots = {"payload": Slot("payload", value=[self.cam], status="valid")}
        mutation = {"field": "payload", "operation": "add", "items": [self.arm], "raw_text": "再加一个机械臂"}
        res = self.slot_store.apply_list_mutation(slots, mutation, required_schema=self.pipeline_req)
        self.assertFalse(res["success"])
        self.assertEqual(slots["payload"].value, [self.cam])
        self.assertIsNotNone(slots["payload"].validation_error)
        self.assertIn("不属于当前任务允许范围", slots["payload"].validation_error)

    def test_13_unknown_invalid_payload_add_and_replace(self):
        slots = {"payload": Slot("payload", value=[self.cam], status="valid")}

        mut_add = {"field": "payload", "operation": "add", "items": ["无中生有水下切割刀"], "raw_text": "添加无中生有水下切割刀"}
        res_add = self.slot_store.apply_list_mutation(slots, mut_add, required_schema=self.pipeline_req)
        self.assertFalse(res_add["success"])
        self.assertEqual(slots["payload"].value, [self.cam])

        mut_rep = {"field": "payload", "operation": "replace", "target_items": ["高清水下摄像机"], "items": ["未知抓手"], "raw_text": "把摄像机换成未知抓手"}
        res_rep = self.slot_store.apply_list_mutation(slots, mut_rep, required_schema=self.pipeline_req)
        self.assertFalse(res_rep["success"])
        self.assertEqual(slots["payload"].value, [self.cam])

    def test_14_output_builder_strict_list_validation(self):
        field_def = {"key": "payload", "type": "list", "allowed_values": [self.cam, self.sonar]}

        out_valid = self.output_builder._extract_field(
            key="payload", ftype="list", field_def=field_def, task_state={"payload": [self.cam, self.sonar]}, task_type_key="pipeline_inspection"
        )
        self.assertEqual(out_valid, [self.cam, self.sonar])

        out_invalid = self.output_builder._extract_field(
            key="payload", ftype="list", field_def=field_def, task_state={"payload": [self.cam, "未知非法载荷"]}, task_type_key="pipeline_inspection"
        )
        self.assertIsNone(out_invalid)

        out_empty = self.output_builder._extract_field(
            key="payload", ftype="list", field_def=field_def, task_state={"payload": []}, task_type_key="pipeline_inspection"
        )
        self.assertIsNone(out_empty)

    def test_15_dialogue_manager_multiturn(self):
        dm = DialogueManager(llm=self.llm, kb=self.kb)
        dm.task_state["task_type_key"] = "pipeline_inspection"
        dm.slot_store.slots["task_type_key"] = Slot("task_type_key", value="pipeline_inspection", status="valid")
        dm.phase = "collecting"

        dm.process("添加高清水下摄像机")

        payload_slot = dm.slot_store.slots["payload"]
        task_state_val = dm.slot_store.get_task_state().get("payload")
        built_val = dm._last_built_json.get("payload") if dm._last_built_json else None

        self.assertIn(self.cam, payload_slot.value or [])
        self.assertEqual(payload_slot.status, "valid")
        self.assertIn(self.cam, task_state_val or [])
        self.assertIn(self.cam, built_val or [])

        dm.process("再加一个前视声呐")

        payload_slot = dm.slot_store.slots["payload"]
        task_state_val = dm.slot_store.get_task_state().get("payload")
        built_val = dm._last_built_json.get("payload") if dm._last_built_json else None

        self.assertIn(self.cam, payload_slot.value)
        self.assertIn(self.sonar, payload_slot.value)
        self.assertEqual(payload_slot.status, "valid")
        self.assertIn(self.sonar, task_state_val)
        self.assertIn(self.sonar, built_val)

        dm.process("去掉前视声呐")

        payload_slot = dm.slot_store.slots["payload"]
        task_state_val = dm.slot_store.get_task_state().get("payload")
        built_val = dm._last_built_json.get("payload") if dm._last_built_json else None

        self.assertEqual(payload_slot.value, [self.cam])
        self.assertEqual(payload_slot.status, "valid")
        self.assertEqual(task_state_val, [self.cam])
        self.assertEqual(built_val, [self.cam])

        dm.process("所有载荷都不要了")

        payload_slot = dm.slot_store.slots["payload"]
        task_state_val = dm.slot_store.get_task_state().get("payload")
        built_val = dm._last_built_json.get("payload") if dm._last_built_json else None

        self.assertEqual(payload_slot.value, [])
        self.assertEqual(payload_slot.status, "missing")
        self.assertIsNone(built_val)

    def test_16_dialogue_manager_invalid_add_rejection(self):
        dm = DialogueManager(llm=self.llm, kb=self.kb)
        dm.task_state["task_type_key"] = "pipeline_inspection"
        dm.slot_store.slots["task_type_key"] = Slot("task_type_key", value="pipeline_inspection", status="valid")
        dm.phase = "collecting"

        dm.process("添加高清水下摄像机")

        reply = dm.process("再加一个机械臂")

        self.assertIn("操作失败", reply)
        payload_slot = dm.slot_store.slots["payload"]
        task_state_val = dm.slot_store.get_task_state().get("payload")
        built_val = dm._last_built_json.get("payload") if dm._last_built_json else None

        self.assertEqual(payload_slot.value, [self.cam])
        self.assertEqual(payload_slot.status, "valid")
        self.assertEqual(task_state_val, [self.cam])
        self.assertEqual(built_val, [self.cam])

    def test_17_asr_text_flow_parity(self):
        dm = DialogueManager(llm=self.llm, kb=self.kb)
        dm.task_state["task_type_key"] = "pipeline_inspection"
        dm.slot_store.slots["task_type_key"] = Slot("task_type_key", value="pipeline_inspection", status="valid")
        dm.phase = "collecting"

        dm.process("添加高清水下摄像机")

        asr_transcribed_text = "再加一个前视声呐"
        dm.process(asr_transcribed_text)

        payload_slot = dm.slot_store.slots["payload"]
        self.assertIn(self.sonar, payload_slot.value)

    def test_18_unknown_add_payload_via_extractor_and_dm(self):
        dm = DialogueManager(llm=self.llm, kb=self.kb)
        dm.task_state["task_type_key"] = "pipeline_inspection"
        dm.slot_store.slots["task_type_key"] = Slot("task_type_key", value="pipeline_inspection", status="valid")
        dm.phase = "collecting"

        dm.process("添加高清水下摄像机")

        dm.process("添加无中生有水下切割刀")

        payload_slot = dm.slot_store.slots["payload"]
        self.assertEqual(payload_slot.value, [self.cam])
        self.assertIsNotNone(payload_slot.validation_error)
        self.assertIn("无中生有水下切割刀", payload_slot.validation_error)

    def test_19_unknown_replace_payload_via_extractor_and_dm(self):
        dm = DialogueManager(llm=self.llm, kb=self.kb)
        dm.task_state["task_type_key"] = "pipeline_inspection"
        dm.slot_store.slots["task_type_key"] = Slot("task_type_key", value="pipeline_inspection", status="valid")
        dm.phase = "collecting"

        dm.process("添加高清水下摄像机")

        dm.process("把高清水下摄像机换成未知工具")

        payload_slot = dm.slot_store.slots["payload"]
        self.assertEqual(payload_slot.value, [self.cam])
        self.assertEqual(payload_slot.status, "valid")
        self.assertIsNotNone(payload_slot.validation_error)

    def test_20_mixed_turn_payload_failure_preserves_equipment(self):
        dm = DialogueManager(llm=self.llm, kb=self.kb)
        dm.task_state["task_type_key"] = "pipeline_inspection"
        dm.slot_store.slots["task_type_key"] = Slot("task_type_key", value="pipeline_inspection", status="valid")
        dm.phase = "collecting"

        dm.process("使用天鹰座一号机，携带机械臂和前视声呐")

        eq_slot = dm.slot_store.slots.get("equipment_type")
        self.assertIsNotNone(eq_slot)
        self.assertEqual(eq_slot.value, "轻型工作级深海机器人")
        self.assertEqual(dm.slot_store.get_task_state().get("equipment_type"), "轻型工作级深海机器人")

    def test_21_catalog_alias_resolves_to_task_canonical_value(self):
        dm = DialogueManager(llm=self.llm, kb=self.kb)
        dm.task_state["task_type_key"] = "tree_valve_operation"
        dm.slot_store.slots["task_type_key"] = Slot("task_type_key", value="tree_valve_operation", status="valid")
        dm.phase = "collecting"

        dm.process("添加 LED水下照明灯")

        payload_slot = dm.slot_store.slots["payload"]
        self.assertEqual(payload_slot.value, ["LED水下照明灯"])
        self.assertEqual(payload_slot.status, "valid")

    def test_22_targeted_removal_not_misidentified_as_clear(self):
        dm = DialogueManager(llm=self.llm, kb=self.kb)
        dm.task_state["task_type_key"] = "pipeline_inspection"
        dm.slot_store.slots["task_type_key"] = Slot("task_type_key", value="pipeline_inspection", status="valid")
        dm.phase = "collecting"

        dm.process("添加高清水下摄像机和前视声呐")

        dm.process("不需要载荷中的前视声呐")

        payload_slot = dm.slot_store.slots["payload"]
        self.assertEqual(payload_slot.value, [self.cam])
        self.assertEqual(payload_slot.status, "valid")

    def test_23_failed_mutation_audit_metadata(self):
        store = SlotStore()
        slots = store.slots
        mutation = {
            "field": "payload",
            "operation": "add",
            "items": ["未知设备"],
            "raw_text": "添加未知设备",
            "confidence": 0.9,
            "source": "user_input",
        }
        res = store.apply_list_mutation(
            slots,
            mutation,
            required_schema=[{"key": "payload", "allowed_values": ["高清水下摄像机"]}],
            payload_catalog={},
        )
        self.assertFalse(res["success"])
        slot = slots["payload"]
        self.assertEqual(slot.raw_value, "添加未知设备")
        self.assertEqual(slot.source, "user_input")
        self.assertEqual(slot.confidence, 0.9)
        self.assertIsNotNone(slot.validation_error)

    def test_24_exact_task_allowed_value_not_collapsed_by_catalog_alias(self):
        dm = DialogueManager(llm=self.llm, kb=self.kb)
        dm.task_state["task_type_key"] = "tree_valve_operation"
        dm.slot_store.slots["task_type_key"] = Slot("task_type_key", value="tree_valve_operation", status="valid")
        dm.phase = "collecting"

        dm.process("添加电液机械臂")

        payload_slot = dm.slot_store.slots["payload"]
        self.assertEqual(payload_slot.value, ["电液机械臂"])
        self.assertEqual(payload_slot.status, "valid")

    def test_25_natural_language_targeted_removals(self):
        phrases = ["删除载荷里的前视声呐", "删除载荷前视声呐", "放弃工具里的前视声呐"]
        for phrase in phrases:
            dm = DialogueManager(llm=self.llm, kb=self.kb)
            dm.task_state["task_type_key"] = "pipeline_inspection"
            dm.slot_store.slots["task_type_key"] = Slot("task_type_key", value="pipeline_inspection", status="valid")
            dm.phase = "collecting"

            dm.process("添加高清水下摄像机和前视声呐")
            dm.process(phrase)

            payload_slot = dm.slot_store.slots["payload"]
            self.assertEqual(payload_slot.value, [self.cam], f"Phrase '{phrase}' failed to perform targeted removal.")
            self.assertEqual(payload_slot.status, "valid")

    def test_26_optional_suffix_canonical_mapping(self):
        dm = DialogueManager(llm=self.llm, kb=self.kb)
        dm.task_state["task_type_key"] = "tree_valve_operation"
        dm.slot_store.slots["task_type_key"] = Slot("task_type_key", value="tree_valve_operation", status="valid")
        dm.phase = "collecting"

        dm.process("添加双目视觉模块")

        payload_slot = dm.slot_store.slots["payload"]
        self.assertEqual(payload_slot.value, ["双目视觉模块"])
        self.assertEqual(payload_slot.status, "valid")
        self.assertNotEqual(payload_slot.value, ["高清水下摄像机"])

    def test_27_optional_suffix_burial_laser_ruler(self):
        dm = DialogueManager(llm=self.llm, kb=self.kb)
        dm.task_state["task_type_key"] = "pipeline_burial"
        dm.slot_store.slots["task_type_key"] = Slot("task_type_key", value="pipeline_burial", status="valid")
        dm.phase = "collecting"

        dm.process("添加激光标尺")

        payload_slot = dm.slot_store.slots["payload"]
        self.assertEqual(payload_slot.value, ["激光标尺"])
        self.assertEqual(payload_slot.status, "valid")

    def test_28_optional_suffix_remove_existing(self):
        dm = DialogueManager(llm=self.llm, kb=self.kb)
        dm.task_state["task_type_key"] = "pipeline_burial"
        dm.slot_store.slots["task_type_key"] = Slot("task_type_key", value="pipeline_burial", status="valid")
        dm.phase = "collecting"

        dm.process("添加机械切割开沟模块")
        dm.process("去掉机械切割开沟模块")

        payload_slot = dm.slot_store.slots["payload"]
        self.assertEqual(payload_slot.value, [])
        self.assertEqual(payload_slot.status, "missing")

    def test_29_scoped_removal_with_all_quantifier(self):
        dm = DialogueManager(llm=self.llm, kb=self.kb)
        dm.task_state["task_type_key"] = "pipeline_inspection"
        dm.slot_store.slots["task_type_key"] = Slot("task_type_key", value="pipeline_inspection", status="valid")
        dm.phase = "collecting"

        dm.process("添加高清水下摄像机和前视声呐")
        dm.process("删除所有载荷里的前视声呐")

        payload_slot = dm.slot_store.slots["payload"]
        self.assertEqual(payload_slot.value, [self.cam])
        self.assertEqual(payload_slot.status, "valid")

    def test_30_scoped_removal_with_but_keep_phrase(self):
        dm = DialogueManager(llm=self.llm, kb=self.kb)
        dm.task_state["task_type_key"] = "pipeline_inspection"
        dm.slot_store.slots["task_type_key"] = Slot("task_type_key", value="pipeline_inspection", status="valid")
        dm.phase = "collecting"

        dm.process("添加高清水下摄像机和前视声呐")
        dm.process("删除所有载荷中的前视声呐，但保留摄像机")

        payload_slot = dm.slot_store.slots["payload"]
        self.assertEqual(payload_slot.value, [self.cam])
        self.assertEqual(payload_slot.status, "valid")

    def test_31_pure_clear_still_clears(self):
        dm = DialogueManager(llm=self.llm, kb=self.kb)
        dm.task_state["task_type_key"] = "pipeline_inspection"
        dm.slot_store.slots["task_type_key"] = Slot("task_type_key", value="pipeline_inspection", status="valid")
        dm.phase = "collecting"

        dm.process("添加高清水下摄像机和前视声呐")
        dm.process("删除所有载荷")

        payload_slot = dm.slot_store.slots["payload"]
        self.assertEqual(payload_slot.value, [])
        self.assertEqual(payload_slot.status, "missing")


if __name__ == "__main__":
    unittest.main()
