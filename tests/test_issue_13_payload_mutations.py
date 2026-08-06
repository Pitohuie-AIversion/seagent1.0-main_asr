"""
tests/test_issue_13_payload_mutations.py — GitHub Issue #13 payload list mutations test suite
"""

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.knowledge_retriever import KnowledgeBase
from src.output_builder import OutputBuilder
from src.extractor import ParameterExtractor
from src.slot_store import SlotStore, Slot
from src.dialogue_manager import DialogueManager


class FakeLLMForMutation:
    def __init__(self):
        self.chat_count = 0

    def extract_json(self, messages, max_tokens=800):
        system_text = ""
        if isinstance(messages, list) and messages:
            for m in messages:
                if isinstance(m, dict) and m.get("role") == "system":
                    system_text += str(m.get("content", ""))
        if "dialogue_mode" in system_text or "意图分流" in system_text or "IntentRouter" in system_text:
            return {
                "dialogue_mode": "task_collection",
                "intent_category": "TASK_UPDATE",
                "confidence": 0.95,
                "reason": "task parameter update",
            }
        return {"slot_candidates": [], "unresolved": []}

    def chat(self, messages, **kwargs):
        self.chat_count += 1
        return "收到，参数已处理。"

    def filter_reply(self, reply):
        return reply


class TestIssue13PayloadMutations(unittest.TestCase):
    def setUp(self):
        self.kb = KnowledgeBase()
        self.builder = OutputBuilder(self.kb)
        self.slot_store = SlotStore(self.kb)
        self.llm = FakeLLMForMutation()
        self.extractor = ParameterExtractor(self.llm)

        # Real canonical names from assets.yaml
        self.cam = "高清水下摄像机"
        self.sonar = "前视声呐"
        self.arm = "多功能液压机械臂"

    def test_01_add_payload(self):
        """1. add：已有摄像机，再加机械臂"""
        slots = self.slot_store.clone_slots()
        slots["payload"] = Slot("payload", value=[self.cam], value_type="list", status="valid")

        mutation = {
            "field": "payload",
            "operation": "add",
            "items": [self.arm],
            "raw_text": "再加一个机械臂",
            "confidence": 0.95,
            "source": "user_input",
        }
        res = self.slot_store.apply_list_mutation(slots, mutation)
        self.assertTrue(res["success"])
        self.assertTrue(res["changed"])
        self.assertEqual(res["new_value"], [self.cam, self.arm])

    def test_02_duplicate_add(self):
        """2. duplicate add：同一载荷或其 alias 不重复"""
        slots = self.slot_store.clone_slots()
        slots["payload"] = Slot("payload", value=[self.cam, self.arm], value_type="list", status="valid")

        mutation = {
            "field": "payload",
            "operation": "add",
            "items": ["机械臂"],
            "raw_text": "添加机械臂",
            "confidence": 0.95,
            "source": "user_input",
        }
        res = self.slot_store.apply_list_mutation(slots, mutation)
        self.assertTrue(res["success"])
        self.assertFalse(res["changed"])
        self.assertEqual(res["new_value"], [self.cam, self.arm])

    def test_03_remove_payload(self):
        """3. remove：只删除指定载荷"""
        slots = self.slot_store.clone_slots()
        slots["payload"] = Slot("payload", value=[self.cam, self.sonar, self.arm], value_type="list", status="valid")

        mutation = {
            "field": "payload",
            "operation": "remove",
            "items": [self.sonar],
            "raw_text": "去掉前视声呐",
            "confidence": 0.95,
            "source": "user_input",
        }
        res = self.slot_store.apply_list_mutation(slots, mutation)
        self.assertTrue(res["success"])
        self.assertTrue(res["changed"])
        self.assertEqual(res["new_value"], [self.cam, self.arm])

    def test_04_remove_missing(self):
        """4. remove missing：原列表不变并记录 validation_error"""
        slots = self.slot_store.clone_slots()
        slots["payload"] = Slot("payload", value=[self.cam], value_type="list", status="valid")

        mutation = {
            "field": "payload",
            "operation": "remove",
            "items": [self.sonar],
            "raw_text": "去掉前视声呐",
            "confidence": 0.95,
            "source": "user_input",
        }
        res = self.slot_store.apply_list_mutation(slots, mutation)
        self.assertFalse(res["success"])
        self.assertFalse(res["changed"])
        self.assertEqual(slots["payload"].value, [self.cam])
        self.assertEqual(slots["payload"].status, "valid")
        self.assertIsNotNone(slots["payload"].validation_error)
        self.assertIn("不在当前列表中", slots["payload"].validation_error)

    def test_05_replace_payload(self):
        """5. replace：摄像机原子替换为机械臂"""
        slots = self.slot_store.clone_slots()
        slots["payload"] = Slot("payload", value=[self.cam, self.sonar], value_type="list", status="valid")

        mutation = {
            "field": "payload",
            "operation": "replace",
            "target_items": [self.cam],
            "items": [self.arm],
            "raw_text": "把高清水下摄像机换成机械臂",
            "confidence": 0.95,
            "source": "user_input",
        }
        res = self.slot_store.apply_list_mutation(slots, mutation)
        self.assertTrue(res["success"])
        self.assertEqual(res["new_value"], [self.sonar, self.arm])

    def test_06_replace_missing_target(self):
        """6. replace missing target：原列表不变"""
        slots = self.slot_store.clone_slots()
        slots["payload"] = Slot("payload", value=[self.sonar], value_type="list", status="valid")

        mutation = {
            "field": "payload",
            "operation": "replace",
            "target_items": [self.cam],
            "items": [self.arm],
            "raw_text": "把高清水下摄像机换成机械臂",
            "confidence": 0.95,
            "source": "user_input",
        }
        res = self.slot_store.apply_list_mutation(slots, mutation)
        self.assertFalse(res["success"])
        self.assertEqual(slots["payload"].value, [self.sonar])
        self.assertEqual(slots["payload"].status, "valid")

    def test_07_replace_invalid_new_item(self):
        """7. replace invalid new item：原列表不变"""
        slots = self.slot_store.clone_slots()
        slots["payload"] = Slot("payload", value=[self.cam], value_type="list", status="valid")

        required = [{"key": "payload", "allowed_values": [self.cam, self.sonar, self.arm]}]
        mutation = {
            "field": "payload",
            "operation": "replace",
            "target_items": [self.cam],
            "items": ["非法神奇挖掘爪"],
            "raw_text": "把高清水下摄像机换成非法神奇挖掘爪",
            "confidence": 0.95,
            "source": "user_input",
        }
        res = self.slot_store.apply_list_mutation(slots, mutation, required_schema=required)
        self.assertFalse(res["success"])
        self.assertEqual(slots["payload"].value, [self.cam])

    def test_08_clear_payload(self):
        """8. clear：明确清空全部载荷"""
        slots = self.slot_store.clone_slots()
        slots["payload"] = Slot("payload", value=[self.cam, self.sonar], value_type="list", status="valid")

        mutation = {
            "field": "payload",
            "operation": "clear",
            "items": [],
            "target_items": [],
            "raw_text": "所有载荷都不要了",
            "confidence": 0.95,
            "source": "user_input",
        }
        res = self.slot_store.apply_list_mutation(slots, mutation)
        self.assertTrue(res["success"])
        self.assertEqual(res["new_value"], [])
        self.assertEqual(slots["payload"].value, [])

    def test_09_non_explicit_clear_does_not_clear(self):
        """9. 非明确 clear 表达不得清空"""
        res = self.extractor.extract_updates(
            user_message="我认为载荷可能有点多",
            current_state={"payload": [self.cam]},
            task_type_key="pipeline_inspection",
        )
        self.assertEqual(res.get("list_mutations"), [])

    def test_10_ambiguous_single_item_no_overwrite(self):
        """10. 已有列表时仅输入“机械臂”不得覆盖"""
        res = self.extractor.extract_updates(
            user_message="机械臂",
            current_state={"payload": [self.cam]},
            task_type_key="pipeline_inspection",
        )
        self.assertEqual(res.get("list_mutations"), [])
        self.assertTrue(any("缺乏明确的增删改指令" in u for u in res.get("unresolved", [])))

    def test_11_initial_payload_add(self):
        """11. 初始 payload missing 时直接回答合法载荷能够初始化"""
        res = self.extractor.extract_updates(
            user_message="高清水下摄像机",
            current_state={"payload": None},
            task_type_key="pipeline_inspection",
        )
        muts = res.get("list_mutations", [])
        self.assertEqual(len(muts), 1)
        self.assertEqual(muts[0]["operation"], "add")
        self.assertEqual(muts[0]["items"], [self.cam])

    def test_12_alias_canonical_mapping(self):
        """12. alias 映射到 canonical name"""
        slots = self.slot_store.clone_slots()
        slots["payload"] = Slot("payload", value=[], value_type="list", status="missing")

        mutation = {
            "field": "payload",
            "operation": "add",
            "items": ["机械臂"],
            "raw_text": "添加机械臂",
            "confidence": 0.95,
            "source": "user_input",
        }
        res = self.slot_store.apply_list_mutation(slots, mutation)
        self.assertTrue(res["success"])
        self.assertEqual(slots["payload"].value, [self.arm])

    def test_13_id_canonical_deduplication(self):
        """13. ID/canonical name 去重"""
        slots = self.slot_store.clone_slots()
        slots["payload"] = Slot("payload", value=[self.arm], value_type="list", status="valid")

        mutation = {
            "field": "payload",
            "operation": "add",
            "items": ["电液机械臂"],
            "raw_text": "添加电液机械臂",
            "confidence": 0.95,
            "source": "user_input",
        }
        res = self.slot_store.apply_list_mutation(slots, mutation)
        self.assertTrue(res["success"])
        self.assertEqual(slots["payload"].value, [self.arm])

    def test_14_metadata_update_on_success(self):
        """14. mutation 成功后 slot version、updated_at、source、raw_value、confidence 正确更新"""
        self.slot_store.slots.setdefault("payload", Slot("payload", value_type="list", status="missing"))
        new_slots, new_unres, exp_ver = self.slot_store.snapshot()
        old_ver = new_slots["payload"].version

        mutation = {
            "field": "payload",
            "operation": "add",
            "items": [self.cam],
            "raw_text": "添加高清水下摄像机",
            "confidence": 0.98,
            "source": "user_input",
        }
        res = self.slot_store.apply_list_mutation(new_slots, mutation)
        self.assertTrue(res["success"])
        self.slot_store.commit_transaction(new_slots, new_unres, expected_version=exp_ver)

        updated_slot = self.slot_store.slots["payload"]
        self.assertEqual(updated_slot.value, [self.cam])
        self.assertEqual(updated_slot.raw_value, "添加高清水下摄像机")
        self.assertEqual(updated_slot.confidence, 0.98)
        self.assertEqual(updated_slot.version, old_ver + 1)

    def test_15_metadata_preserved_on_failure(self):
        """15. mutation 失败后 value 和 valid 状态保留"""
        new_slots, new_unres, exp_ver = self.slot_store.snapshot()
        new_slots["payload"] = Slot("payload", value=[self.cam], value_type="list", status="valid")
        self.slot_store.commit_transaction(new_slots, new_unres, expected_version=exp_ver)

        new_slots, new_unres, exp_ver = self.slot_store.snapshot()
        mutation = {
            "field": "payload",
            "operation": "remove",
            "items": [self.sonar],
            "raw_text": "去掉前视声呐",
            "confidence": 0.95,
            "source": "user_input",
        }
        res = self.slot_store.apply_list_mutation(new_slots, mutation)
        self.assertFalse(res["success"])
        self.slot_store.commit_transaction(new_slots, new_unres, expected_version=exp_ver)

        stored_slot = self.slot_store.slots["payload"]
        self.assertEqual(stored_slot.value, [self.cam])
        self.assertEqual(stored_slot.status, "valid")
        self.assertIsNotNone(stored_slot.validation_error)

    def test_16_snapshot_export_restore(self):
        """16. snapshot 导出与恢复后 payload 和元数据一致"""
        new_slots, new_unres, exp_ver = self.slot_store.snapshot()
        mutation = {
            "field": "payload",
            "operation": "add",
            "items": [self.cam, self.arm],
            "raw_text": "配备高清水下摄像机和机械臂",
            "confidence": 0.95,
            "source": "user_input",
        }
        self.slot_store.apply_list_mutation(new_slots, mutation)
        self.slot_store.commit_transaction(new_slots, new_unres, expected_version=exp_ver)

        exported = self.slot_store.export_snapshot()
        new_store = SlotStore.from_snapshot(exported, kb=self.kb)
        self.assertEqual(new_store.slots["payload"].value, [self.cam, self.arm])
        self.assertEqual(new_store.slots["payload"].raw_value, "配备高清水下摄像机和机械臂")

    def test_17_dialogue_manager_multiturn(self):
        """17. DialogueManager 多轮 add/remove/replace/clear"""
        dm = DialogueManager(llm=self.llm, kb=self.kb)
        dm.task_state["task_type_key"] = "pipeline_inspection"
        dm.slot_store.slots["task_type_key"] = Slot("task_type_key", value="pipeline_inspection", status="valid")
        dm.phase = "collecting"

        # Round 1: add cam
        dm.process("添加高清水下摄像机")
        self.assertIn(self.cam, dm.slot_store.slots["payload"].value or [])

        # Round 2: add arm
        dm.process("再加一个机械臂")
        payload = dm.slot_store.slots["payload"].value
        self.assertIn(self.cam, payload)
        self.assertIn(self.arm, payload)

        # Round 3: replace cam with sonar
        dm.process("把高清水下摄像机换成前视声呐")
        payload = dm.slot_store.slots["payload"].value
        self.assertNotIn(self.cam, payload)
        self.assertIn(self.sonar, payload)
        self.assertIn(self.arm, payload)

        # Round 4: clear
        dm.process("所有载荷都不要了")
        payload = dm.slot_store.slots["payload"].value
        self.assertEqual(payload, [])

    def test_18_output_builder_parity(self):
        """18. OutputBuilder 最终 payload 与 SlotStore 完全一致"""
        field_def = {"key": "payload", "label": "携带工具", "type": "list", "allowed_values": [self.cam, self.arm]}
        task_state = {"payload": [self.cam, self.arm]}
        built = self.builder._extract_field("payload", "list", field_def, task_state, "pipeline_inspection")
        self.assertEqual(built, [self.cam, self.arm])

        # Test invalid element returning None
        invalid_state = {"payload": [self.cam, "非法工具"]}
        built_invalid = self.builder._extract_field("payload", "list", field_def, invalid_state, "pipeline_inspection")
        self.assertIsNone(built_invalid)

    def test_19_general_llm_chat_isolation(self):
        """19. 普通 LLM 对话不写入 payload"""
        dm = DialogueManager(llm=self.llm, kb=self.kb)
        initial_payload = dm.slot_store.slots.get("payload").value if dm.slot_store.slots.get("payload") else None
        dm.process("你好，请问今天天气怎么样？")
        current_payload = dm.slot_store.slots.get("payload").value if dm.slot_store.slots.get("payload") else None
        self.assertEqual(current_payload, initial_payload)

    def test_20_asr_text_flow_parity(self):
        """20. ASR 转写后的任务文本走同一 mutation 逻辑"""
        dm = DialogueManager(llm=self.llm, kb=self.kb)
        dm.task_state["task_type_key"] = "pipeline_inspection"
        dm.slot_store.slots["task_type_key"] = Slot("task_type_key", value="pipeline_inspection", status="valid")
        dm.phase = "collecting"

        asr_transcribed_text = "再加一个机械臂"
        dm.process(asr_transcribed_text)
        payload = dm.slot_store.slots["payload"].value
        self.assertIn(self.arm, payload or [])


if __name__ == "__main__":
    unittest.main()
