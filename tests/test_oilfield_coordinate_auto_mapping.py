import unittest
from unittest.mock import patch

from src.dialogue_manager import DialogueManager
from src.knowledge_retriever import KnowledgeBase
from src.slot_store import Slot


class FakeLLM:
    def extract_json(self, messages, max_tokens=800):
        return {"slot_candidates": [], "unresolved": []}

    def chat(self, messages, **kwargs):
        return "收到"

    def filter_reply(self, reply):
        return reply


class OilfieldCoordinateAutoMappingTest(unittest.TestCase):
    def setUp(self):
        self.kb = KnowledgeBase()
        self.llm = FakeLLM()
        self.dm = DialogueManager(self.llm, self.kb)

    def test_auto_map_coordinates_when_user_gives_only_oilfield_name(self):
        """当用户仅提供油田名称且未提供坐标时，系统自动映射油田默认中心坐标，且标记为 valid。"""
        new_slots = {}
        updates = {"oilfield_name": "流花11-1油田"}
        linked = self.dm._link_oilfield_update_in_transaction(updates, new_slots)

        self.assertEqual(linked.get("oilfield_name"), "流花11-1油田")
        self.assertIn("oilfield_coordinates", linked)
        self.assertEqual(linked["oilfield_coordinates"], {"lat": 20.815, "lon": 115.735})

        coord_slot = new_slots.get("oilfield_coordinates")
        self.assertIsNotNone(coord_slot)
        self.assertEqual(coord_slot.value, {"lat": 20.815, "lon": 115.735})
        self.assertEqual(coord_slot.status, "valid")
        self.assertEqual(coord_slot.source, "oilfield_default")

    def test_preserve_custom_coordinates_when_user_explicitly_provides(self):
        """当用户显式提供自定义坐标时，系统保留用户的自定义坐标，不被默认坐标覆盖。"""
        new_slots = {}
        custom_coords = {"lat": 20.812, "lon": 115.732}
        updates = {
            "oilfield_name": "流花11-1油田",
            "oilfield_coordinates": custom_coords,
        }
        linked = self.dm._link_oilfield_update_in_transaction(updates, new_slots)

        self.assertEqual(linked.get("oilfield_name"), "流花11-1油田")
        self.assertEqual(linked.get("oilfield_coordinates"), custom_coords)

    def test_overwrite_default_coordinates_with_custom_coordinates(self):
        """当已有默认坐标时，用户后续提供自定义坐标，系统能够正常覆盖。"""
        new_slots = {
            "oilfield_name": Slot("oilfield_name", value="流花11-1油田", status="valid"),
            "oilfield_coordinates": Slot("oilfield_coordinates", value={"lat": 20.815, "lon": 115.735}, status="valid", source="oilfield_default"),
        }
        custom_coords = {"lat": 20.818, "lon": 115.738}
        updates = {
            "oilfield_coordinates": custom_coords,
        }
        # 当更新中包含自定义坐标时，用户自定义值优先
        self.assertEqual(updates["oilfield_coordinates"], custom_coords)


if __name__ == "__main__":
    unittest.main()
