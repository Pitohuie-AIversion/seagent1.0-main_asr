"""
test_robot_capability_preselection.py — Unit and integration tests for Issue #40:
Robot selection task capability admission pre-filtering and 4-level cascade auto-collapse.
"""

import unittest
from unittest.mock import MagicMock
from src.knowledge_retriever import KnowledgeBase, RobotSelectionDataError
from src.dialogue_manager import DialogueManager
from src.llm_client import LLMClient
from src.slot_store import Slot, invalidate_robot_cascade_dependents


class TestRobotCapabilityPreselection(unittest.TestCase):
    def setUp(self):
        self.kb = KnowledgeBase()
        self.llm = MagicMock(spec=LLMClient)
        self.llm.generate.return_value = "null"
        self.dm = DialogueManager(self.llm, self.kb)

    def _init_task(self, task_type_key: str):
        schema = self.kb.get_task_schema(task_type_key)
        req = schema.get("required_fields", []) if isinstance(schema, dict) else []
        opt = schema.get("optional_fields", []) if isinstance(schema, dict) else []
        self.dm.slot_store.init_task_slots(req + opt)
        new_slots = self.dm.slot_store.clone_slots()
        new_slots["task_type_key"].value = task_type_key
        new_slots["task_type_key"].status = "valid"
        self.dm._apply_updates_in_transaction({"task_type_key": task_type_key}, new_slots, allow_overwrite=True)
        self.dm.slot_store.commit_transaction(new_slots, [])
        self.dm.task_state = self.dm.slot_store.get_task_state()

    def _apply_updates(self, updates: dict, task_type_key: str, allow_overwrite: bool = True):
        new_slots = self.dm.slot_store.clone_slots()
        if task_type_key:
            new_slots["task_type_key"].value = task_type_key
            new_slots["task_type_key"].status = "valid"
        self.dm._apply_updates_in_transaction(updates, new_slots, allow_overwrite=allow_overwrite)
        self.dm._normalize_and_validate_in_transaction(new_slots, task_type_key)
        self.dm.slot_store.commit_transaction(new_slots, [])
        self.dm.task_state = self.dm.slot_store.get_task_state()

    # ──────────────────────────────────────────────────────────────────────────
    # 1. Feasible Domain & Capability Pre-filtering
    # ──────────────────────────────────────────────────────────────────────────

    def test_01_pipeline_burial_capability_filtering(self):
        """1. pipeline_burial 仅允许 cable_burial capable 机器人"""
        domain = self.kb.get_feasible_robot_selection_domain("pipeline_burial")
        self.assertIn("families", domain)
        self.assertEqual(
            {f["family_id"] for f in domain["families"]},
            {"crawler_heavy_seabed_robot", "towed_heavy_seabed_robot", "special_work_class_robot"},
        )
        classes = domain["classes"]
        class_ids = [c["class_id"] for c in classes]
        self.assertEqual(class_ids, ["cable_burial_robot"])

        families = classes[0]["families"]
        family_ids = [f["family_id"] for f in families]
        self.assertEqual(
            set(family_ids),
            {"crawler_heavy_seabed_robot", "towed_heavy_seabed_robot", "special_work_class_robot"},
        )
        for f in families:
            self.assertIn("cable_burial", f["capabilities"])

    def test_02_tree_valve_operation_capability_filtering(self):
        """2. tree_valve_operation 仅允许 tree_operation capable 机器人"""
        domain = self.kb.get_feasible_robot_selection_domain("tree_valve_operation")
        classes = domain["classes"]
        class_ids = [c["class_id"] for c in classes]
        self.assertEqual(class_ids, ["work_class_rov"])

        families = classes[0]["families"]
        family_ids = [f["family_id"] for f in families]
        self.assertEqual(family_ids, ["general_work_class_rov"])
        for f in families:
            self.assertIn("tree_operation", f["capabilities"])

    def test_03_pipeline_inspection_capability_filtering(self):
        """3. pipeline_inspection 仅允许 inspection capable 机器人族"""
        domain = self.kb.get_feasible_robot_selection_domain("pipeline_inspection")
        classes = domain["classes"]
        class_ids = [c["class_id"] for c in classes]
        self.assertEqual(sorted(class_ids), ["auv", "observation_rov"])

        for c in classes:
            for f in c["families"]:
                self.assertIn("inspection", f["capabilities"])

    def test_04_class_reverse_capability_filtering(self):
        """4. allowed_robot_classes 不再参与 family 任务适配，只按 capability 过滤"""
        saved_template = dict(self.kb.task_schemas["task_templates"]["pipeline_burial"])
        try:
            self.kb.task_schemas["task_templates"]["pipeline_burial"] = {
                **saved_template,
                "allowed_robot_classes": ["auv"],
                "required_capabilities": ["cable_burial"],
            }
            domain = self.kb.get_feasible_robot_selection_domain("pipeline_burial")
            family_ids = {f["family_id"] for f in domain["families"]}
            self.assertEqual(
                family_ids,
                {"crawler_heavy_seabed_robot", "towed_heavy_seabed_robot", "special_work_class_robot"},
            )
        finally:
            self.kb.task_schemas["task_templates"]["pipeline_burial"] = saved_template

    def test_task_schema_no_longer_exposes_equipment_class_slot(self):
        """WRITE 状态不再维护 equipment_class 槽位。"""
        for task_key, schema in self.kb.task_schemas["task_templates"].items():
            for mode_fields in schema.get("output_schema", {}).values():
                self.assertNotIn(
                    "equipment_class",
                    [field.get("key") for field in mode_fields],
                    f"{task_key} still exposes equipment_class",
                )

    def test_invalid_robot_class_metadata_does_not_block_write_domain(self):
        """robot_class 是 QUERY metadata，配置异常不得阻塞 WRITE capability domain。"""
        saved_family = dict(self.kb.robot_fleet["robot_families"]["observation_rov"])
        try:
            self.kb.robot_fleet["robot_families"]["observation_rov"] = {
                **saved_family,
                "robot_class": "missing_metadata_class",
            }
            domain = self.kb.get_feasible_robot_selection_domain("pipeline_inspection")
            family_ids = {f["family_id"] for f in domain["families"]}
            self.assertIn("observation_rov", family_ids)
            family = next(f for f in domain["families"] if f["family_id"] == "observation_rov")
            self.assertEqual(family["metadata_error"]["code"], "INVALID_ROBOT_CLASS_REFERENCE")
        finally:
            self.kb.robot_fleet["robot_families"]["observation_rov"] = saved_family

    def test_variant_specification_is_inferred_from_hard_params_not_class(self):
        """Variant 规格字段由 hard_params 自身决定，不再由 robot_class 决定。"""
        spec = self.kb._extract_and_validate_variant_spec(
            "metadata_only_wrong_class",
            "autonomous_underwater_vehicle",
            "autonomous_underwater_vehicle_324cc",
            {"family_id": "autonomous_underwater_vehicle", "hard_params": {"diameter_mm": 324}},
        )
        self.assertEqual(spec["type"], "diameter_mm")
        self.assertEqual(spec["display_value"], "324CC")

        spec = self.kb._extract_and_validate_variant_spec(
            "auv",
            "general_work_class_rov",
            "general_work_class_rov_250hp",
            {"family_id": "general_work_class_rov", "hard_params": {"power_hp": 250}},
        )
        self.assertEqual(spec["type"], "power_hp")
        self.assertEqual(spec["display_value"], "250HP")

        with self.assertRaises(RobotSelectionDataError) as cm:
            self.kb._extract_and_validate_variant_spec(
                "auv",
                "observation_rov",
                "bad_variant",
                {"family_id": "observation_rov", "hard_params": {"power_hp": 250, "diameter_mm": 324}},
            )
        self.assertEqual(cm.exception.error_code, "AMBIGUOUS_SPECIFICATION_FIELDS")

    def test_family_cascade_invalidates_payload(self):
        """Family / Variant / Unit 改变后 payload 必须失效，避免旧载荷跨机器人复用。"""
        slots = {
            "equipment_family": Slot("equipment_family", value="小型观察级机器人", status="valid"),
            "equipment_type": Slot("equipment_type", value="小型观察级机器人 150HP", status="valid"),
            "equipment_unit_id": Slot("equipment_unit_id", value="OBSROV--001", status="valid"),
            "equipment_name": Slot("equipment_name", value="OBSROV--001", status="valid"),
            "payload": Slot("payload", value=["高清水下摄像机"], status="valid", value_type="list"),
        }
        invalidate_robot_cascade_dependents(slots, ["equipment_family"])
        self.assertEqual(slots["payload"].status, "missing")
        self.assertEqual(slots["payload"].value, [])

    # ──────────────────────────────────────────────────────────────────────────
    # 2. Four-Level Auto Collapse
    # ──────────────────────────────────────────────────────────────────────────

    def test_05_auto_collapse_pipeline_burial(self):
        """5. pipeline_burial: family(3 candidates)->stop"""
        self._init_task("pipeline_burial")

        new_slots = self.dm.slot_store.slots
        self.assertEqual(new_slots["task_type_key"].value, "pipeline_burial")

        # Family 有 3 个 candidates -> 停止收敛
        self.assertEqual(new_slots["equipment_family"].status, "missing")
        self.assertIsNone(new_slots["equipment_family"].value)

    def test_06_auto_collapse_tree_valve_operation(self):
        """6. tree_valve_operation: family(1)->auto, variant(1)->auto, unit(1)->auto"""
        self._init_task("tree_valve_operation")

        new_slots = self.dm.slot_store.slots
        self.assertEqual(new_slots["task_type_key"].value, "tree_valve_operation")

        self.assertEqual(new_slots["equipment_family"].status, "valid")
        self.assertEqual(new_slots["equipment_family"].value, "通用工作级深海机器人")
        self.assertEqual(new_slots["equipment_family"].source, "auto")

        self.assertEqual(new_slots["equipment_type"].status, "valid")
        self.assertEqual(new_slots["equipment_type"].value, "通用工作级深海机器人 250HP")
        self.assertEqual(new_slots["equipment_type"].source, "auto")

        self.assertEqual(new_slots["equipment_unit_id"].status, "valid")
        self.assertEqual(new_slots["equipment_unit_id"].value, "WROV-250-001")
        self.assertEqual(new_slots["equipment_unit_id"].source, "auto")

    def test_07_auto_collapse_pipeline_inspection_multi_candidate(self):
        """7. pipeline_inspection: family 多候选 -> 停止自动收敛，询问 family"""
        self._init_task("pipeline_inspection")

        new_slots = self.dm.slot_store.slots
        self.assertEqual(new_slots["task_type_key"].value, "pipeline_inspection")

        self.assertIsNone(new_slots["equipment_family"].value)

    def test_08_auto_collapse_candidate_count_zero_fail_closed(self):
        """8. 候选数 = 0 时 Fail Closed，标记选型错误"""
        saved_template = dict(self.kb.task_schemas["task_templates"]["pipeline_burial"])
        try:
            self.kb.task_schemas["task_templates"]["pipeline_burial"] = {
                **saved_template,
                "allowed_robot_classes": ["non_existent_class"],
                "required_capabilities": ["cable_burial"],
            }
            self._init_task("pipeline_burial")
            new_slots = self.dm.slot_store.slots
            self.assertEqual(new_slots["equipment_family"].status, "missing")
            self.assertIsNone(new_slots["equipment_family"].value)
        finally:
            self.kb.task_schemas["task_templates"]["pipeline_burial"] = saved_template

    # ──────────────────────────────────────────────────────────────────────────
    # 3. Direct Input Validation
    # ──────────────────────────────────────────────────────────────────────────

    def test_09_robot_class_query_interface_kept(self):
        """9. robot class 查询接口保留，用于知识问答而非 WRITE 状态。"""
        classes = self.kb.list_robot_classes("pipeline_inspection")
        class_ids = {c["class_id"] for c in classes}
        self.assertEqual(class_ids, {"observation_rov", "auv"})

    def test_10_direct_input_disallowed_capability_family_rejected(self):
        """10. 用户输入 capability 不满足的 family 被拒绝"""
        self._init_task("pipeline_burial")
        # 尝试输入观察级 ROV 族（只有 inspection 能力）
        self._apply_updates({"equipment_family": "observation_rov"}, task_type_key="pipeline_burial", allow_overwrite=False)
        new_slots = self.dm.slot_store.slots
        self.assertNotEqual(new_slots["equipment_family"].value, "小型观察级机器人")

    # ──────────────────────────────────────────────────────────────────────────
    # 4. Cascade Invalidation & Task Type Switch
    # ──────────────────────────────────────────────────────────────────────────

    def test_11_task_type_change_recomputes_domain_and_invalidates_old_robot(self):
        """11. 切换任务类型（pipeline_inspection -> tree_valve_operation），旧机器人自动失效并重新收敛"""
        self._init_task("pipeline_inspection")
        self._apply_updates({"equipment_family": "auv"}, task_type_key="pipeline_inspection")

        slots = self.dm.slot_store.slots
        self.assertEqual(slots["task_type_key"].value, "pipeline_inspection")
        self.assertEqual(slots["equipment_family"].value, "水下无人自主航行器")

        # 切换任务类型到 采油树控制面板插入
        self._apply_updates({"task_type_key": "tree_valve_operation"}, task_type_key="tree_valve_operation")

        new_slots = self.dm.slot_store.slots
        self.assertEqual(new_slots["task_type_key"].value, "tree_valve_operation")

        # 旧的 AUV 系列已被自动清除，并重新收敛为通用工作级 ROV
        self.assertEqual(new_slots["equipment_family"].value, "通用工作级深海机器人")
        self.assertEqual(new_slots["equipment_type"].value, "通用工作级深海机器人 250HP")
        self.assertEqual(new_slots["equipment_unit_id"].value, "WROV-250-001")

    def test_12_equipment_family_change_invalidates_downstream(self):
        """12. 修改 equipment_family 自动失效 type/unit 并重新收敛"""
        self._init_task("pipeline_inspection")
        self._apply_updates({"equipment_family": "observation_rov"}, task_type_key="pipeline_inspection")
        slots = self.dm.slot_store.slots
        self.assertEqual(slots["equipment_family"].value, "观察级深海机器人")

        self._apply_updates({"equipment_family": "auv"}, task_type_key="pipeline_inspection")
        slots = self.dm.slot_store.slots
        self.assertEqual(slots["equipment_family"].value, "水下无人自主航行器")

    def test_task_type_change_clears_robot_even_when_capability_still_matches(self):
        """Task Type 改变后不复用旧机器人，重新走 Family -> Variant -> Unit 选择。"""
        original_templates = self.kb.task_schemas["task_templates"]
        self.kb.task_schemas["task_templates"] = {
            **original_templates,
            "inspection_clone": {
                **original_templates["pipeline_inspection"],
                "code": "PIC",
                "task_type_values": ["克隆巡检"],
            },
        }
        try:
            self._init_task("pipeline_inspection")
            self._apply_updates({"equipment_family": "auv"}, task_type_key="pipeline_inspection")
            self._apply_updates({"payload": ["高清水下摄像机"]}, task_type_key="pipeline_inspection")

            slots = self.dm.slot_store.slots
            self.assertEqual(slots["equipment_family"].value, "水下无人自主航行器")
            self.assertEqual(slots["payload"].status, "valid")

            self._apply_updates({"task_type_key": "inspection_clone"}, task_type_key="inspection_clone")

            slots = self.dm.slot_store.slots
            self.assertEqual(slots["task_type_key"].value, "inspection_clone")
            self.assertEqual(slots["equipment_family"].status, "missing")
            self.assertIsNone(slots["equipment_family"].value)
            self.assertEqual(slots["equipment_type"].status, "missing")
            self.assertEqual(slots["equipment_unit_id"].status, "missing")
            self.assertEqual(slots["payload"].status, "missing")
        finally:
            self.kb.task_schemas["task_templates"] = original_templates

    # ──────────────────────────────────────────────────────────────────────────
    # 5. UI Candidates & Regressions
    # ──────────────────────────────────────────────────────────────────────────

    def test_13_output_builder_uses_filtered_candidates(self):
        """13. OutputBuilder 候选仅包含过滤后的 allowed_values"""
        schema = self.dm.builder.get_required("pipeline_burial", "normal", self.dm.task_state)
        eq_family_field = next(f for f in schema if f["key"] == "equipment_family")
        self.assertEqual(
            set(eq_family_field["allowed_values"]),
            {"履带式海底重载作业机器人", "拖曳式海底重载作业机器人", "特种工作级深海机器人"},
        )

    def test_14_ordinary_llm_chat_no_auto_binding(self):
        """14. 普通 LLM 自由对话不触发机器人 Auto Binding"""
        self.dm._switch_dialogue_mode("normal_chat")
        new_slots = self.dm.slot_store.slots
        self.assertIsNone(new_slots["equipment_family"].value)
        self.assertEqual(new_slots["equipment_family"].status, "missing")

    def test_15_knowledge_qa_no_robot_slot_writing(self):
        """15. Knowledge QA 知识问答不写入机器人 Slot"""
        self.dm._switch_dialogue_mode("knowledge_qa")
        new_slots = self.dm.slot_store.slots
        self.assertIsNone(new_slots["equipment_family"].value)
        self.assertEqual(new_slots["equipment_family"].status, "missing")

    def test_16_double_dash_unit_ids_preserved(self):
        """16. 校验双横线编号 (LROV--001 / LROV--002 / OBSROV--001) 完好未被篡改"""
        units = self.kb.robot_fleet.get("fleet_units", [])
        unit_ids = [u["unit_id"] for u in units]
        self.assertIn("LROV--001", unit_ids)
        self.assertIn("LROV--002", unit_ids)
        self.assertIn("OBSROV--001", unit_ids)

    def test_18_candidate_family_does_not_auto_bind_type(self):
        """18. equipment_family 为 candidate 状态时不得向下自动绑定 type"""
        self._init_task("pipeline_inspection")
        slots = self.dm.slot_store.slots
        slots["equipment_family"].value = "水下无人自主航行器"
        slots["equipment_family"].status = "candidate"
        self.dm._auto_collapse_robot_cascade(slots)
        self.assertNotEqual(slots["equipment_type"].status, "valid")

    def test_19_candidate_type_does_not_auto_bind_unit(self):
        """19. equipment_type 为 candidate 状态时不得向下自动绑定 unit"""
        self._init_task("pipeline_inspection")
        slots = self.dm.slot_store.slots
        slots["equipment_family"].value = "水下无人自主航行器"
        slots["equipment_family"].status = "valid"
        slots["equipment_type"].value = "水下无人自主航行器 324CC"
        slots["equipment_type"].status = "candidate"
        self.dm._auto_collapse_robot_cascade(slots)
        self.assertNotEqual(slots["equipment_unit_id"].status, "valid")

    def test_20_zero_capability_domain_fail_closed(self):
        """20. 无任何 family 满足 required_capabilities 时，Domain 为空并 fail closed 标记 invalid"""
        fake_task_schemas = {
            "task_templates": {
                "zero_cap_task": {
                    "task_type_key": "zero_cap_task",
                    "allowed_robot_classes": ["auv"],
                    "required_capabilities": ["nonexistent_capability"],
                    "required_fields": [{"key": "equipment_family", "type": "string"}],
                }
            }
        }
        original_schemas = self.kb.task_schemas
        self.kb.task_schemas = fake_task_schemas
        try:
            self._init_task("zero_cap_task")
            slots = self.dm.slot_store.slots
            self.assertEqual(slots["equipment_family"].status, "invalid")
            self.assertIsNone(slots["equipment_family"].value)
            self.assertIsNotNone(slots["equipment_family"].validation_error)
        finally:
            self.kb.task_schemas = original_schemas

    def test_21_direct_invalid_variant(self):
        """21. tree_valve_operation 任务下直接输入不符合 capability 的 AUV 变体被拒绝"""
        self._init_task("tree_valve_operation")
        self._apply_updates({"equipment_type": "水下无人自主航行器 324CC"}, task_type_key="tree_valve_operation")
        slots = self.dm.slot_store.slots
        self.assertNotEqual(slots["equipment_type"].status, "valid")

    def test_22_direct_invalid_unit(self):
        """22. tree_valve_operation 任务下直接指定 AUV-324cc-001 必须在静态 selection 层拒绝"""
        self._init_task("tree_valve_operation")
        self._apply_updates({"equipment_unit_id": "AUV-324cc-001"}, task_type_key="tree_valve_operation")
        slots = self.dm.slot_store.slots
        self.assertNotEqual(slots["equipment_unit_id"].status, "valid")
        self.assertEqual(slots["equipment_unit_id"].candidate_value, "AUV-324cc-001")
        self.assertNotEqual(slots["equipment_unit_id"].value, "AUV-324cc-001")

    def test_23_auto_bound_unit_snapshot_round_trip(self):
        """23. auto-bound unit 带有 source='auto'，经 snapshot 导出与 restore 后 source 保持 'auto'"""
        self._init_task("tree_valve_operation")
        slots = self.dm.slot_store.slots
        self.assertEqual(slots["equipment_unit_id"].status, "valid")
        self.assertEqual(slots["equipment_unit_id"].source, "auto")

        snap = self.dm.slot_store.export_snapshot()
        from src.slot_store import SlotStore
        new_store = SlotStore(kb=self.kb)
        new_store.restore_snapshot(snap)
        restored_unit = new_store.slots.get("equipment_unit_id")
        self.assertIsNotNone(restored_unit)
        self.assertEqual(restored_unit.source, "auto")


if __name__ == "__main__":
    unittest.main()
