"""Cross-file contracts for the asset registry and read-only tool queries."""

import unittest

from src.knowledge_retriever import KnowledgeBase


class AssetsContractTest(unittest.TestCase):
    def setUp(self):
        self.kb = KnowledgeBase()

    def _matched_payload_names(self, query: str) -> list[str]:
        evidence = self.kb.execute_typed_query("TOOL_QUERY", query)
        for item in evidence.get("results", []):
            if item.get("category") == "payload_catalog":
                return [
                    payload["name"]
                    for payload in item.get("matched_payloads", [])
                ]
        return []

    def test_every_task_payload_suggestion_has_a_catalog_entry(self):
        catalog_names = {
            item.get("name")
            for item in self.kb.assets.get("payload_catalog", {}).values()
        }

        for task_key, options in self.kb.assets.get("payload_options", {}).items():
            with self.subTest(task_key=task_key):
                self.assertEqual(
                    [],
                    [
                        name
                        for name in options.get("common", [])
                        if name not in catalog_names
                    ],
                )

    def test_specific_tool_queries_prefer_the_exact_catalog_name(self):
        expected_matches = {
            "多波束声呐是什么？": ["多波束声呐"],
            "成像声呐是什么？": ["成像声呐"],
            "双目视觉模块是什么？": ["双目视觉模块"],
            "三维视觉系统是什么？": ["三维视觉系统"],
            "电液机械臂是什么？": ["电液机械臂"],
            "厚度检测传感器是什么？": ["厚度检测传感器"],
        }

        for query, expected in expected_matches.items():
            with self.subTest(query=query):
                self.assertEqual(expected, self._matched_payload_names(query))

    def test_catalog_names_are_not_aliases_of_other_payloads(self):
        catalog = self.kb.assets.get("payload_catalog", {})
        for catalog_id, item in catalog.items():
            name = item.get("name")
            conflicting_ids = [
                other_id
                for other_id, other in catalog.items()
                if other_id != catalog_id and name in other.get("aliases", [])
            ]
            with self.subTest(catalog_id=catalog_id, name=name):
                self.assertEqual([], conflicting_ids)

    def test_vessel_compatibility_mirror_matches_authoritative_records(self):
        vessel_ids = [vessel["id"] for vessel in self.kb.assets["vessels"]]
        self.assertEqual(vessel_ids, self.kb.assets.get("vessel_ids"))


if __name__ == "__main__":
    unittest.main()
