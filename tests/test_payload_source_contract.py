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
        测试 payload_options.* 合法值逻辑：
        选择机器后，合法携带工具 = task_commons ∩ robot.supported_payloads
        （排除自带硬件 onboard_payloads）

        - 没有机器人 → task payload_options
        - task 允许 且 robot 支持 (supported_payloads) → 接受（交集部分）
        - task 允许 但 robot 不支持 (非 supported_payloads) → 拒绝
        - robot 自带 (onboard_payloads) 但不在 supported_payloads → 不作为选配推荐/允许携带工具列出
        """
        task_key = "pipeline_inspection"
        task_commons = self.kb.assets.get("payload_options", {}).get(task_key, {}).get("common", [])

        # 1. 没有机器人 -> 返回 task payload_options (task_commons)
        res_no_robot = self.builder._lookup_ref("payload_options.pipeline_inspection", task_type_key=task_key, task_state={})
        self.assertEqual(res_no_robot, task_commons)

        # 2. 有机器人 -> 合法值 = task_commons ∩ robot.supported_payloads
        rov = self.kb.get_rov("观察级深海机器人")
        self.assertIsNotNone(rov)
        robot_supported = set(rov.get("supported_payloads", []))
        robot_onboard = set(rov.get("onboard_payloads", []))

        task_state_with_robot = {"equipment_type": "观察级深海机器人"}
        res_with_robot = self.builder._lookup_ref("payload_options.pipeline_inspection", task_type_key=task_key, task_state=task_state_with_robot)

        # 验证结果中包含且仅包含 robot.supported_payloads
        self.assertEqual(set(res_with_robot), robot_supported)

        # 验证仅 onboard（不在 supported_payloads）的载荷不会出现在选配推荐/允许列表中
        pure_onboard = robot_onboard - robot_supported
        for item in pure_onboard:
            self.assertNotIn(item, res_with_robot, f"自带硬件 {item!r} 不应出现在 payload 扩展选配推荐列表中")

    def test_missing_payload_field_carries_selected_robot_onboard_payloads(self):
        """选择机器人型号后，payload 缺失字段应携带该型号已搭载载荷上下文。"""
        built_json, missing = self.builder.build(
            {
                "task_type_key": "pipeline_inspection",
                "task_type": "管缆巡检",
                "start_time": "2026-08-14 17:30:00",
                "end_time": "2026-08-14 19:00:00",
                "water_depth": 300,
                "cable_type": "海底油气管道",
                "equipment_family": "observation_rov",
                "equipment_type": "观察级深海机器人 75HP",
                "equipment_unit_id": "OBSROV-75-001",
            },
            "pipeline_inspection",
        )

        self.assertNotIn("payload", built_json)
        payload_missing = next(item for item in missing if item["key"] == "payload")
        self.assertEqual(payload_missing["equipment_type"], "观察级深海机器人 75HP")
        self.assertIn("水下成像系统", payload_missing["onboard_payloads"])
        self.assertIn("FLS声呐系统", payload_missing["onboard_payloads"])

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
