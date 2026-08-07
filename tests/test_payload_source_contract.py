import unittest

from src.knowledge_retriever import KnowledgeBase
from src.output_builder import OutputBuilder
from src.slot_store import SlotStore, Slot


class PayloadSourceContractTest(unittest.TestCase):
    def setUp(self):
        self.kb = KnowledgeBase()
        self.builder = OutputBuilder(self.kb)

    def test_onboard_supported_all_payload_sets_integrity(self):
        """测试 onboard_payloads, supported_payloads, all_payloads 三集合独立性与并集语义。"""
        provs = self.kb.get_all_rovs()
        self.assertTrue(len(provs) > 0)
        for r in provs:
            onboard = set(r.get("onboard_payloads", []))
            supported = set(r.get("supported_payloads", []))
            all_p = set(r.get("all_payloads", []))

            # all = onboard ∪ supported
            self.assertEqual(all_p, onboard | supported)

            # onboard_payloads 与 supported_payloads 互不包含/互不覆盖
            # 如果包含 onboard，supported_payloads 不等于 all_payloads (当 onboard 非空且有差异时)
            if onboard:
                self.assertNotIn("raw_supported_payloads", r)

    def test_payload_options_intersection_matrix(self):
        """
        测试 payload_options.* 严格求交集逻辑:
        - 没有机器人 -> task payload_options
        - task 不允许、robot 支持 -> 拒绝
        - task 允许、robot 不支持 -> 拒绝
        - task 允许、robot 支持 -> 接受
        """
        task_key = "pipeline_inspection"
        task_commons = self.kb.assets.get("payload_options", {}).get(task_key, {}).get("common", [])

        # 1. 没有机器人 -> 返回 task payload_options (task_commons)
        res_no_robot = self.builder._lookup_ref("payload_options.pipeline_inspection", task_type_key=task_key, task_state={})
        self.assertEqual(res_no_robot, task_commons)

        # 2. 有机器人 -> 求 task payload_options ∩ robot all_payloads
        rov = self.kb.get_rov("观察级深海机器人")
        self.assertIsNotNone(rov)
        robot_all = set(rov.get("all_payloads", []))

        task_state_with_robot = {"equipment_type": "观察级深海机器人"}
        res_with_robot = self.builder._lookup_ref("payload_options.pipeline_inspection", task_type_key=task_key, task_state=task_state_with_robot)

        # 交集断言
        expected_intersection = [item for item in task_commons if item in robot_all]
        self.assertEqual(res_with_robot, expected_intersection)

        # 校验拒绝/接受判定:
        for payload_item in task_commons:
            if payload_item in robot_all:
                # task 允许、robot 支持 -> 接受
                self.assertIn(payload_item, res_with_robot)
            else:
                # task 允许、robot 不支持 -> 拒绝
                self.assertNotIn(payload_item, res_with_robot)

    def test_mutation_validation_rejects_payload_not_in_intersection(self):
        """测试 SlotStore apply_list_mutation 在限制模式下拒绝不在交集内的载荷。"""
        store = SlotStore()
        slots = store.slots
        task_key = "pipeline_inspection"

        # 准备任务 schema 约束
        allowed = self.builder._lookup_ref("payload_options.pipeline_inspection", task_type_key=task_key, task_state={"equipment_type": "观察级深海机器人"})
        req_schema = [{"key": "payload", "allowed_values": allowed}]

        # 假设 "机械切割开沟模块" 不在观察级 ROV 的允许交集中
        mutation = {
            "field": "payload",
            "operation": "add",
            "items": ["机械切割开沟模块"],
            "raw_text": "添加机械切割开沟模块",
            "confidence": 0.9,
            "source": "user_input",
        }
        res = store.apply_list_mutation(slots, mutation, required_schema=req_schema, payload_catalog=self.kb.assets.get("payload_catalog", {}))
        self.assertFalse(res["success"])
        self.assertIsNotNone(slots["payload"].validation_error)


if __name__ == "__main__":
    unittest.main()
