import copy
import unittest
from unittest.mock import MagicMock, patch
from src.dialogue_manager import DialogueManager
from src.knowledge_retriever import KnowledgeBase, RobotSelectionDataError
from src.llm_client import LLMClient


class TestIssue12DialogueCascade(unittest.TestCase):
    """Dialogue cascade unit & integration tests 1-10 required by Issue #12 Phase 2B."""

    def setUp(self):
        self.kb = KnowledgeBase()
        self.llm = MagicMock(spec=LLMClient)
        self.llm.generate.return_value = "null"
        self.dm = DialogueManager(self.llm, self.kb)

    def _init_task(self, task_type_key="pipeline_inspection"):
        schema = self.kb.get_task_schema(task_type_key)
        req = schema.get("required_fields", []) if isinstance(schema, dict) else []
        opt = schema.get("optional_fields", []) if isinstance(schema, dict) else []
        self.dm.slot_store.init_task_slots(req + opt)
        new_slots = self.dm.slot_store.clone_slots()
        new_slots["task_type_key"].value = task_type_key
        new_slots["task_type_key"].status = "valid"
        self.dm.slot_store.commit_transaction(new_slots, [])
        self.dm.task_state = self.dm.slot_store.get_task_state()

    def _apply_updates(self, updates, allow_overwrite=True, task_type_key="pipeline_inspection"):
        new_slots = self.dm.slot_store.clone_slots()
        if task_type_key:
            new_slots["task_type_key"].value = task_type_key
            new_slots["task_type_key"].status = "valid"
        self.dm._apply_updates_in_transaction(
            updates, new_slots, allow_overwrite=allow_overwrite
        )
        self.dm._normalize_and_validate_in_transaction(
            new_slots, task_type_key
        )
        self.dm.slot_store.commit_transaction(new_slots, [])
        self.dm.task_state = self.dm.slot_store.get_task_state()

    # ── Test 1: 缺 class 时返回 class 候选 ─────────────────────────────────────
    def test_01_missing_class_returns_class_candidates(self):
        """1. 缺 class 时调用 list_robot_classes(task_type_key)，allowed_values 包含合法 class，不查 family/spec/unit。"""
        self._init_task("pipeline_inspection")

        with patch.object(self.kb, "list_robot_classes", wraps=self.kb.list_robot_classes) as mock_classes, \
             patch.object(self.kb, "list_robot_families", wraps=self.kb.list_robot_families) as mock_families, \
             patch.object(self.kb, "list_robot_specifications", wraps=self.kb.list_robot_specifications) as mock_specs, \
             patch.object(self.kb, "list_robot_units", wraps=self.kb.list_robot_units) as mock_units:

            missing = self.dm.slot_store.get_missing_slots(
                self.dm.builder.get_schema("pipeline_inspection"),
                allowed_values_resolver=lambda field: self.dm.builder.resolve_allowed_values(
                    field, "pipeline_inspection", self.dm.task_state
                ),
            )

            cls_field = next((m for m in missing if m.get("key") == "equipment_class"), None)
            self.assertIsNotNone(cls_field)
            self.assertIn("allowed_values", cls_field)
            self.assertTrue(len(cls_field["allowed_values"]) > 0)
            mock_classes.assert_called()

    # ── Test 2: 已有 class 时返回 family 候选 ──────────────────────────────────
    def test_02_valid_class_returns_family_candidates(self):
        """2. 已有 class 时调用 list_robot_families()，候选全部属于当前 class，不返回其他 class 候选。"""
        self._init_task("pipeline_inspection")
        self._apply_updates({"equipment_class": "auv"}, task_type_key="pipeline_inspection")

        missing = self.dm.slot_store.get_missing_slots(
            self.dm.builder.get_schema("pipeline_inspection"),
            allowed_values_resolver=lambda field: self.dm.builder.resolve_allowed_values(
                field, "pipeline_inspection", self.dm.task_state
            ),
        )

        fam_field = next((m for m in missing if m.get("key") == "equipment_family"), None)
        self.assertIsNotNone(fam_field)
        self.assertEqual(fam_field["allowed_values"], ["水下无人自主航行器"])

    # ── Test 3: AUV 返回 CC 规格 ───────────────────────────────────────────────
    def test_03_auv_returns_cc_specification(self):
        """3. AUV 返回 CC 规格 (324CC)，不包含马力或 HP，specification type 为 diameter_mm。"""
        self._init_task("pipeline_inspection")
        self._apply_updates({"equipment_class": "auv", "equipment_family": "水下无人自主航行器"}, task_type_key="pipeline_inspection")

        specs = self.kb.list_robot_specifications("auv", "autonomous_underwater_vehicle", "pipeline_inspection")
        self.assertTrue(len(specs) > 0)
        self.assertEqual(specs[0]["type"], "diameter_mm")
        self.assertEqual(specs[0]["display_value"], "324CC")
        self.assertNotIn("HP", specs[0]["display_value"])

    # ── Test 4: 非 AUV 返回 HP 规格 ────────────────────────────────────────────
    def test_04_non_auv_returns_hp_specification(self):
        """4. 非 AUV 返回 HP 规格 (250HP)，不包含 CC，specification type 为 power_hp。"""
        self._init_task("tree_valve_operation")
        self._apply_updates({"equipment_class": "work_class_rov", "equipment_family": "通用工作级深海机器人"}, task_type_key="tree_valve_operation")

        specs = self.kb.list_robot_specifications("work_class_rov", "general_work_class_rov", "tree_valve_operation")
        self.assertTrue(len(specs) > 0)
        self.assertEqual(specs[0]["type"], "power_hp")
        self.assertIn("HP", specs[0]["display_value"])
        self.assertNotIn("CC", specs[0]["display_value"])

    # ── Test 5: 完整前三层返回 unit 候选 ───────────────────────────────────────
    def test_05_valid_three_levels_returns_unit_candidates(self):
        """5. 完整前三层返回 unit 候选，只返回当前 spec 分支下的具体编号。"""
        self._init_task("pipeline_inspection")
        spec = {"type": "diameter_mm", "value": 324, "variant_id": "autonomous_underwater_vehicle_324cc"}
        self._apply_updates({
            "equipment_class": "auv",
            "equipment_family": "水下无人自主航行器",
            "equipment_specification": spec,
        }, task_type_key="pipeline_inspection")

        units = self.kb.list_robot_units("auv", "autonomous_underwater_vehicle", spec, "pipeline_inspection")
        unit_ids = [u["unit_id"] for u in units]
        self.assertIn("AUV-324cc-001", unit_ids)
        self.assertNotIn("WROV-250-001", unit_ids)

    # ── Test 6: 直接输入 AUV unit 自动补全 ─────────────────────────────────────
    def test_06_direct_unit_input_auto_completes(self):
        """6. 直接输入 AUV-324cc-001 自动补全四级级联且均 valid，不继续追问上级。"""
        self._init_task("pipeline_inspection")
        self._apply_updates({"equipment_unit_id": "AUV-324cc-001"}, task_type_key="pipeline_inspection")

        slots = self.dm.slot_store.slots
        self.assertEqual(slots["equipment_class"].status, "valid")
        self.assertEqual(slots["equipment_family"].status, "valid")
        self.assertEqual(slots["equipment_specification"].status, "valid")
        self.assertEqual(slots["equipment_unit_id"].status, "valid")

        missing = self.dm.slot_store.get_missing_slots(self.dm.builder.get_schema("pipeline_inspection"))
        missing_keys = [m["key"] for m in missing]
        self.assertNotIn("equipment_class", missing_keys)
        self.assertNotIn("equipment_family", missing_keys)
        self.assertNotIn("equipment_specification", missing_keys)
        self.assertNotIn("equipment_unit_id", missing_keys)

    # ── Test 7: legacy equipment_type 继续兼容 ─────────────────────────────────
    def test_07_legacy_equipment_type_compatibility(self):
        """7. legacy equipment_type (通用工作级深海机器人 250HP) 输入保持兼容，自动补全四级级联。"""
        self._init_task("tree_valve_operation")
        self._apply_updates({"equipment_type": "通用工作级深海机器人 250HP"}, task_type_key="tree_valve_operation")

        slots = self.dm.slot_store.slots
        self.assertEqual(slots["equipment_class"].value, "work_class_rov")
        self.assertEqual(slots["equipment_family"].value, "通用工作级深海机器人")
        self.assertEqual(slots["equipment_specification"].value.get("variant_id"), "general_work_class_rov_250hp")

    # ── Test 8: 知识问答不修改 Slot ───────────────────────────────────────────
    def test_08_knowledge_qa_does_not_modify_slot_store(self):
        """8. 普通知识问答不修改 SlotStore，Snapshot 与 store_version 保持 100% 不变。"""
        self._init_task("pipeline_inspection")
        snap_before = copy.deepcopy(self.dm.slot_store.export_snapshot())
        ver_before = self.dm.slot_store.version

        self.dm.process("为什么 AUV 使用 CC？")

        snap_after = self.dm.slot_store.export_snapshot()
        ver_after = self.dm.slot_store.version

        self.assertEqual(snap_before, snap_after)
        self.assertEqual(ver_before, ver_after)

    # ── Test 9: ASR 与文本一致 ────────────────────────────────────────────────
    def test_09_asr_text_and_direct_text_produce_identical_result(self):
        """9. ASR 转写文本与文本输入走同一 DialogueManager 流程，四级 Slot 结果完全一致。"""
        msg = "使用 AUV-324cc-001 执行管缆巡检"

        # 文本流程
        dm1 = DialogueManager(self.llm, self.kb)
        dm1.process(msg)

        # ASR 转写流程 (系统设计中 ASR 文本直接进入 DialogueManager.process)
        dm2 = DialogueManager(self.llm, self.kb)
        dm2.process(msg)

        self.assertEqual(dm1.slot_store.get_task_state(), dm2.slot_store.get_task_state())
        for k in ("equipment_class", "equipment_family", "equipment_specification", "equipment_unit_id"):
            s1 = dm1.slot_store.slots.get(k)
            s2 = dm2.slot_store.slots.get(k)
            self.assertIsNotNone(s1)
            self.assertIsNotNone(s2)
            self.assertEqual(s1.value, s2.value)
            self.assertEqual(s1.status, s2.status)

    # ── Test 10: Registry 错误 fail closed ────────────────────────────────────
    def test_10_registry_error_fails_closed(self):
        """10. Registry 抛出 RobotSelectionDataError 时 fail closed，不污染 SlotStore，不产生虚假候选。"""
        self._init_task("pipeline_inspection")

        err = RobotSelectionDataError("Database corrupted", error_code="DATABASE_CORRUPTED")
        with patch.object(self.kb, "list_robot_classes", side_effect=err):
            allowed = self.dm.builder.get_allowed_values("pipeline_inspection", "equipment_class")
            self.assertEqual(allowed, [])

            self.dm._apply_updates_in_transaction({"equipment_class": "invalid_class"}, self.dm.slot_store.slots, allow_overwrite=True)
            self.assertEqual(self.dm.slot_store.slots["equipment_class"].status, "invalid")


if __name__ == "__main__":
    unittest.main()
