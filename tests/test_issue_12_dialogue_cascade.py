import copy
import unittest
from unittest.mock import MagicMock, patch
from src.dialogue_manager import DialogueManager
from src.knowledge_retriever import KnowledgeBase, RobotSelectionDataError
from src.llm_client import LLMClient


class TestIssue12DialogueCascade(unittest.TestCase):
    """Dialogue cascade unit & integration tests 1-13 required for Issue #12 Phase 2B."""

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

    # ── Test 1: 缺 class 时返回 class 候选且不下钻 ─────────────────────────────
    def test_01_missing_class_returns_class_candidates(self):
        """1. 缺 class 时调用 list_robot_classes，不下钻调用 family/spec/unit。"""
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
            mock_families.assert_not_called()
            mock_specs.assert_not_called()
            mock_units.assert_not_called()

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

    # ── Test 3: AUV 输入 '324CC' 转 4 级 canonical equipment_type ─────────────────
    def test_03_auv_returns_cc_specification(self):
        """3. 输入显示值 '324CC' 确定性转换为 canonical equipment_type 并填充 4 级级联。"""
        self._init_task("pipeline_inspection")
        self._apply_updates({
            "equipment_class": "auv",
            "equipment_family": "水下无人自主航行器",
            "equipment_type": "324CC",
        }, task_type_key="pipeline_inspection")

        type_slot = self.dm.slot_store.slots.get("equipment_type")
        self.assertIsNotNone(type_slot)
        self.assertEqual(type_slot.status, "valid")
        self.assertEqual(type_slot.value, "水下无人自主航行器 324CC")

    # ── Test 4: 非 AUV 输入 '250HP' 转 4 级 canonical equipment_type ──────────────
    def test_04_non_auv_returns_hp_specification(self):
        """4. 输入显示值 '250HP' 确定性转换为 canonical equipment_type 并填充 4 级级联。"""
        self._init_task("tree_valve_operation")
        self._apply_updates({
            "equipment_class": "work_class_rov",
            "equipment_family": "通用工作级深海机器人",
            "equipment_type": "250HP",
        }, task_type_key="tree_valve_operation")

        type_slot = self.dm.slot_store.slots.get("equipment_type")
        self.assertIsNotNone(type_slot)
        self.assertEqual(type_slot.status, "valid")
        self.assertEqual(type_slot.value, "通用工作级深海机器人 250HP")

    # ── Test 5: 完整对话链：输入 324CC 后推进到 unit 候选 ─────────────────────
    def test_05_valid_three_levels_returns_unit_candidates(self):
        """5. 输入 324CC 完成前三级级联后，下一级 missing 获取精准 unit 候选。"""
        self._init_task("pipeline_inspection")
        self._apply_updates({
            "equipment_class": "auv",
            "equipment_family": "水下无人自主航行器",
            "equipment_type": "水下无人自主航行器 324CC",
        }, task_type_key="pipeline_inspection")

        missing = self.dm.slot_store.get_missing_slots(
            self.dm.builder.get_schema("pipeline_inspection"),
            allowed_values_resolver=lambda field: self.dm.builder.resolve_allowed_values(
                field, "pipeline_inspection", self.dm.task_state
            ),
        )

        unit_field = next((m for m in missing if m.get("key") == "equipment_unit_id"), None)
        self.assertIsNotNone(unit_field)
        self.assertIn("AUV-324cc-001", unit_field["allowed_values"])
        self.assertNotIn("WROV-250-001", unit_field["allowed_values"])

    # ── Test 6: 直接输入 AUV unit 自动补全 ─────────────────────────────────────
    def test_06_direct_unit_input_auto_completes(self):
        """6. 直接输入 AUV-324cc-001 自动补全四级级联且均 valid，不继续追问上级。"""
        self._init_task("pipeline_inspection")
        self._apply_updates({"equipment_unit_id": "AUV-324cc-001"}, task_type_key="pipeline_inspection")

        slots = self.dm.slot_store.slots
        self.assertEqual(slots["equipment_class"].status, "valid")
        self.assertEqual(slots["equipment_family"].status, "valid")
        self.assertEqual(slots["equipment_type"].status, "valid")
        self.assertEqual(slots["equipment_unit_id"].status, "valid")

        missing = self.dm.slot_store.get_missing_slots(self.dm.builder.get_schema("pipeline_inspection"))
        missing_keys = [m["key"] for m in missing]
        self.assertNotIn("equipment_class", missing_keys)
        self.assertNotIn("equipment_family", missing_keys)
        self.assertNotIn("equipment_type", missing_keys)
        self.assertNotIn("equipment_unit_id", missing_keys)

    # ── Test 7: legacy equipment_type 继续兼容 ─────────────────────────────────
    def test_07_legacy_equipment_type_compatibility(self):
        """7. legacy equipment_type (通用工作级深海机器人 250HP) 输入保持兼容，自动补全四级级联。"""
        self._init_task("tree_valve_operation")
        self._apply_updates({"equipment_type": "通用工作级深海机器人 250HP"}, task_type_key="tree_valve_operation")

        slots = self.dm.slot_store.slots
        self.assertEqual(slots["equipment_class"].value, "work_class_rov")
        self.assertEqual(slots["equipment_family"].value, "通用工作级深海机器人")
        self.assertEqual(slots["equipment_type"].value, "通用工作级深海机器人 250HP")

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

    # ── Test 9: ASR handoff 文本与普通文本一致性契约 ─────────────────────────
    def test_09_asr_handoff_text_and_direct_text_produce_identical_result(self):
        """9. ASR handoff 转写文本与文本输入在 DialogueManager 中产生完全相同的结果。"""
        msg = "使用 AUV-324cc-001 执行管缆巡检"

        dm1 = DialogueManager(self.llm, self.kb)
        dm1.process(msg, request_id="req_text_01")

        dm2 = DialogueManager(self.llm, self.kb)
        dm2.process(msg, request_id="req_asr_01")

        self.assertEqual(dm1.slot_store.get_task_state(), dm2.slot_store.get_task_state())
        for k in ("equipment_class", "equipment_family", "equipment_type", "equipment_unit_id"):
            s1 = dm1.slot_store.slots.get(k)
            s2 = dm2.slot_store.slots.get(k)
            self.assertIsNotNone(s1)
            self.assertIsNotNone(s2)
            self.assertEqual(s1.value, s2.value)
            self.assertEqual(s1.status, s2.status)

    # ── Test 10: Registry 错误 fail closed 并提示用户 ──────────────────────────
    def test_10_registry_error_fails_closed(self):
        """10. Registry 抛出 RobotSelectionDataError 时 fail closed，不产生虚假候选且提示用户候选暂不可用。"""
        from src.prompts import build_responder_messages

        self._init_task("pipeline_inspection")

        err = RobotSelectionDataError("Database corrupted", error_code="DATABASE_CORRUPTED")
        with patch.object(self.kb, "list_robot_classes", side_effect=err):
            allowed = self.dm.builder.get_allowed_values("pipeline_inspection", "equipment_class")
            self.assertEqual(allowed, [])

            self.dm._apply_updates_in_transaction({"equipment_class": "invalid_class"}, self.dm.slot_store.slots, allow_overwrite=True)
            self.assertEqual(self.dm.slot_store.slots["equipment_class"].status, "invalid")

            missing_empty = [{"key": "equipment_type", "label": "作业设备型号", "type": "string", "allowed_values": []}]
            msg = build_responder_messages(
                task_state={"equipment_class": "auv", "equipment_family": "水下无人自主航行器"},
                built_json={"equipment_class": "auv", "equipment_family": "水下无人自主航行器"},
                missing_fields=missing_empty,
                mode="normal", phase="collecting", knowledge_context="", constraint_context={"type": "none"}, conversation_history=[], latest_user_message="继续", ROV2type={}, support_task=[]
            )[0]["content"]
            self.assertIn("候选暂不可用", msg)

    # ── Test 11 (P1-1): 切换父级 class 绕过/不使用旧 family 缓存 ─────────────────
    def test_11_parent_class_change_bypasses_stale_cache(self):
        """11 (P1-1). 切换 equipment_class 时，动态候选解析立即返回对应新类别的 family，不使用旧缓存。"""
        self._init_task("pipeline_inspection")
        self._apply_updates({"equipment_class": "auv"}, task_type_key="pipeline_inspection")

        fams_auv = self.dm.builder.resolve_allowed_values(
            {"allowed_values_ref": "robot_family_full_names"}, "pipeline_inspection", self.dm.task_state
        )
        self.assertEqual(fams_auv, ["水下无人自主航行器"])

        # 切换 class 到 observation_rov
        self._apply_updates({"equipment_class": "observation_rov"}, task_type_key="pipeline_inspection")
        fams_obs = self.dm.builder.resolve_allowed_values(
            {"allowed_values_ref": "robot_family_full_names"}, "pipeline_inspection", self.dm.task_state
        )
        self.assertIn("观察级深海机器人", fams_obs)
        self.assertNotIn("水下无人自主航行器", fams_obs)

    # ── Test 12 (P1-5): 非领域异常不被候选解析吞掉 ─────────────────────────────
    def test_12_non_domain_exception_is_not_swallowed(self):
        """12 (P1-5). TypeError / AttributeError 等程序异常在 allowed_values_ref 解析时不被吞掉。"""
        self._init_task("pipeline_inspection")

        with patch.object(self.kb, "list_robot_classes", side_effect=TypeError("Unexpected code bug")):
            with self.assertRaises(TypeError):
                self.dm.builder.resolve_allowed_values(
                    {"allowed_values_ref": "robot_category_labels"}, "pipeline_inspection", self.dm.task_state
                )

    # ── Test 13 (P1-3): string 槽位接收 dict 被拒绝而不被当成合法 string 返回 ────────
    def test_13_string_slot_rejects_dict(self):
        """13 (P1-3). string 类型的槽位（如 cable_type）传入 dict 时，不被原样作为 string 返回。"""
        self._init_task("pipeline_inspection")
        self.dm.task_state["cable_type"] = {"unexpected": "dict_object"}

        field_def = {"key": "cable_type", "type": "string", "allowed_values_ref": "cable_type_values"}
        extracted = self.dm.builder._extract_field("cable_type", "string", field_def, self.dm.task_state, "pipeline_inspection")
        self.assertNotIsInstance(extracted, dict)
        self.assertIsNone(extracted)

    # ── Test 14 (P1-1): get_required -> _resolve_candidate_catalog 不吞 TypeError ──
    def test_14_candidate_catalog_resolution_does_not_swallow_type_error(self):
        """14 (P1-1). get_required() 经过 _resolve_candidate_catalog() 时不吞掉 TypeError 等程序异常。"""
        self._init_task("pipeline_inspection")
        with patch.object(self.kb, "list_robot_classes", side_effect=TypeError("Programming bug in catalog")):
            with self.assertRaises(TypeError):
                self.dm.builder.get_required("pipeline_inspection", task_state=self.dm.task_state)

    # ── Test 15 (P1-2): Prompts 明确按 equipment_type 追问 ────────────────────────
    def test_15_prompt_instructions_for_auv_and_non_auv_specifications(self):
        """15 (P1-2). prompts 按 equipment_type 追问设备型号。"""
        from src.prompts import build_responder_messages

        missing_type = [
            {"key": "equipment_type", "label": "作业设备型号", "type": "string", "allowed_values": ["水下无人自主航行器 324CC"]},
        ]
        msg_auv = build_responder_messages(
            task_state={"equipment_class": "auv", "equipment_family": "水下无人自主航行器"},
            built_json={"equipment_class": "auv", "equipment_family": "水下无人自主航行器"},
            missing_fields=missing_type,
            mode="normal", phase="collecting", knowledge_context="", constraint_context={"type": "none"}, conversation_history=[], latest_user_message="继续", ROV2type={}, support_task=[]
        )[0]["content"]
        self.assertIn("equipment_type", msg_auv)
        self.assertIn("水下无人自主航行器 324CC", msg_auv)

    def test_equipment_type_missing_alone_does_not_trigger_spec_prompt(self):
        """equipment_type 缺失时生成 equipment_type 专属提示。"""
        from src.prompts import build_responder_messages

        missing_fields = [
            {
                "key": "equipment_type",
                "label": "作业设备型号",
                "type": "string",
                "allowed_values": ["通用工作级深海机器人 250HP"],
            }
        ]

        msg = build_responder_messages(
            task_state={
                "equipment_class": "work_class_rov",
                "equipment_family": "通用工作级深海机器人",
            },
            built_json={
                "equipment_class": "work_class_rov",
                "equipment_family": "通用工作级深海机器人",
            },
            missing_fields=missing_fields,
            mode="normal",
            phase="collecting",
            knowledge_context="",
            constraint_context={"type": "none"},
            conversation_history=[],
            latest_user_message="继续",
            ROV2type={},
            support_task=[],
        )[0]["content"]

        self.assertIn("equipment_type", msg)


if __name__ == "__main__":
    unittest.main()
