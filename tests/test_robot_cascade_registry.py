"""
test_robot_cascade_registry.py — 4-level robot cascade registry and specification contract tests for Issue #12
"""

import copy
import math
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

    def test_04_non_auv_specification(self):
        """4. 非 AUV specification 返回 power_hp, hp, 250HP, variant_id"""
        specs = self.kb.list_robot_specifications("work_class_rov", "general_work_class_rov")
        self.assertEqual(len(specs), 1)
        spec = specs[0]
        self.assertEqual(spec["type"], "power_hp")
        self.assertEqual(spec["value"], 250)
        self.assertEqual(spec["unit"], "hp")
        self.assertEqual(spec["display_value"], "250HP")
        self.assertEqual(spec["variant_id"], "general_work_class_rov_250hp")

    def test_05_auv_324_specification(self):
        """5. AUV 324 规格返回 diameter_mm, 324, mm, 324CC, variant_id"""
        specs = self.kb.list_robot_specifications("auv", "autonomous_underwater_vehicle")
        self.assertEqual(len(specs), 1)
        spec = specs[0]
        self.assertEqual(spec["type"], "diameter_mm")
        self.assertEqual(spec["value"], 324)
        self.assertEqual(spec["unit"], "mm")
        self.assertEqual(spec["display_value"], "324CC")
        self.assertEqual(spec["variant_id"], "autonomous_underwater_vehicle_324cc")

    def test_06_auv_specification_has_no_power_hp(self):
        """6. AUV specification 中不存在 power_hp 语义"""
        specs = self.kb.list_robot_specifications("auv", "autonomous_underwater_vehicle")
        for spec in specs:
            self.assertNotEqual(spec.get("type"), "power_hp")
            self.assertNotIn("power_hp", spec)

    def test_07_list_robot_units_by_specification(self):
        """7. 根据完整 specification 只返回对应分支的 unit"""
        specs = self.kb.list_robot_specifications("work_class_rov", "general_work_class_rov")
        spec = specs[0]
        units = self.kb.list_robot_units("work_class_rov", "general_work_class_rov", spec)
        self.assertGreater(len(units), 0)
        for u in units:
            self.assertEqual(u["variant_id"], "general_work_class_rov_250hp")
            self.assertEqual(u["unit_id"], "WROV-250-001")

    def test_08_validate_static_robot_selection_success(self):
        """8. 完整合法四级组合通过静态校验"""
        specs = self.kb.list_robot_specifications("work_class_rov", "general_work_class_rov")
        spec = specs[0]
        result = self.kb.validate_static_robot_selection(
            robot_class="work_class_rov",
            family="general_work_class_rov",
            specification=spec,
            unit_id="WROV-250-001",
            task_type_key="tree_valve_operation",
        )
        self.assertEqual(result["robot_class"], "work_class_rov")
        self.assertEqual(result["family_id"], "general_work_class_rov")
        self.assertEqual(result["variant_id"], "general_work_class_rov_250hp")
        self.assertEqual(result["unit_id"], "WROV-250-001")
        self.assertEqual(result["specification"]["display_value"], "250HP")

    # ──────────────────────────────────────────────────────────────────────────
    # 错误路径测试 (9-24)
    # ──────────────────────────────────────────────────────────────────────────

    def test_09_family_does_not_belong_to_class(self):
        """9. family 不属于 class"""
        with self.assertRaises(RobotSelectionDataError) as cm:
            self.kb.list_robot_specifications("auv", "crawler_heavy_seabed_robot")
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
            custom_kb.list_robot_specifications(
                "work_class_rov",
                "general_work_class_rov",
                task_type_key="tree_valve_operation",
            )
        self.assertEqual(cm.exception.error_code, "FAMILY_CAPABILITY_MISMATCH")

    def test_12_auv_input_power_hp_specification(self):
        """12. AUV 输入 power_hp specification"""
        spec = {
            "type": "power_hp",
            "value": 324,
            "unit": "hp",
            "display_value": "324HP",
            "variant_id": "autonomous_underwater_vehicle_324cc",
        }
        with self.assertRaises(RobotSelectionDataError) as cm:
            self.kb.list_robot_units("auv", "autonomous_underwater_vehicle", spec)
        self.assertEqual(cm.exception.error_code, "SPECIFICATION_TYPE_MISMATCH")

    def test_13_non_auv_input_diameter_mm_specification(self):
        """13. 非 AUV 输入 diameter_mm specification"""
        spec = {
            "type": "diameter_mm",
            "value": 250,
            "unit": "mm",
            "display_value": "250CC",
            "variant_id": "general_work_class_rov_250hp",
        }
        with self.assertRaises(RobotSelectionDataError) as cm:
            self.kb.list_robot_units("work_class_rov", "general_work_class_rov", spec)
        self.assertEqual(cm.exception.error_code, "SPECIFICATION_TYPE_MISMATCH")

    def test_14_specification_value_mismatch(self):
        """14. specification 的 value 与 variant 配置不一致"""
        spec = {
            "type": "power_hp",
            "value": 999,
            "unit": "hp",
            "display_value": "999HP",
            "variant_id": "general_work_class_rov_250hp",
        }
        with self.assertRaises(RobotSelectionDataError) as cm:
            self.kb.list_robot_units("work_class_rov", "general_work_class_rov", spec)
        self.assertEqual(cm.exception.error_code, "SPECIFICATION_VALUE_MISMATCH")

    def test_15_specification_variant_belongs_to_other_family(self):
        """15. specification 的 variant 属于其他 family"""
        spec = {
            "type": "power_hp",
            "value": 1600,
            "unit": "hp",
            "display_value": "1600HP",
            "variant_id": "crawler_heavy_seabed_robot_1600hp",
        }
        with self.assertRaises(RobotSelectionDataError) as cm:
            self.kb.list_robot_units("work_class_rov", "general_work_class_rov", spec)
        self.assertEqual(cm.exception.error_code, "VARIANT_FAMILY_MISMATCH")

    def test_16_unit_belongs_to_other_variant(self):
        """16. unit 属于其他 variant"""
        specs = self.kb.list_robot_specifications("work_class_rov", "general_work_class_rov")
        spec = specs[0]
        with self.assertRaises(RobotSelectionDataError) as cm:
            self.kb.validate_static_robot_selection(
                robot_class="work_class_rov",
                family="general_work_class_rov",
                specification=spec,
                unit_id="CRAWLER-1600-001",
            )
        self.assertEqual(cm.exception.error_code, "UNIT_VARIANT_MISMATCH")

    def test_17_non_existent_unit(self):
        """17. 不存在的 unit"""
        specs = self.kb.list_robot_specifications("work_class_rov", "general_work_class_rov")
        spec = specs[0]
        with self.assertRaises(RobotSelectionDataError) as cm:
            self.kb.validate_static_robot_selection(
                robot_class="work_class_rov",
                family="general_work_class_rov",
                specification=spec,
                unit_id="NON_EXISTENT_UNIT_ID",
            )
        self.assertEqual(cm.exception.error_code, "UNIT_NOT_FOUND")

    def test_18_non_existent_class_family_or_variant(self):
        """18. 不存在的 class、family 或 variant"""
        with self.assertRaises(RobotSelectionDataError) as cm1:
            self.kb.list_robot_families("non_existent_class")
        self.assertEqual(cm1.exception.error_code, "ROBOT_CLASS_NOT_FOUND")

        with self.assertRaises(RobotSelectionDataError) as cm2:
            self.kb.list_robot_specifications("work_class_rov", "non_existent_family")
        self.assertEqual(cm2.exception.error_code, "FAMILY_NOT_FOUND")

    def test_19_auv_diameter_mm_is_none(self):
        """19. AUV diameter_mm=None"""
        custom_kb = KnowledgeBase()
        custom_kb.robot_fleet = copy.deepcopy(self.kb.robot_fleet)
        custom_kb.robot_fleet["model_variants"]["autonomous_underwater_vehicle_324cc"]["hard_params"]["diameter_mm"] = None
        with self.assertRaises(RobotSelectionDataError) as cm:
            custom_kb.list_robot_specifications("auv", "autonomous_underwater_vehicle")
        self.assertEqual(cm.exception.error_code, "MISSING_SPECIFICATION_VALUE")

    def test_20_non_auv_power_hp_is_none(self):
        """20. 非 AUV power_hp=None"""
        custom_kb = KnowledgeBase()
        custom_kb.robot_fleet = copy.deepcopy(self.kb.robot_fleet)
        custom_kb.robot_fleet["model_variants"]["general_work_class_rov_250hp"]["hard_params"]["power_hp"] = None
        with self.assertRaises(RobotSelectionDataError) as cm:
            custom_kb.list_robot_specifications("work_class_rov", "general_work_class_rov")
        self.assertEqual(cm.exception.error_code, "MISSING_SPECIFICATION_VALUE")

    def test_21_target_field_is_not_applicable(self):
        """21. 目标字段为 '不适用'"""
        custom_kb = KnowledgeBase()
        custom_kb.robot_fleet = copy.deepcopy(self.kb.robot_fleet)
        custom_kb.robot_fleet["model_variants"]["general_work_class_rov_250hp"]["hard_params"]["power_hp"] = "不适用"
        with self.assertRaises(RobotSelectionDataError) as cm:
            custom_kb.list_robot_specifications("work_class_rov", "general_work_class_rov")
        self.assertEqual(cm.exception.error_code, "SPECIFICATION_NOT_APPLICABLE")

    def test_22_target_field_is_string_or_bool(self):
        """22. 目标字段为字符串或 bool"""
        custom_kb = KnowledgeBase()
        custom_kb.robot_fleet = copy.deepcopy(self.kb.robot_fleet)
        # Test bool
        custom_kb.robot_fleet["model_variants"]["general_work_class_rov_250hp"]["hard_params"]["power_hp"] = True
        with self.assertRaises(RobotSelectionDataError) as cm1:
            custom_kb.list_robot_specifications("work_class_rov", "general_work_class_rov")
        self.assertEqual(cm1.exception.error_code, "INVALID_SPECIFICATION_TYPE")

        # Test non-numeric string
        custom_kb.robot_fleet["model_variants"]["general_work_class_rov_250hp"]["hard_params"]["power_hp"] = "abc"
        with self.assertRaises(RobotSelectionDataError) as cm2:
            custom_kb.list_robot_specifications("work_class_rov", "general_work_class_rov")
        self.assertEqual(cm2.exception.error_code, "INVALID_SPECIFICATION_TYPE")

    def test_23_config_references_non_existent_parent(self):
        """23. 配置引用不存在的父级"""
        custom_kb = KnowledgeBase()
        custom_kb.task_schemas = copy.deepcopy(self.kb.task_schemas)
        custom_kb.task_schemas["task_templates"]["tree_valve_operation"]["allowed_robot_classes"].append("non_existent_class_ref")
        with self.assertRaises(RobotSelectionDataError) as cm:
            custom_kb.list_robot_classes(task_type_key="tree_valve_operation")
        self.assertEqual(cm.exception.error_code, "INVALID_ROBOT_CLASS_REFERENCE")

    def test_24_invalid_config_fail_closed_no_silent_fallback(self):
        """24. 非法配置不会被静默过滤或默认补全"""
        custom_kb = KnowledgeBase()
        custom_kb.robot_fleet = copy.deepcopy(self.kb.robot_fleet)
        # Set negative value <= 0
        custom_kb.robot_fleet["model_variants"]["general_work_class_rov_250hp"]["hard_params"]["power_hp"] = -5
        with self.assertRaises(RobotSelectionDataError) as cm:
            custom_kb.list_robot_specifications("work_class_rov", "general_work_class_rov")
        self.assertEqual(cm.exception.error_code, "NON_POSITIVE_SPECIFICATION_VALUE")

    # ──────────────────────────────────────────────────────────────────────────
    # 审查补充强化测试 (25-39)
    # ──────────────────────────────────────────────────────────────────────────

    def test_25_unknown_task_type_key_returns_task_template_not_found(self):
        """25. 所有带 task_type_key 的接口在未知任务模板时统一返回 TASK_TEMPLATE_NOT_FOUND"""
        methods = [
            lambda: self.kb.list_robot_classes(task_type_key="invalid_task_key"),
            lambda: self.kb.list_robot_families("work_class_rov", task_type_key="invalid_task_key"),
            lambda: self.kb.list_robot_specifications("work_class_rov", "general_work_class_rov", task_type_key="invalid_task_key"),
            lambda: self.kb.list_robot_units("work_class_rov", "general_work_class_rov", {"type": "power_hp", "value": 250, "variant_id": "general_work_class_rov_250hp"}, task_type_key="invalid_task_key"),
            lambda: self.kb.validate_static_robot_selection("work_class_rov", "general_work_class_rov", {"type": "power_hp", "value": 250, "variant_id": "general_work_class_rov_250hp"}, "WROV-250-001", task_type_key="invalid_task_key"),
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
            custom_kb.list_robot_specifications("work_class_rov", "general_work_class_rov")
        self.assertEqual(cm.exception.error_code, "INVALID_FAMILY_REFERENCE")

    def test_29_fleet_unit_references_non_existent_variant(self):
        """29. fleet_units 引用不存在的 variant"""
        custom_kb = KnowledgeBase()
        custom_kb.robot_fleet = copy.deepcopy(self.kb.robot_fleet)
        custom_kb.robot_fleet["fleet_units"].append({
            "unit_id": "TEST-UNIT-999",
            "variant_id": "non_existent_variant",
        })
        specs = custom_kb.list_robot_specifications("work_class_rov", "general_work_class_rov")
        with self.assertRaises(RobotSelectionDataError) as cm:
            custom_kb.list_robot_units("work_class_rov", "general_work_class_rov", specs[0])
        self.assertEqual(cm.exception.error_code, "INVALID_VARIANT_REFERENCE")

    def test_30_duplicate_unit_id(self):
        """30. 配置中存在重复 unit_id 时阻断"""
        custom_kb = KnowledgeBase()
        custom_kb.robot_fleet = copy.deepcopy(self.kb.robot_fleet)
        custom_kb.robot_fleet["fleet_units"].append({
            "unit_id": "WROV-250-001",
            "variant_id": "general_work_class_rov_250hp",
        })
        specs = custom_kb.list_robot_specifications("work_class_rov", "general_work_class_rov")
        with self.assertRaises(RobotSelectionDataError) as cm:
            custom_kb.list_robot_units("work_class_rov", "general_work_class_rov", specs[0])
        self.assertEqual(cm.exception.error_code, "DUPLICATE_UNIT_ID")

    def test_31_power_hp_is_nan(self):
        """31. power_hp=float('nan') 明确阻断"""
        custom_kb = KnowledgeBase()
        custom_kb.robot_fleet = copy.deepcopy(self.kb.robot_fleet)
        custom_kb.robot_fleet["model_variants"]["general_work_class_rov_250hp"]["hard_params"]["power_hp"] = float("nan")
        with self.assertRaises(RobotSelectionDataError) as cm:
            custom_kb.list_robot_specifications("work_class_rov", "general_work_class_rov")
        self.assertEqual(cm.exception.error_code, "NON_FINITE_SPECIFICATION_VALUE")

    def test_32_power_hp_is_positive_inf(self):
        """32. power_hp=float('inf') 明确阻断"""
        custom_kb = KnowledgeBase()
        custom_kb.robot_fleet = copy.deepcopy(self.kb.robot_fleet)
        custom_kb.robot_fleet["model_variants"]["general_work_class_rov_250hp"]["hard_params"]["power_hp"] = float("inf")
        with self.assertRaises(RobotSelectionDataError) as cm:
            custom_kb.list_robot_specifications("work_class_rov", "general_work_class_rov")
        self.assertEqual(cm.exception.error_code, "NON_FINITE_SPECIFICATION_VALUE")

    def test_33_diameter_mm_is_negative_inf(self):
        """33. diameter_mm=float('-inf') 明确阻断"""
        custom_kb = KnowledgeBase()
        custom_kb.robot_fleet = copy.deepcopy(self.kb.robot_fleet)
        custom_kb.robot_fleet["model_variants"]["autonomous_underwater_vehicle_324cc"]["hard_params"]["diameter_mm"] = float("-inf")
        with self.assertRaises(RobotSelectionDataError) as cm:
            custom_kb.list_robot_specifications("auv", "autonomous_underwater_vehicle")
        self.assertEqual(cm.exception.error_code, "NON_FINITE_SPECIFICATION_VALUE")

    def test_34_specification_value_is_bool(self):
        """34. 调用方传入 specification value 为 bool 时校验失败"""
        specs = self.kb.list_robot_specifications("work_class_rov", "general_work_class_rov")
        forged_spec = copy.deepcopy(specs[0])
        forged_spec["value"] = True
        with self.assertRaises(RobotSelectionDataError) as cm:
            self.kb.list_robot_units("work_class_rov", "general_work_class_rov", forged_spec)
        self.assertEqual(cm.exception.error_code, "SPECIFICATION_VALUE_MISMATCH")

    def test_35_specification_value_is_nan(self):
        """35. 调用方传入 specification value 为 NaN 时校验失败"""
        specs = self.kb.list_robot_specifications("work_class_rov", "general_work_class_rov")
        forged_spec = copy.deepcopy(specs[0])
        forged_spec["value"] = float("nan")
        with self.assertRaises(RobotSelectionDataError) as cm:
            self.kb.list_robot_units("work_class_rov", "general_work_class_rov", forged_spec)
        self.assertEqual(cm.exception.error_code, "SPECIFICATION_VALUE_MISMATCH")

    def test_36_caller_forged_display_value_does_not_affect_canonical_result(self):
        """36. 调用方伪造 display_value 不影响后端重构的权威 canonical 结果"""
        specs = self.kb.list_robot_specifications("work_class_rov", "general_work_class_rov")
        forged_spec = copy.deepcopy(specs[0])
        forged_spec["display_value"] = "FORGED_999HP"
        res = self.kb.validate_static_robot_selection("work_class_rov", "general_work_class_rov", forged_spec, "WROV-250-001")
        self.assertEqual(res["specification"]["display_value"], "250HP")

    def test_37_variant_id_exists_but_belongs_to_other_class_family(self):
        """37. specification 的 variant_id 存在但属于其他 class/family"""
        spec = {
            "type": "power_hp",
            "value": 1600,
            "unit": "hp",
            "display_value": "1600HP",
            "variant_id": "crawler_heavy_seabed_robot_1600hp",
        }
        with self.assertRaises(RobotSelectionDataError) as cm:
            self.kb.list_robot_units("work_class_rov", "general_work_class_rov", spec)
        self.assertEqual(cm.exception.error_code, "VARIANT_FAMILY_MISMATCH")

    def test_38_family_with_one_valid_and_one_invalid_variant_fails_closed(self):
        """38. 同一 family 下若存在一个非法 variant，整体查询立即 fail closed 报错"""
        custom_kb = KnowledgeBase()
        custom_kb.robot_fleet = copy.deepcopy(self.kb.robot_fleet)
        # Add an invalid second variant to general_work_class_rov
        custom_kb.robot_fleet["model_variants"]["general_work_class_rov_broken"] = {
            "family_id": "general_work_class_rov",
            "full_name": "坏掉的通用工作级ROV",
            "hard_params": {
                "power_hp": None,
            },
        }
        with self.assertRaises(RobotSelectionDataError) as cm:
            custom_kb.list_robot_specifications("work_class_rov", "general_work_class_rov")
        self.assertEqual(cm.exception.error_code, "MISSING_SPECIFICATION_VALUE")

    def test_39_existing_knowledge_base_methods_backward_compatibility(self):
        """39. 现有 KnowledgeBase 公共方法行为保持兼容无回归"""
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

    def test_40_sibling_variant_with_orphan_variant_fails_closed(self):
        """40. 即使同 family 存在合法 sibling variant，存在孤儿 orphan variant 时整体查询 fail closed 触发 INVALID_FAMILY_REFERENCE"""
        custom_kb = KnowledgeBase()
        custom_kb.robot_fleet = copy.deepcopy(self.kb.robot_fleet)
        # Add an orphan variant referencing non-existent family
        custom_kb.robot_fleet["model_variants"]["orphan_variant_x"] = {
            "family_id": "non_existent_family_xyz",
            "full_name": "孤儿型号",
            "hard_params": {"power_hp": 999},
        }
        # Attempt query for valid family 'general_work_class_rov' which has valid sibling 'general_work_class_rov_250hp'
        with self.assertRaises(RobotSelectionDataError) as cm:
            custom_kb.list_robot_specifications("work_class_rov", "general_work_class_rov")
        self.assertEqual(cm.exception.error_code, "INVALID_FAMILY_REFERENCE")

    def test_41_ambiguous_family_alias_fails_closed(self):
        """41. family alias 存在歧义时抛出 AMBIGUOUS_FAMILY_SELECTOR"""
        custom_kb = KnowledgeBase()
        custom_kb.robot_fleet = copy.deepcopy(self.kb.robot_fleet)
        # Add identical alias to two families
        custom_kb.robot_fleet["robot_families"]["general_work_class_rov"]["aliases"].append("歧义别名")
        custom_kb.robot_fleet["robot_families"]["crawler_heavy_seabed_robot"]["aliases"].append("歧义别名")
        with self.assertRaises(RobotSelectionDataError) as cm:
            custom_kb.list_robot_specifications("work_class_rov", "歧义别名")
        self.assertEqual(cm.exception.error_code, "AMBIGUOUS_FAMILY_SELECTOR")

    def test_42_auv_with_power_hp_fails_closed(self):
        """42. AUV 错误填写 power_hp 时 fail closed 抛出 INCOMPATIBLE_SPECIFICATION_FIELD"""
        custom_kb = KnowledgeBase()
        custom_kb.robot_fleet = copy.deepcopy(self.kb.robot_fleet)
        custom_kb.robot_fleet["model_variants"]["autonomous_underwater_vehicle_324cc"]["hard_params"]["power_hp"] = 250
        with self.assertRaises(RobotSelectionDataError) as cm:
            custom_kb.list_robot_specifications("auv", "autonomous_underwater_vehicle")
        self.assertEqual(cm.exception.error_code, "INCOMPATIBLE_SPECIFICATION_FIELD")

    def test_43_non_auv_with_numeric_diameter_fails_closed(self):
        """43. 非 AUV 错误填写有效数字 diameter_mm 时 fail closed 抛出 INCOMPATIBLE_SPECIFICATION_FIELD"""
        custom_kb = KnowledgeBase()
        custom_kb.robot_fleet = copy.deepcopy(self.kb.robot_fleet)
        custom_kb.robot_fleet["model_variants"]["general_work_class_rov_250hp"]["hard_params"]["diameter_mm"] = 324
        with self.assertRaises(RobotSelectionDataError) as cm:
            custom_kb.list_robot_specifications("work_class_rov", "general_work_class_rov")
        self.assertEqual(cm.exception.error_code, "INCOMPATIBLE_SPECIFICATION_FIELD")


if __name__ == "__main__":
    unittest.main()
