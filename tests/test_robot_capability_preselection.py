"""
test_robot_capability_preselection.py — Unit and integration tests for Issue #40:
Robot selection task capability admission pre-filtering and 4-level cascade auto-collapse.
"""

import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import MagicMock
from src.knowledge_retriever import KnowledgeBase, RobotSelectionDataError
from src.dialogue_manager import DialogueManager
from src.llm_client import LLMClient
from src.simulated_time import get_current_datetime


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
        """4. 模拟 class 被反向过滤：若 allowed class 下无任何 family 满足 capability，该 class 不进入候选"""
        saved_template = dict(self.kb.task_schemas["task_templates"]["pipeline_burial"])
        try:
            self.kb.task_schemas["task_templates"]["pipeline_burial"] = {
                **saved_template,
                "allowed_robot_classes": ["auv", "cable_burial_robot"],
                "required_capabilities": ["cable_burial"],
            }
            domain = self.kb.get_feasible_robot_selection_domain("pipeline_burial")
            class_ids = [c["class_id"] for c in domain["classes"]]
            self.assertNotIn("auv", class_ids)
            self.assertEqual(class_ids, ["cable_burial_robot"])
        finally:
            self.kb.task_schemas["task_templates"]["pipeline_burial"] = saved_template

    # ──────────────────────────────────────────────────────────────────────────
    # 2. Four-Level Auto Collapse
    # ──────────────────────────────────────────────────────────────────────────

    def test_05_auto_collapse_pipeline_burial(self):
        """5. pipeline_burial: class(1 candidate)->auto, family(3 candidates)->stop"""
        self._init_task("pipeline_burial")

        new_slots = self.dm.slot_store.slots
        self.assertEqual(new_slots["task_type_key"].value, "pipeline_burial")

        # Class 只有 1 个 candidate (cable_burial_robot) -> 自动绑定
        self.assertEqual(new_slots["equipment_class"].status, "valid")
        self.assertEqual(new_slots["equipment_class"].value, "管缆埋设机器人")
        self.assertEqual(new_slots["equipment_class"].source, "auto")

        # Family 有 3 个 candidates -> 停止收敛
        self.assertEqual(new_slots["equipment_family"].status, "missing")
        self.assertIsNone(new_slots["equipment_family"].value)

    def test_06_auto_collapse_tree_valve_operation(self):
        """6. tree_valve_operation: class(1)->auto, family(1)->auto, variant(1)->auto, unit(1)->auto"""
        self._init_task("tree_valve_operation")

        new_slots = self.dm.slot_store.slots
        self.assertEqual(new_slots["task_type_key"].value, "tree_valve_operation")

        self.assertEqual(new_slots["equipment_class"].status, "valid")
        self.assertEqual(new_slots["equipment_class"].value, "工作级ROV")
        self.assertEqual(new_slots["equipment_class"].source, "auto")

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
        """7. pipeline_inspection: class(2 candidates: observation_rov, auv) -> 停止自动收敛，询问 class"""
        self._init_task("pipeline_inspection")

        new_slots = self.dm.slot_store.slots
        self.assertEqual(new_slots["task_type_key"].value, "pipeline_inspection")

        self.assertEqual(new_slots["equipment_class"].status, "missing")
        self.assertIsNone(new_slots["equipment_class"].value)
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
            self.assertEqual(new_slots["equipment_class"].status, "invalid")
            self.assertIsNone(new_slots["equipment_class"].value)
        finally:
            self.kb.task_schemas["task_templates"]["pipeline_burial"] = saved_template

    # ──────────────────────────────────────────────────────────────────────────
    # 3. Direct Input Validation
    # ──────────────────────────────────────────────────────────────────────────

    def test_09_direct_input_disallowed_class_rejected(self):
        """9. 用户直接输入任务不允许的 class 被拒绝"""
        self._init_task("tree_valve_operation")
        # 此时已自动收敛到 work_class_rov
        # 尝试改为 AUV
        self._apply_updates({"equipment_class": "auv"}, task_type_key="tree_valve_operation", allow_overwrite=False)
        new_slots = self.dm.slot_store.slots
        self.assertNotIn(new_slots["equipment_class"].value, ("auv", "AUV"))
        self.assertEqual(new_slots["equipment_class"].value, "工作级ROV")

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
        self._apply_updates({"equipment_class": "auv"}, task_type_key="pipeline_inspection")

        slots = self.dm.slot_store.slots
        self.assertEqual(slots["task_type_key"].value, "pipeline_inspection")
        self.assertIn(slots["equipment_class"].value, ("auv", "AUV"))

        # 切换任务类型到 采油树控制面板插入
        self._apply_updates({"task_type_key": "tree_valve_operation"}, task_type_key="tree_valve_operation")

        new_slots = self.dm.slot_store.slots
        self.assertEqual(new_slots["task_type_key"].value, "tree_valve_operation")

        # 旧的 AUV 机器人已被自动清除，并重新收敛为通用工作级 ROV
        self.assertEqual(new_slots["equipment_class"].value, "工作级ROV")
        self.assertEqual(new_slots["equipment_family"].value, "通用工作级深海机器人")
        self.assertEqual(new_slots["equipment_type"].value, "通用工作级深海机器人 250HP")
        self.assertEqual(new_slots["equipment_unit_id"].value, "WROV-250-001")

    def test_11b_task_type_change_clears_stale_robot_conflict(self):
        """旧任务的未决 Class conflict 不得阻断新任务的唯一自动收敛。"""
        self._init_task("pipeline_inspection")
        self._apply_updates(
            {"equipment_class": "auv"},
            task_type_key="pipeline_inspection",
        )
        self._apply_updates(
            {"equipment_class": "未知飞船"},
            task_type_key="pipeline_inspection",
            allow_overwrite=False,
        )
        self.assertEqual(
            self.dm.slot_store.slots["equipment_class"].status,
            "conflict",
        )

        new_slots = self.dm.slot_store.clone_slots()
        self.dm._apply_updates_in_transaction(
            {"task_type_key": "tree_valve_operation"},
            new_slots,
            allow_overwrite=True,
        )
        self.dm._normalize_and_validate_in_transaction(
            new_slots,
            "tree_valve_operation",
        )
        self.dm.slot_store.commit_transaction(new_slots, [])

        slots = self.dm.slot_store.slots
        self.assertEqual(slots["equipment_class"].value, "工作级ROV")
        self.assertEqual(slots["equipment_family"].value, "通用工作级深海机器人")
        self.assertEqual(slots["equipment_type"].value, "通用工作级深海机器人 250HP")
        self.assertEqual(slots["equipment_unit_id"].value, "WROV-250-001")

    def test_11c_task_type_change_clears_stale_robot_invalid(self):
        """旧任务的无效 Class 候选在任务切换时失效，不污染新任务。"""
        self._init_task("pipeline_inspection")
        self._apply_updates(
            {"equipment_class": "未知飞船"},
            task_type_key="pipeline_inspection",
            allow_overwrite=False,
        )
        self.assertEqual(
            self.dm.slot_store.slots["equipment_class"].status,
            "invalid",
        )

        new_slots = self.dm.slot_store.clone_slots()
        self.dm._apply_updates_in_transaction(
            {"task_type_key": "tree_valve_operation"},
            new_slots,
            allow_overwrite=True,
        )
        self.dm._normalize_and_validate_in_transaction(
            new_slots,
            "tree_valve_operation",
        )
        self.dm.slot_store.commit_transaction(new_slots, [])

        slots = self.dm.slot_store.slots
        self.assertEqual(slots["equipment_class"].value, "工作级ROV")
        self.assertEqual(slots["equipment_family"].value, "通用工作级深海机器人")
        self.assertEqual(slots["equipment_type"].value, "通用工作级深海机器人 250HP")
        self.assertEqual(slots["equipment_unit_id"].value, "WROV-250-001")

    def test_11d_first_task_selection_clears_pretask_invalid_robot(self):
        """首次确定任务也应清理任务语境外的未生效机器人候选。"""
        self.dm.slot_store.slots["equipment_class"].status = "invalid"
        self.dm.slot_store.slots["equipment_class"].candidate_value = "未知飞船"
        self.dm.slot_store.slots["equipment_class"].validation_error = "未知机器人"

        new_slots = self.dm.slot_store.clone_slots()
        self.dm._apply_updates_in_transaction(
            {"task_type_key": "tree_valve_operation"},
            new_slots,
            allow_overwrite=True,
        )

        self.assertEqual(new_slots["equipment_class"].value, "工作级ROV")
        self.assertEqual(new_slots["equipment_family"].value, "通用工作级深海机器人")
        self.assertEqual(new_slots["equipment_type"].value, "通用工作级深海机器人 250HP")
        self.assertEqual(new_slots["equipment_unit_id"].value, "WROV-250-001")

    def test_11e_candidate_variant_does_not_restrict_explicit_unit_update(self):
        """未确认的 candidate Variant 不是 lineage，明确 Unit 应按 Registry 反推。"""
        self._init_task("pipeline_inspection")
        type_slot = self.dm.slot_store.slots["equipment_type"]
        type_slot.value = "轻型工作级深海机器人 150HP"
        type_slot.status = "candidate"
        type_slot.candidate_value = "轻型工作级深海机器人 150HP"
        type_slot.source = "user_input"

        new_slots = self.dm.slot_store.clone_slots()
        self.dm._apply_updates_in_transaction(
            {"equipment_unit_id": "OBSROV-75-001"},
            new_slots,
            allow_overwrite=True,
        )

        self.assertEqual(new_slots["equipment_class"].value, "observation_rov")
        self.assertEqual(new_slots["equipment_family"].value, "观察级深海机器人")
        self.assertEqual(new_slots["equipment_type"].value, "观察级深海机器人 75HP")
        self.assertEqual(new_slots["equipment_unit_id"].value, "OBSROV-75-001")

    def test_12_equipment_class_change_invalidates_downstream(self):
        """12. 修改 equipment_class 自动失效 family/type/unit 并重新收敛"""
        self._init_task("pipeline_inspection")
        self._apply_updates({"equipment_class": "observation_rov"}, task_type_key="pipeline_inspection")
        slots = self.dm.slot_store.slots
        self.assertIn(slots["equipment_class"].value, ("observation_rov", "观察级ROV"))

        self._apply_updates({"equipment_class": "auv"}, task_type_key="pipeline_inspection")
        slots = self.dm.slot_store.slots
        self.assertIn(slots["equipment_class"].value, ("auv", "AUV"))
        self.assertEqual(slots["equipment_family"].value, "水下无人自主航行器")

    # ──────────────────────────────────────────────────────────────────────────
    # 5. UI Candidates & Regressions
    # ──────────────────────────────────────────────────────────────────────────

    def test_13_output_builder_uses_filtered_candidates(self):
        """13. OutputBuilder 候选仅包含过滤后的 allowed_values"""
        schema = self.dm.builder.get_required("pipeline_burial", "normal", self.dm.task_state)
        eq_class_field = next(f for f in schema if f["key"] == "equipment_class")
        self.assertEqual(eq_class_field["allowed_values"], ["管缆埋设机器人"])

    def test_14_ordinary_llm_chat_no_auto_binding(self):
        """14. 普通 LLM 自由对话不触发机器人 Auto Binding"""
        self.dm._switch_dialogue_mode("normal_chat")
        new_slots = self.dm.slot_store.slots
        self.assertIsNone(new_slots["equipment_class"].value)
        self.assertEqual(new_slots["equipment_class"].status, "missing")

    def test_15_knowledge_qa_no_robot_slot_writing(self):
        """15. Knowledge QA 知识问答不写入机器人 Slot"""
        self.dm._switch_dialogue_mode("knowledge_qa")
        new_slots = self.dm.slot_store.slots
        self.assertIsNone(new_slots["equipment_class"].value)
        self.assertEqual(new_slots["equipment_class"].status, "missing")

    def test_16_powered_unit_ids_preserved(self):
        """16. 校验带规格的正式单机编号完整保留。"""
        units = self.kb.robot_fleet.get("fleet_units", [])
        unit_ids = [u["unit_id"] for u in units]
        self.assertIn("LROV-150-001", unit_ids)
        self.assertIn("LROV-150-002", unit_ids)
        self.assertIn("OBSROV-75-001", unit_ids)

    def test_17_candidate_class_does_not_auto_bind_family(self):
        """17. equipment_class 为 candidate 状态时不得向下自动绑定 family"""
        self._init_task("pipeline_inspection")
        slots = self.dm.slot_store.slots
        slots["equipment_class"].value = "auv"
        slots["equipment_class"].status = "candidate"
        self.dm._auto_collapse_robot_cascade(slots)
        self.assertNotEqual(slots["equipment_family"].status, "valid")

    def test_18_candidate_family_does_not_auto_bind_type(self):
        """18. equipment_family 为 candidate 状态时不得向下自动绑定 type"""
        self._init_task("pipeline_inspection")
        slots = self.dm.slot_store.slots
        slots["equipment_class"].value = "auv"
        slots["equipment_class"].status = "valid"
        slots["equipment_family"].value = "水下无人自主航行器"
        slots["equipment_family"].status = "candidate"
        self.dm._auto_collapse_robot_cascade(slots)
        self.assertNotEqual(slots["equipment_type"].status, "valid")

    def test_19_candidate_type_does_not_auto_bind_unit(self):
        """19. equipment_type 为 candidate 状态时不得向下自动绑定 unit"""
        self._init_task("pipeline_inspection")
        slots = self.dm.slot_store.slots
        slots["equipment_class"].value = "auv"
        slots["equipment_class"].status = "valid"
        slots["equipment_family"].value = "水下无人自主航行器"
        slots["equipment_family"].status = "valid"
        slots["equipment_type"].value = "水下无人自主航行器 324CC"
        slots["equipment_type"].status = "candidate"
        self.dm._auto_collapse_robot_cascade(slots)
        self.assertNotEqual(slots["equipment_unit_id"].status, "valid")

    def test_20_zero_capability_domain_fail_closed(self):
        """20. 合法 class/fleet 但无任何 family 满足 required_capabilities 时，Domain 为空并 fail closed 标记 invalid"""
        # AUV 不具备 tree_operation 能力
        fake_task_schemas = {
            "task_templates": {
                "zero_cap_task": {
                    "task_type_key": "zero_cap_task",
                    "allowed_robot_classes": ["auv"],
                    "required_capabilities": ["tree_operation"],
                    "required_fields": [{"key": "equipment_class", "type": "string"}],
                }
            }
        }
        original_schemas = self.kb.task_schemas
        self.kb.task_schemas = fake_task_schemas
        try:
            self._init_task("zero_cap_task")
            slots = self.dm.slot_store.slots
            self.assertEqual(slots["equipment_class"].status, "invalid")
            self.assertIsNone(slots["equipment_class"].value)
            self.assertIsNotNone(slots["equipment_class"].validation_error)
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


