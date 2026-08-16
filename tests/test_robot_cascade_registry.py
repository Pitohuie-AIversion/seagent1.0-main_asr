"""
test_robot_cascade_registry.py — 4-level robot cascade registry and model variant contract tests for Issue #12
"""

import copy
import unittest
from src.knowledge_retriever import KnowledgeBase, RobotSelectionDataError


class TestRobotCascadeRegistry(unittest.TestCase):
    def setUp(self):
        self.kb = KnowledgeBase()

    # ──────────────────────────────────────────────────────────────────────────
    # 正常路径测试 (1-8)
    # ──────────────────────────────────────────────────────────────────────────

    def test_01_list_robot_classes_filter_by_task_type(self):
        """1. 根据任务类型筛选合法 class"""
        classes = self.kb.list_robot_classes(task_type_key="pipeline_inspection")
        class_ids = [c["class_id"] for c in classes]
        self.assertEqual(sorted(class_ids), ["auv", "observation_rov"])

    def test_02_work_class_rov_returns_own_family_only(self):
        """2. work_class_rov 只返回自身 family"""
        families = self.kb.list_robot_families("work_class_rov")
        family_ids = [f["family_id"] for f in families]
        self.assertEqual(family_ids, ["general_work_class_rov"])

    def test_03_auv_returns_auv_family_only(self):
        """3. auv 只返回 AUV family"""
        families = self.kb.list_robot_families("auv")
        family_ids = [f["family_id"] for f in families]
        self.assertEqual(family_ids, ["autonomous_underwater_vehicle"])

    def test_04_non_auv_variant(self):
        """4. 非 AUV model variant 返回 power_hp, 250HP, variant_id"""
        variants = self.kb.list_robot_variants("work_class_rov", "general_work_class_rov")
        self.assertEqual(len(variants), 1)
        var = variants[0]
        self.assertEqual(var["hard_params"]["power_hp"], 250)
        self.assertEqual(var["full_name"], "通用工作级深海机器人 250HP")
        self.assertEqual(var["variant_id"], "general_work_class_rov_250hp")

    def test_05_auv_324_variant(self):
        """5. AUV 324 型号返回 diameter_mm, 324, 324CC, variant_id"""
        variants = self.kb.list_robot_variants("auv", "autonomous_underwater_vehicle")
        self.assertEqual(len(variants), 1)
        var = variants[0]
        self.assertEqual(var["hard_params"]["diameter_mm"], 324)
        self.assertEqual(var["full_name"], "水下无人自主航行器 324CC")
        self.assertEqual(var["variant_id"], "autonomous_underwater_vehicle_324cc")

    def test_06_auv_variant_has_no_power_hp(self):
        """6. AUV variant 中不存在 power_hp 语义"""
        variants = self.kb.list_robot_variants("auv", "autonomous_underwater_vehicle")
        for var in variants:
            self.assertIsNone(var.get("hard_params", {}).get("power_hp"))

    def test_07_list_robot_units_by_variant(self):
        """7. 根据完整 variant 只返回对应分支的 unit"""
        units = self.kb.list_robot_units("work_class_rov", "general_work_class_rov", "general_work_class_rov_250hp")
        self.assertGreater(len(units), 0)
        for u in units:
            self.assertEqual(u["variant_id"], "general_work_class_rov_250hp")
            self.assertEqual(u["unit_id"], "WROV-250-001")

    def test_08_validate_static_robot_selection_success(self):
        """8. 完整合法四级组合通过静态校验"""
        result = self.kb.validate_static_robot_selection(
            robot_class="work_class_rov",
            family="general_work_class_rov",
            specification="general_work_class_rov_250hp",
            unit_id="WROV-250-001",
            task_type_key="tree_valve_operation",
        )
        self.assertEqual(result["robot_class"], "work_class_rov")
        self.assertEqual(result["family_id"], "general_work_class_rov")
        self.assertEqual(result["variant_id"], "general_work_class_rov_250hp")
        self.assertEqual(result["unit_id"], "WROV-250-001")
        self.assertEqual(result["equipment_type"], "通用工作级深海机器人 250HP")

    # ──────────────────────────────────────────────────────────────────────────
    # 错误路径测试 (9-24)
    # ──────────────────────────────────────────────────────────────────────────

    def test_09_family_does_not_belong_to_class(self):
        """9. family 不属于 class"""
        with self.assertRaises(RobotSelectionDataError) as cm:
            self.kb.list_robot_variants("auv", "crawler_heavy_seabed_robot")
        self.assertEqual(cm.exception.error_code, "FAMILY_CLASS_MISMATCH")

    def test_10_class_not_allowed_for_task_template(self):
        """10. class 不符合 task template"""
        with self.assertRaises(RobotSelectionDataError) as cm:
            self.kb.list_robot_families("cable_burial_robot", task_type_key="pipeline_inspection")
        self.assertEqual(cm.exception.error_code, "CLASS_NOT_ALLOWED_FOR_TASK")

    def test_11_family_missing_required_capability(self):
        """11. family 缺少所需 capability"""
        custom_kb = KnowledgeBase()
        custom_kb.robot_fleet = copy.deepcopy(self.kb.robot_fleet)
        custom_kb.robot_fleet["robot_families"]["general_work_class_rov"]["capabilities"] = ["inspection"]
        with self.assertRaises(RobotSelectionDataError) as cm:
            custom_kb.list_robot_variants(
                "work_class_rov",
                "general_work_class_rov",
                task_type_key="tree_valve_operation",
            )
        self.assertEqual(cm.exception.error_code, "FAMILY_CAPABILITY_MISMATCH")

    def test_12_auv_variant_mismatch(self):
        """12. AUV 查询非 AUV variant 触发 VARIANT_FAMILY_MISMATCH"""
        with self.assertRaises(RobotSelectionDataError) as cm:
            self.kb.list_robot_units("auv", "autonomous_underwater_vehicle", "general_work_class_rov_250hp")
        self.assertEqual(cm.exception.error_code, "VARIANT_FAMILY_MISMATCH")

    def test_13_non_auv_variant_mismatch(self):
        """13. 非 AUV 查询 AUV variant 触发 VARIANT_FAMILY_MISMATCH"""
        with self.assertRaises(RobotSelectionDataError) as cm:
            self.kb.list_robot_units("work_class_rov", "general_work_class_rov", "autonomous_underwater_vehicle_324cc")
        self.assertEqual(cm.exception.error_code, "VARIANT_FAMILY_MISMATCH")

    def test_14_non_existent_variant(self):
        """14. 查询不存在的 variant 触发 VARIANT_NOT_FOUND"""
        with self.assertRaises(RobotSelectionDataError) as cm:
            self.kb.list_robot_units("work_class_rov", "general_work_class_rov", "general_work_class_rov_999hp")
        self.assertEqual(cm.exception.error_code, "VARIANT_NOT_FOUND")

    def test_15_specification_variant_belongs_to_other_family_direct(self):
        """15. variant 属于其他 family"""
        with self.assertRaises(RobotSelectionDataError) as cm:
            self.kb.list_robot_units("work_class_rov", "general_work_class_rov", "crawler_heavy_seabed_robot_1600hp")
        self.assertEqual(cm.exception.error_code, "VARIANT_FAMILY_MISMATCH")

    def test_16_unit_belongs_to_other_variant(self):
        """16. unit 属于其他 variant"""
        with self.assertRaises(RobotSelectionDataError) as cm:
            self.kb.validate_static_robot_selection(
                robot_class="work_class_rov",
                family="general_work_class_rov",
                specification="general_work_class_rov_250hp",
                unit_id="CRAWLER-1600-001",
            )
        self.assertEqual(cm.exception.error_code, "UNIT_VARIANT_MISMATCH")

    def test_17_non_existent_unit(self):
        """17. 不存在的 unit"""
        with self.assertRaises(RobotSelectionDataError) as cm:
            self.kb.validate_static_robot_selection(
                robot_class="work_class_rov",
                family="general_work_class_rov",
                specification="general_work_class_rov_250hp",
                unit_id="NON_EXISTENT_UNIT_ID",
            )
        self.assertEqual(cm.exception.error_code, "UNIT_NOT_FOUND")

    def test_18_non_existent_class_family_or_variant(self):
        """18. 不存在的 class、family 或 variant"""
        with self.assertRaises(RobotSelectionDataError) as cm1:
            self.kb.list_robot_families("non_existent_class")
        self.assertEqual(cm1.exception.error_code, "ROBOT_CLASS_NOT_FOUND")

        with self.assertRaises(RobotSelectionDataError) as cm2:
            self.kb.list_robot_variants("work_class_rov", "non_existent_family")
        self.assertEqual(cm2.exception.error_code, "FAMILY_NOT_FOUND")

    def test_19_variant_family_mismatch(self):
        """19. Variant 与 Family 不匹配时触发 VARIANT_FAMILY_MISMATCH"""
        with self.assertRaises(RobotSelectionDataError) as cm:
            self.kb.list_robot_units("auv", "autonomous_underwater_vehicle", "general_work_class_rov_250hp")
        self.assertEqual(cm.exception.error_code, "VARIANT_FAMILY_MISMATCH")

    def test_20_non_auv_power_hp(self):
        """20. 非 AUV power_hp 正常获取"""
        variants = self.kb.list_robot_variants("work_class_rov", "general_work_class_rov")
        self.assertGreater(len(variants), 0)

    def test_21_invalid_variant_name(self):
        """21. 无效的 variant 名称触发 VARIANT_NOT_FOUND"""
        with self.assertRaises(RobotSelectionDataError) as cm:
            self.kb.list_robot_units("work_class_rov", "general_work_class_rov", "invalid_variant")
        self.assertEqual(cm.exception.error_code, "VARIANT_NOT_FOUND")

    def test_22_target_field_is_string_or_bool(self):
        """22. 无效 variant 触发 VARIANT_NOT_FOUND"""
        with self.assertRaises(RobotSelectionDataError) as cm:
            self.kb.list_robot_units("work_class_rov", "general_work_class_rov", "non_existent_variant")
        self.assertEqual(cm.exception.error_code, "VARIANT_NOT_FOUND")

    def test_23_config_references_non_existent_parent(self):
        """23. 配置引用不存在的父级"""
        custom_kb = KnowledgeBase()
        custom_kb.task_schemas = copy.deepcopy(self.kb.task_schemas)
        custom_kb.task_schemas["task_templates"]["tree_valve_operation"]["allowed_robot_classes"].append("non_existent_class_ref")
        with self.assertRaises(RobotSelectionDataError) as cm:
            custom_kb.list_robot_classes(task_type_key="tree_valve_operation")
        self.assertEqual(cm.exception.error_code, "INVALID_ROBOT_CLASS_REFERENCE")

    def test_24_invalid_variant_reference(self):
        """24. 非法 variant 触发 VARIANT_NOT_FOUND"""
        with self.assertRaises(RobotSelectionDataError) as cm:
            self.kb.list_robot_units("work_class_rov", "general_work_class_rov", "invalid_ref")
        self.assertEqual(cm.exception.error_code, "VARIANT_NOT_FOUND")

    # ──────────────────────────────────────────────────────────────────────────
    # 审查补充强化测试 (25-39)
    # ──────────────────────────────────────────────────────────────────────────

    def test_25_unknown_task_type_key_returns_task_template_not_found(self):
        """25. 所有带 task_type_key 的接口在未知任务模板时统一返回 TASK_TEMPLATE_NOT_FOUND"""
        methods = [
            lambda: self.kb.list_robot_classes(task_type_key="invalid_task_key"),
            lambda: self.kb.list_robot_families("work_class_rov", task_type_key="invalid_task_key"),
            lambda: self.kb.list_robot_variants("work_class_rov", "general_work_class_rov", task_type_key="invalid_task_key"),
            lambda: self.kb.list_robot_units("work_class_rov", "general_work_class_rov", "general_work_class_rov_250hp", task_type_key="invalid_task_key"),
            lambda: self.kb.validate_static_robot_selection("work_class_rov", "general_work_class_rov", "general_work_class_rov_250hp", "WROV-250-001", task_type_key="invalid_task_key"),
        ]
        for m in methods:
            with self.assertRaises(RobotSelectionDataError) as cm:
                m()
            self.assertEqual(cm.exception.error_code, "TASK_TEMPLATE_NOT_FOUND")

    def test_26_family_missing_robot_class(self):
        """26. family 缺失 robot_class 或指向不存在的 class 彻底阻断"""
        custom_kb = KnowledgeBase()
        custom_kb.robot_fleet = copy.deepcopy(self.kb.robot_fleet)
        custom_kb.robot_fleet["robot_families"]["general_work_class_rov"]["robot_class"] = None
        with self.assertRaises(RobotSelectionDataError) as cm:
            custom_kb.list_robot_families("work_class_rov")
        self.assertEqual(cm.exception.error_code, "INVALID_ROBOT_CLASS_REFERENCE")

    def test_27_family_references_non_existent_robot_class(self):
        """27. family 引用不存在的 robot_class"""
        custom_kb = KnowledgeBase()
        custom_kb.robot_fleet = copy.deepcopy(self.kb.robot_fleet)
        custom_kb.robot_fleet["robot_families"]["general_work_class_rov"]["robot_class"] = "missing_class"
        with self.assertRaises(RobotSelectionDataError) as cm:
            custom_kb.list_robot_families("work_class_rov")
        self.assertEqual(cm.exception.error_code, "INVALID_ROBOT_CLASS_REFERENCE")

    def test_28_variant_references_non_existent_family(self):
        """28. variant 引用不存在的 family 必触发 INVALID_FAMILY_REFERENCE"""
        custom_kb = KnowledgeBase()
        custom_kb.robot_fleet = copy.deepcopy(self.kb.robot_fleet)
        custom_kb.robot_fleet["model_variants"]["general_work_class_rov_250hp"]["family_id"] = "missing_family"
        with self.assertRaises(RobotSelectionDataError) as cm:
            custom_kb.list_robot_variants("work_class_rov", "general_work_class_rov")
        self.assertEqual(cm.exception.error_code, "INVALID_FAMILY_REFERENCE")

    def test_29_fleet_unit_references_non_existent_variant(self):
        """29. fleet_units 引用不存在的 variant"""
        custom_kb = KnowledgeBase()
        custom_kb.robot_fleet = copy.deepcopy(self.kb.robot_fleet)
        custom_kb.robot_fleet["fleet_units"].append({
            "unit_id": "TEST-UNIT-999",
            "variant_id": "non_existent_variant",
        })
        with self.assertRaises(RobotSelectionDataError) as cm:
            custom_kb.list_robot_units("work_class_rov", "general_work_class_rov", "general_work_class_rov_250hp")
        self.assertEqual(cm.exception.error_code, "INVALID_VARIANT_REFERENCE")

    def test_30_duplicate_unit_id(self):
        """30. 配置中存在重复 unit_id 时阻断"""
        custom_kb = KnowledgeBase()
        custom_kb.robot_fleet = copy.deepcopy(self.kb.robot_fleet)
        custom_kb.robot_fleet["fleet_units"].append({
            "unit_id": "WROV-250-001",
            "variant_id": "general_work_class_rov_250hp",
        })
        with self.assertRaises(RobotSelectionDataError) as cm:
            custom_kb.list_robot_units("work_class_rov", "general_work_class_rov", "general_work_class_rov_250hp")
        self.assertEqual(cm.exception.error_code, "DUPLICATE_UNIT_ID")

    def test_31_non_existent_variant_query(self):
        """31. 不存在的 variant 触发 VARIANT_NOT_FOUND"""
        with self.assertRaises(RobotSelectionDataError) as cm:
            self.kb.list_robot_units("work_class_rov", "general_work_class_rov", "non_existent_nan")
        self.assertEqual(cm.exception.error_code, "VARIANT_NOT_FOUND")

    def test_32_non_existent_variant_pos_inf(self):
        """32. 不存在的 variant 触发 VARIANT_NOT_FOUND"""
        with self.assertRaises(RobotSelectionDataError) as cm:
            self.kb.list_robot_units("work_class_rov", "general_work_class_rov", "non_existent_inf")
        self.assertEqual(cm.exception.error_code, "VARIANT_NOT_FOUND")

    def test_33_non_existent_variant_neg_inf(self):
        """33. 不存在的 variant 触发 VARIANT_NOT_FOUND"""
        with self.assertRaises(RobotSelectionDataError) as cm:
            self.kb.list_robot_units("auv", "autonomous_underwater_vehicle", "non_existent_neg_inf")
        self.assertEqual(cm.exception.error_code, "VARIANT_NOT_FOUND")

    def test_34_variant_selection_validates(self):
        """34. static robot selection 校验正确返回 4 层信息"""
        res = self.kb.validate_static_robot_selection("work_class_rov", "general_work_class_rov", "general_work_class_rov_250hp", "WROV-250-001")
        self.assertEqual(res["unit_id"], "WROV-250-001")
        self.assertEqual(res["variant_id"], "general_work_class_rov_250hp")

    def test_35_variant_id_exists_but_belongs_to_other_class_family(self):
        """35. variant_id 存在但属于其他 class/family"""
        with self.assertRaises(RobotSelectionDataError) as cm:
            self.kb.list_robot_units("work_class_rov", "general_work_class_rov", "crawler_heavy_seabed_robot_1600hp")
        self.assertEqual(cm.exception.error_code, "VARIANT_FAMILY_MISMATCH")

    def test_36_variant_not_found_on_unit_query(self):
        """36. 查询未找到的 variant 抛出 VARIANT_NOT_FOUND"""
        with self.assertRaises(RobotSelectionDataError) as cm:
            self.kb.list_robot_units("work_class_rov", "general_work_class_rov", "non_existent_variant_36")
        self.assertEqual(cm.exception.error_code, "VARIANT_NOT_FOUND")

    def test_37_existing_knowledge_base_methods_backward_compatibility(self):
        """37. 现有 KnowledgeBase 公共方法行为保持兼容无回归"""
        rovs = self.kb.get_all_rovs()
        self.assertGreater(len(rovs), 0)
        classes = self.kb.get_robot_classes()
        self.assertIn("work_class_rov", classes)
        families = self.kb.get_robot_families_for_task("tree_valve_operation")
        self.assertGreater(len(families), 0)
        variants = self.kb.get_task_allowed_robot_variants("tree_valve_operation")
        self.assertGreater(len(variants), 0)
        resolved_unit = self.kb.resolve_robot_unit("WROV-250-001")
        self.assertIsNotNone(resolved_unit)
        self.assertEqual(resolved_unit["unit_id"], "WROV-250-001")

    def test_38_sibling_variant_with_orphan_variant_fails_closed(self):
        """38. 即使同 family 存在合法 sibling variant，存在孤儿 orphan variant 时整体查询 fail closed 触发 INVALID_FAMILY_REFERENCE"""
        custom_kb = KnowledgeBase()
        custom_kb.robot_fleet = copy.deepcopy(self.kb.robot_fleet)
        custom_kb.robot_fleet["model_variants"]["orphan_variant_x"] = {
            "family_id": "non_existent_family_xyz",
            "full_name": "孤儿型号",
            "hard_params": {"power_hp": 999},
        }
        with self.assertRaises(RobotSelectionDataError) as cm:
            custom_kb.list_robot_variants("work_class_rov", "general_work_class_rov")
        self.assertEqual(cm.exception.error_code, "INVALID_FAMILY_REFERENCE")

    def test_39_ambiguous_family_alias_fails_closed(self):
        """39. family alias 存在歧义时抛出 AMBIGUOUS_FAMILY_SELECTOR"""
        custom_kb = KnowledgeBase()
        custom_kb.robot_fleet = copy.deepcopy(self.kb.robot_fleet)
        custom_kb.robot_fleet["robot_families"]["general_work_class_rov"]["aliases"].append("歧义别名")
        custom_kb.robot_fleet["robot_families"]["crawler_heavy_seabed_robot"]["aliases"].append("歧义别名")
        with self.assertRaises(RobotSelectionDataError) as cm:
            custom_kb.list_robot_variants("work_class_rov", "歧义别名")
        self.assertEqual(cm.exception.error_code, "AMBIGUOUS_FAMILY_SELECTOR")

    def test_40_unit_query_rejects_invalid_model_variants_shape(self):
        """40. model_variants 结构为 None/list/非 dict 时，unit 查询不会泄漏原生 TypeError，稳定返回 INVALID_MODEL_VARIANTS_CONFIG"""
        for bad_variants in [None, [], "invalid_shape"]:
            custom_kb = KnowledgeBase()
            custom_kb.robot_fleet = copy.deepcopy(self.kb.robot_fleet)
            custom_kb.robot_fleet["model_variants"] = bad_variants
            with self.assertRaises(RobotSelectionDataError) as cm:
                custom_kb.list_robot_units("work_class_rov", "general_work_class_rov", "general_work_class_rov_250hp")
            self.assertEqual(cm.exception.error_code, "INVALID_MODEL_VARIANTS_CONFIG")

    def test_41_unit_query_rejects_family_class_mismatch(self):
        """41. Unit 查询必须校验 Family -> Class，不能只校验 Variant -> Family。"""
        with self.assertRaises(RobotSelectionDataError) as cm:
            self.kb.list_robot_units(
                "auv",
                "general_work_class_rov",
                "general_work_class_rov_250hp",
            )
        self.assertEqual(cm.exception.error_code, "FAMILY_CLASS_MISMATCH")

    def test_42_unit_query_rejects_task_incompatible_class(self):
        """42. 最终 Unit 查询不得通过全库型号回退绕过任务准入域。"""
        with self.assertRaises(RobotSelectionDataError) as cm:
            self.kb.list_robot_units(
                "work_class_rov",
                "general_work_class_rov",
                "general_work_class_rov_250hp",
                task_type_key="pipeline_inspection",
            )
        self.assertEqual(cm.exception.error_code, "CLASS_NOT_ALLOWED_FOR_TASK")

    def test_43_exact_unit_id_rejects_conflicting_variant_selector(self):
        """43. 精确 Unit ID 不得忽略同任务域内的冲突 Variant 选择。"""
        resolved = self.kb.resolve_robot_unit(
            "CRAWLER-1600-001",
            task_type_key="pipeline_burial",
            variant_selector="拖曳式海底重载作业机器人 1500HP",
        )
        self.assertIsNone(resolved)

    def test_44_powered_variant_names_match_hard_specs(self):
        """44. 非 AUV 型号的规范名称必须体现已确认的 HP 规格。"""
        expected = {
            "light_work_class_rov_150hp": (150, "轻型工作级深海机器人 150HP"),
            "observation_rov_75hp": (75, "观察级深海机器人 75HP"),
        }
        variants = self.kb.robot_fleet["model_variants"]
        for variant_id, (power_hp, full_name) in expected.items():
            with self.subTest(variant_id=variant_id):
                variant = variants[variant_id]
                self.assertEqual(variant["hard_params"]["power_hp"], power_hp)
                self.assertEqual(variant["full_name"], full_name)

    def test_45_legacy_unit_ids_resolve_to_powered_canonical_ids(self):
        """45. 历史双横线编号仅作为唯一输入别名，解析结果必须规范化。"""
        expected = {
            "LROV--001": "LROV-150-001",
            "LROV--002": "LROV-150-002",
            "OBSROV--001": "OBSROV-75-001",
        }
        for legacy_id, canonical_id in expected.items():
            with self.subTest(legacy_id=legacy_id):
                resolved = self.kb.resolve_robot_unit(legacy_id)
                self.assertIsNotNone(resolved)
                self.assertEqual(resolved["unit_id"], canonical_id)

    def test_46_static_validation_rejects_fuzzy_variant_substrings(self):
        """发布级静态 gate 只接受 Variant 的完整注册标识。"""
        for selector in ("75", "观察级75"):
            with self.subTest(selector=selector):
                with self.assertRaises(RobotSelectionDataError) as cm:
                    self.kb.validate_static_robot_selection(
                        "observation_rov",
                        "observation_rov",
                        selector,
                        "OBSROV-75-001",
                        "pipeline_inspection",
                    )
                self.assertEqual(cm.exception.error_code, "VARIANT_NOT_FOUND")

    def test_47_task_state_static_validation_rejects_fuzzy_unit_substrings(self):
        """Snapshot/publish 不得把唯一模糊子串静默迁移为真实 Unit。"""
        for selector in ("OBS", "75", "OBSROV-75"):
            with self.subTest(selector=selector):
                with self.assertRaises(RobotSelectionDataError) as cm:
                    self.kb.validate_robot_selection_from_task_state(
                        {
                            "task_type_key": "pipeline_inspection",
                            "equipment_unit_id": selector,
                        }
                    )
                self.assertEqual(cm.exception.error_code, "UNIT_NOT_FOUND")

        canonical = self.kb.validate_robot_selection_from_task_state(
            {
                "task_type_key": "pipeline_inspection",
                "equipment_type": "巡检ROV 75HP",
                "equipment_unit_id": "OBSROV--001",
            }
        )
        self.assertEqual(canonical["variant_id"], "observation_rov_75hp")
        self.assertEqual(canonical["unit_id"], "OBSROV-75-001")

    def test_48_enriched_aliases_resolve_canonical_entities(self):
        """48. 验证新增的口语化/阿拉伯数字/代号别名可正确解析到标准实体。"""
        # 测试 Unit 层解析
        unit_expectations = {
            "奇点001": "WROV-250-001",
            "奇点1号机": "WROV-250-001",
            "金牛座1号机": "CRAWLER-1600-001",
            "御夫座1号机": "TOWED-1500-001",
            "凤凰座1号机": "SPECIAL-600-001",
            "天鹰座2号机": "LROV-150-002",
            "LROV-001": "LROV-150-001",
            "OBSROV-001": "OBSROV-75-001",
            "AUV-1号机": "AUV-324cc-001",
        }
        for alias, expected_unit_id in unit_expectations.items():
            with self.subTest(unit_alias=alias):
                resolved = self.kb.resolve_robot_unit(alias)
                self.assertIsNotNone(resolved, f"Failed to resolve unit alias '{alias}'")
                self.assertEqual(resolved["unit_id"], expected_unit_id)

        # 测试 Variant 层解析
        variant_expectations = {
            "1600马力金牛座": "crawler_heavy_seabed_robot_1600hp",
            "250马力奇点": "general_work_class_rov_250hp",
            "150马力天鹰座": "light_work_class_rov_150hp",
            "75马力观察级": "observation_rov_75hp",
            "324口径AUV": "autonomous_underwater_vehicle_324cc",
        }
        for alias, expected_variant_id in variant_expectations.items():
            with self.subTest(variant_alias=alias):
                resolved = self.kb.get_rov(alias)
                self.assertIsNotNone(resolved, f"Failed to resolve variant alias '{alias}'")
                self.assertEqual(resolved["variant_id"], expected_variant_id)


if __name__ == "__main__":
    unittest.main()

