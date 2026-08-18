import json
import unittest

from src.dialogue_manager import DialogueManager, sanitize_user_facing_json
from src.knowledge_retriever import KnowledgeBase
from src.slot_store import SlotStore, Slot


class FakeLLM:
    def extract_json(self, messages, max_tokens=800):
        return {"slot_candidates": [], "unresolved": []}

    def chat(self, messages, **kwargs):
        return "收到"

    def filter_reply(self, reply):
        return reply


class UserFacingJsonSanitizationTest(unittest.TestCase):
    def setUp(self):
        self.kb = KnowledgeBase()
        self.llm = FakeLLM()
        self.dm = DialogueManager(self.llm, self.kb)

    def test_sanitize_user_facing_json_filters_internal_audit_keys(self):
        """测试 sanitize_user_facing_json 强制过滤后台匹配与审计过程 key。"""
        polluted_data = {
            "task_type": "采油树控制面板拔出",
            "task_id": "CT-20260817-010",
            "oilfield_name": "文昌16-2油田",
            "raw_oilfield_name": "文昌16-2",
            "oilfield_match_status": "accepted",
            "oilfield_match_confidence": 1.0,
            "oilfield_match_evidence": ["命中标准名"],
            "oilfield_match_candidates": [{"id": "wencang_16_2", "name": "文昌16-2油田"}],
            "pending_oilfield_name": "文昌16-2",
            "_rov_candidates": ["WROV-250-001"],
            "water_depth": 150.0,
        }

        sanitized = sanitize_user_facing_json(polluted_data)

        self.assertIn("task_type", sanitized)
        self.assertIn("task_id", sanitized)
        self.assertIn("oilfield_name", sanitized)
        self.assertIn("water_depth", sanitized)

        # 验证所有内部过程/审计字段已被净化过滤
        self.assertNotIn("raw_oilfield_name", sanitized)
        self.assertNotIn("oilfield_match_status", sanitized)
        self.assertNotIn("oilfield_match_confidence", sanitized)
        self.assertNotIn("oilfield_match_evidence", sanitized)
        self.assertNotIn("oilfield_match_candidates", sanitized)
        self.assertNotIn("pending_oilfield_name", sanitized)
        self.assertNotIn("_rov_candidates", sanitized)

    def test_slot_store_get_built_json_default_excludes_audit_keys(self):
        """测试 SlotStore.get_built_json() 默认也不导出内部匹配审计槽位。"""
        store = SlotStore(self.kb)
        store.slots["oilfield_name"] = Slot("oilfield_name", value="文昌16-2油田", status="valid")
        store.slots["raw_oilfield_name"] = Slot("raw_oilfield_name", value="文昌16-2", status="valid")
        store.slots["oilfield_match_evidence"] = Slot("oilfield_match_evidence", value=["命中证据"], status="valid")
        store.slots["oilfield_match_confidence"] = Slot("oilfield_match_confidence", value=1.0, status="valid")

        built = store.get_built_json()

        self.assertEqual(built.get("oilfield_name"), "文昌16-2油田")
        self.assertNotIn("raw_oilfield_name", built)
        self.assertNotIn("oilfield_match_evidence", built)
        self.assertNotIn("oilfield_match_confidence", built)


if __name__ == "__main__":
    unittest.main()