class TestRobotConstraintAndAvailabilityPreselection(unittest.TestCase):
    def setUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        self.kb = KnowledgeBase()
        self.kb.state_info.state_file = Path(self._temp_dir.name) / "state.yaml"
        self.llm = MagicMock(spec=LLMClient)
        self.llm.generate.return_value = "null"
        self.dm = DialogueManager(self.llm, self.kb)
        for unit in self.kb.robot_fleet.get("fleet_units", []):
            self._seed_unit(unit["unit_id"], available=True)

    def tearDown(self):
        self._temp_dir.cleanup()

    def _seed_unit(self, unit_id: str, *, available: bool) -> None:
        self.kb.state_info.set_status(
            unit_id,
            {
                "overall_status": "available" if available else "busy",
                "is_online": True,
                "is_busy": not available,
                "current_velocity": 0.1,
                "turbidity": 1.0,
                "confidence": 0.95,
            },
        )

    def _init_task(self, task_type_key: str) -> None:
        schema = self.kb.get_task_schema(task_type_key)
        req = schema.get("required_fields", []) if isinstance(schema, dict) else []
        opt = schema.get("optional_fields", []) if isinstance(schema, dict) else []
        self.dm.slot_store.init_task_slots(req + opt)
        slots = self.dm.slot_store.clone_slots()
        self.dm._apply_updates_in_transaction(
            {"task_type_key": task_type_key},
            slots,
            allow_overwrite=True,
        )
        self.dm._normalize_and_validate_in_transaction(slots, task_type_key)
        self.dm.slot_store.commit_transaction(slots, [])
        self.dm.task_state = self.dm.slot_store.get_task_state()

    def _apply_updates(self, updates: dict, task_type_key: str) -> None:
        slots = self.dm.slot_store.clone_slots()
        self.dm._apply_updates_in_transaction(
            updates,
            slots,
            allow_overwrite=True,
        )
        self.dm._normalize_and_validate_in_transaction(slots, task_type_key)
        self.dm.slot_store.commit_transaction(slots, [])
        self.dm.task_state = self.dm.slot_store.get_task_state()

    def test_24_water_depth_filters_variants_and_auto_locks_auv(self):
        self._init_task("pipeline_inspection")

        self._apply_updates({"water_depth": 800}, "pipeline_inspection")

        slots = self.dm.slot_store.slots
        self.assertEqual(slots["equipment_class"].value, "AUV")
        self.assertEqual(slots["equipment_family"].value, "水下无人自主航行器")
        self.assertEqual(slots["equipment_type"].value, "水下无人自主航行器 324CC")
        self.assertEqual(slots["equipment_unit_id"].value, "AUV-324cc-001")
        for key in (
            "equipment_class",
            "equipment_family",
            "equipment_type",
            "equipment_unit_id",
        ):
            self.assertEqual(slots[key].status, "valid")
            self.assertEqual(slots[key].source, "auto")

    def test_25_selected_payload_filters_unsupported_variant(self):
        domain = self.kb.get_feasible_robot_selection_domain(
            "pipeline_inspection",
            {"payload": ["TSS管缆跟踪传感器"]},
        )

        variant_ids = {
            variant["variant_id"]
            for class_node in domain["classes"]
            for family in class_node["families"]
            for variant in family["variants"]
        }
        self.assertEqual(
            variant_ids,
            {"light_work_class_rov_150hp", "observation_rov_75hp"},
        )

    def test_25b_depth_boundaries_are_inclusive_and_fail_closed(self):
        at_limit = self.kb.get_feasible_robot_selection_domain(
            "pipeline_inspection",
            {"water_depth": 600},
        )
        above_limit = self.kb.get_feasible_robot_selection_domain(
            "pipeline_inspection",
            {"water_depth": 600.0001},
        )

        def variant_ids(domain):
            return {
                variant["variant_id"]
                for class_node in domain["classes"]
                for family in class_node["families"]
                for variant in family["variants"]
            }

        self.assertIn("observation_rov_75hp", variant_ids(at_limit))
        self.assertNotIn("observation_rov_75hp", variant_ids(above_limit))

        for invalid_depth in (True, 0, -1, float("nan"), float("inf")):
            with self.subTest(invalid_depth=invalid_depth), self.assertRaises(
                RobotSelectionDataError
            ) as raised:
                self.kb.get_feasible_robot_selection_domain(
                    "pipeline_inspection",
                    {"water_depth": invalid_depth},
                )
            self.assertEqual(raised.exception.error_code, "INVALID_WATER_DEPTH")

    def test_25c_incompatible_depth_and_payload_fail_closed_with_reasons(self):
        domain = self.kb.get_feasible_robot_selection_domain(
            "pipeline_inspection",
            {
                "water_depth": 800,
                "payload": ["TSS管缆跟踪传感器"],
            },
        )

        self.assertEqual(domain["classes"], [])
        rejected = {
            item["variant_id"]: item["reasons"]
            for item in domain["rejected_variants"]
        }
        self.assertTrue(
            any(
                "max_depth_m" in reason
                for variant_id in (
                    "light_work_class_rov_150hp",
                    "observation_rov_75hp",
                )
                for reason in rejected[variant_id]
            )
        )
        self.assertTrue(
            any(
                "unsupported payloads" in reason
                for reason in rejected["autonomous_underwater_vehicle_324cc"]
            )
        )

    def test_26_immediate_task_filters_busy_unit_and_auto_locks_available_sibling(self):
        self._seed_unit("LROV-150-001", available=False)
        self._seed_unit("LROV-150-002", available=True)
        self._init_task("pipeline_inspection")
        start_time = (
            get_current_datetime() + timedelta(minutes=5)
        ).strftime("%Y-%m-%dT%H:%M:%S")

        self._apply_updates(
            {
                "start_time": start_time,
                "equipment_class": "observation_rov",
                "equipment_family": "light_work_class_rov",
                "equipment_type": "light_work_class_rov_150hp",
            },
            "pipeline_inspection",
        )

        unit_slot = self.dm.slot_store.slots["equipment_unit_id"]
        self.assertEqual(unit_slot.value, "LROV-150-002")
        self.assertEqual(unit_slot.status, "valid")
        self.assertEqual(unit_slot.source, "auto")

    def test_27_two_available_units_remain_ambiguous(self):
        self._seed_unit("LROV-150-001", available=True)
        self._seed_unit("LROV-150-002", available=True)
        self._init_task("pipeline_inspection")
        start_time = (
            get_current_datetime() + timedelta(minutes=5)
        ).strftime("%Y-%m-%dT%H:%M:%S")

        self._apply_updates(
            {
                "start_time": start_time,
                "equipment_class": "observation_rov",
                "equipment_family": "light_work_class_rov",
                "equipment_type": "light_work_class_rov_150hp",
            },
            "pipeline_inspection",
        )

        unit_slot = self.dm.slot_store.slots["equipment_unit_id"]
        self.assertIsNone(unit_slot.value)
        self.assertEqual(unit_slot.status, "missing")

    def test_28_future_task_does_not_filter_by_current_busy_state(self):
        self._seed_unit("LROV-150-001", available=False)
        self._seed_unit("LROV-150-002", available=True)
        self._init_task("pipeline_inspection")
        start_time = (
            get_current_datetime() + timedelta(days=1)
        ).strftime("%Y-%m-%dT%H:%M:%S")

        self._apply_updates(
            {
                "start_time": start_time,
                "equipment_class": "observation_rov",
                "equipment_family": "light_work_class_rov",
                "equipment_type": "light_work_class_rov_150hp",
            },
            "pipeline_inspection",
        )

        unit_slot = self.dm.slot_store.slots["equipment_unit_id"]
        self.assertIsNone(unit_slot.value)
        self.assertEqual(unit_slot.status, "missing")

    def test_29_auto_selection_is_recomputed_when_depth_changes(self):
        self._init_task("pipeline_inspection")
        self._apply_updates({"water_depth": 800}, "pipeline_inspection")
        self.assertEqual(self.dm.slot_store.slots["equipment_class"].value, "AUV")

        self._apply_updates({"water_depth": 500}, "pipeline_inspection")

        slots = self.dm.slot_store.slots
        self.assertIsNone(slots["equipment_class"].value)
        self.assertEqual(slots["equipment_class"].status, "missing")
        self.assertIsNone(slots["equipment_family"].value)
        self.assertIsNone(slots["equipment_type"].value)
        self.assertIsNone(slots["equipment_unit_id"].value)

    def test_30_zero_candidate_error_recovers_after_constraint_is_relaxed(self):
        """系统产生的 0 候选错误不能永久阻塞后续有效条件。"""
        self._init_task("pipeline_inspection")
        self._apply_updates({"water_depth": 1200}, "pipeline_inspection")

        class_slot = self.dm.slot_store.slots["equipment_class"]
        self.assertEqual(class_slot.status, "invalid")
        self.assertIn("No feasible robot class", class_slot.validation_error)

        self._apply_updates({"water_depth": 800}, "pipeline_inspection")

        slots = self.dm.slot_store.slots
        self.assertEqual(slots["equipment_class"].value, "AUV")
        self.assertEqual(slots["equipment_family"].value, "水下无人自主航行器")
        self.assertEqual(slots["equipment_type"].value, "水下无人自主航行器 324CC")
        self.assertEqual(slots["equipment_unit_id"].value, "AUV-324cc-001")

    def test_31_explicit_unit_keeps_auto_ancestors_when_depth_is_infeasible(self):
        """动态条件不得连同自动反推的父层擦除用户明确 Unit。"""
        self._init_task("tree_valve_operation")
        self._apply_updates(
            {
                "equipment_unit_id": {
                    "value": "WROV-250-001",
                    "raw_value": "WROV-250-001",
                    "source": "user_input",
                    "confidence": 1.0,
                }
            },
            "tree_valve_operation",
        )

        self.assertEqual(
            self.dm.slot_store.slots["equipment_unit_id"].source,
            "user_input",
        )
        self._apply_updates({"water_depth": 4000}, "tree_valve_operation")

        slots = self.dm.slot_store.slots
        self.assertIn(slots["equipment_class"].value, ("work_class_rov", "工作级ROV"))
        self.assertEqual(slots["equipment_family"].value, "通用工作级深海机器人")
        self.assertEqual(slots["equipment_type"].value, "通用工作级深海机器人 250HP")
        self.assertEqual(slots["equipment_unit_id"].value, "WROV-250-001")
        self.assertEqual(slots["equipment_unit_id"].source, "user_input")

    def test_32_explicit_class_with_no_feasible_children_is_hard_blocked(self):
        """显式 Class 不能被动态条件擦除，但局部 0 候选必须明确阻断。"""
        self._init_task("pipeline_inspection")
        self._apply_updates(
            {
                "water_depth": 800,
                "equipment_class": {
                    "value": "observation_rov",
                    "raw_value": "observation_rov",
                    "source": "user_input",
                    "confidence": 1.0,
                },
            },
            "pipeline_inspection",
        )

        class_slot = self.dm.slot_store.slots["equipment_class"]
        self.assertEqual(class_slot.value, "observation_rov")
        self.assertEqual(class_slot.status, "valid")
        self.assertEqual(class_slot.source, "user_input")
        self.assertIsNone(self.dm.slot_store.slots["equipment_family"].value)

        result = self.dm.validator.validate_task(self.dm.task_state)
        self.assertEqual(result.overall_status, "validation_error")
        self.assertEqual(
            result.error["code"],
            "NO_FEASIBLE_ROBOT_CANDIDATE",
        )
        self.assertEqual(result.violations[0].severity, "hard")

        context = self.dm._run_constraint_check(
            {"water_depth", "equipment_class"}
        )
        self.assertEqual(context["type"], "hard")
        self.assertEqual(self.dm.phase, "blocked_hard")

    def test_33_explicit_family_local_zero_candidate_recovers(self):
        """显式 Family 的局部 0 候选在条件放宽后应自动恢复。"""
        self._init_task("pipeline_inspection")
        self._apply_updates(
            {
                "water_depth": 800,
                "equipment_family": {
                    "value": "light_work_class_rov",
                    "raw_value": "light_work_class_rov",
                    "source": "user_input",
                    "confidence": 1.0,
                },
            },
            "pipeline_inspection",
        )

        family_slot = self.dm.slot_store.slots["equipment_family"]
        self.assertEqual(family_slot.value, "轻型工作级深海机器人")
        self.assertEqual(family_slot.status, "valid")
        self.assertEqual(family_slot.source, "user_input")
        blocked = self.dm.validator.validate_task(self.dm.task_state)
        self.assertEqual(
            blocked.error["code"],
            "NO_FEASIBLE_ROBOT_CANDIDATE",
        )

        self._apply_updates({"water_depth": 500}, "pipeline_inspection")

        slots = self.dm.slot_store.slots
        self.assertEqual(slots["equipment_family"].value, "轻型工作级深海机器人")
        self.assertEqual(
            slots["equipment_type"].value,
            "轻型工作级深海机器人 150HP",
        )
        recovered = self.dm.validator.validate_task(self.dm.task_state)
        self.assertNotEqual(
            (recovered.error or {}).get("code"),
            "NO_FEASIBLE_ROBOT_CANDIDATE",
        )


if __name__ == "__main__":
    unittest.main()
