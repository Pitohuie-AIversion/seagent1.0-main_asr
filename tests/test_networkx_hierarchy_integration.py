import unittest
from src.knowledge_retriever import KnowledgeBase

class TestNetworkXHierarchyIntegration(unittest.TestCase):
    def setUp(self):
        self.kb = KnowledgeBase()

    def test_hierarchy_graph_initialization(self):
        # 1. 验证拓扑图已成功初始化
        self.assertIsNotNone(self.kb.hierarchy_graph, "hierarchy_graph 应该成功构建")
        self.assertTrue(self.kb.hierarchy_graph.number_of_nodes() > 0, "节点数应该大于0")
        self.assertTrue(self.kb.hierarchy_graph.number_of_edges() > 0, "边数应该大于0")

    def test_upward_ancestor_promotion(self):
        # 2. 测试向上反向推导祖先 (Class 祖先)
        class_id = self.kb.get_ancestor_by_level("light_work_class_rov", "class")
        self.assertEqual(class_id, "observation_rov", "light_work_class_rov 向上推导的 Class 必须为 observation_rov")

    def test_downward_descendants_filtering(self):
        # 3. 测试向下求后代子图 (Families 列表)
        families = self.kb.get_descendants_by_level("observation_rov", "family", source_level="class")
        self.assertIn("light_work_class_rov", families, "observation_rov 的下属 Family 必须包含 light_work_class_rov")
        self.assertIn("observation_rov", families, "observation_rov 的下属 Family 必须包含 observation_rov")

    def test_cascade_path_validity(self):
        # 4. 测试跨层级路径合法性校验
        # 有效路径：observation_rov -> light_work_class_rov -> light_work_class_rov_150hp -> LROV-150-001
        self.assertTrue(self.kb.is_valid_cascade_path("observation_rov", "LROV-150-001", ancestor_level="class", descendant_level="unit"))
        self.assertTrue(self.kb.is_valid_cascade_path("light_work_class_rov", "LROV-150-001", ancestor_level="family", descendant_level="unit"))
        
        # 无效路径：auv -> LROV-150-001
        self.assertFalse(self.kb.is_valid_cascade_path("auv", "LROV-150-001", ancestor_level="class", descendant_level="unit"))

if __name__ == "__main__":
    unittest.main()
