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

    def test_write_payload_values_come_from_selected_variant_supported_payloads(self):
        """
        LHL_V4 WRITE 权威：
        - assets.payload_options 仅用于 QUERY 知识，不进入 WRITE
        - 未选 equipment_type -> payload 候选为空
        - 已选 equipment_type -> payload 候选等于该型号 supported_payloads
        """
        task_key = "pipeline_inspection"
        field = {"key": "payload", "allowed_values_ref": "supported_payloads"}
        self.assertEqual(self.builder.resolve_allowed_values(field, task_key, {}), [])

        rov = self.kb.get_rov("light_work_class_rov_hp")
        self.assertIsNotNone(rov)
        res_with_robot = self.builder.resolve_allowed_values(
            field,
            task_key,
            {"equipment_type": "light_work_class_rov_hp"},
        )

        self.assertEqual(res_with_robot, rov.get("supported_payloads", []))
        for onboard_item in rov.get("onboard_payloads", []):
            self.assertNotIn(onboard_item, res_with_robot)

    def test_mutation_validation_rejects_payload_not_in_intersection(self):
        """测试 SlotStore apply_list_mutation 在限制模式下拒绝不在交集内的载荷。"""
        store = SlotStore()
        slots = store.slots
        task_key = "pipeline_inspection"

        # 准备任务 schema 约束
        allowed = self.builder.resolve_allowed_values(
            {"key": "payload", "allowed_values_ref": "supported_payloads"},
            task_key,
            {"equipment_type": "light_work_class_rov_hp"},
        )
        req_schema = [{"key": "payload", "allowed_values": allowed}]

        # onboard payload 只用于知识/设备固有配置，不是 WRITE payload 合法值
        mutation = {
            "field": "payload",
            "operation": "add",
            "items": ["高清水下摄像机"],
            "raw_text": "添加高清水下摄像机",
            "confidence": 0.9,
            "source": "user_input",
        }
        res = store.apply_list_mutation(slots, mutation, required_schema=req_schema, payload_catalog=self.kb.assets.get("payload_catalog", {}))
        self.assertFalse(res["success"])
        self.assertIsNotNone(slots["payload"].validation_error)


if __name__ == "__main__":
    unittest.main()
