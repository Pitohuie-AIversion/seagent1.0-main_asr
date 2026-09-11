"""
tests/test_selection_engine_submodules.py — 机器人选型子模块与门面契约测试
"""

import pytest
from datetime import datetime, timezone
from src.knowledge_retriever import KnowledgeBase
from src.knowledge.selection_engine import RobotSelectionEngine
from src.knowledge.variant_evaluator import VariantEvaluator
from src.knowledge.unit_resolver import UnitResolver
from src.knowledge.models import RobotSelectionDataError, RobotVariantFeasibility
from src.simulated_time import get_simulated_time


@pytest.fixture
def kb():
    return KnowledgeBase()


@pytest.fixture
def engine(kb):
    return kb._selection_engine


class TestVariantEvaluator:
    """测试 VariantEvaluator 规格校验与可行性评估逻辑。"""

    def test_evaluator_properties_and_task_key(self, engine):
        evaluator = engine.evaluator
        assert isinstance(evaluator, VariantEvaluator)
        assert evaluator.kb is engine.kb
        assert "task_templates" in evaluator.task_schemas

        # 合法任务模板
        tmpl = evaluator.validate_task_type_key("pipeline_inspection")
        assert tmpl is not None
        assert "required_capabilities" in tmpl

        # 未知任务模板报错
        with pytest.raises(RobotSelectionDataError) as exc_info:
            evaluator.validate_task_type_key("non_existent_task")
        assert exc_info.value.error_code == "TASK_TEMPLATE_NOT_FOUND"

    def test_config_integrity_validations(self, engine):
        evaluator = engine.evaluator
        # 默认真实配置应完整通过校验
        evaluator.validate_model_variants_integrity()
        evaluator.validate_fleet_units_integrity()

    def test_validated_positive_number_boundaries(self):
        # 合法正数值
        assert VariantEvaluator.validated_positive_number(300, error_code="E", field_name="f") == 300.0
        assert VariantEvaluator.validated_positive_number("150.5", error_code="E", field_name="f") == 150.5

        # 非法情况：布尔值、负数、零、NaN、Inf、字符串
        for bad_val in [True, False, -10, 0, float("nan"), float("inf"), "invalid"]:
            with pytest.raises(RobotSelectionDataError) as exc:
                VariantEvaluator.validated_positive_number(bad_val, error_code="BAD_NUM", field_name="test_f")
            assert exc.value.error_code == "BAD_NUM"

    def test_evaluate_static_robot_variant_depth_and_payload(self, engine):
        evaluator = engine.evaluator
        variants = engine.robot_fleet.get("model_variants", {})
        # 找一个具体的 variant，如 3000m 工作级
        target_vid = next((vid for vid, v in variants.items() if (v.get("hard_params") or {}).get("max_depth_m", 0) >= 3000), None)
        assert target_vid is not None
        v_cfg = variants[target_vid]

        # 1. 深度符合且无载荷要求 -> 满足
        feas = evaluator.evaluate_static_robot_variant(target_vid, v_cfg, {"water_depth": 1000.0})
        assert feas.eligible is True
        assert len(feas.reasons) == 0

        # 2. 深度超限 -> 不满足
        feas_deep = evaluator.evaluate_static_robot_variant(target_vid, v_cfg, {"water_depth": 5000.0})
        assert feas_deep.eligible is False
        assert any("exceeds max_depth_m" in r for r in feas_deep.reasons)

        # 3. 载荷要求测试
        hard_params = v_cfg.get("hard_params", {})
        onboard = hard_params.get("onboard_payloads")
        from src.knowledge.models import normalize_payload_groups
        onboard_list, _ = normalize_payload_groups(onboard)
        if onboard_list:
            sample_onboard = onboard_list[0]
            feas_p = evaluator.evaluate_static_robot_variant(target_vid, v_cfg, {"payload": [sample_onboard]})
            assert feas_p.eligible is True
            assert sample_onboard not in feas_p.requires_installation

    def test_task_starts_within_runtime_window(self):
        get_simulated_time().set_current_time(datetime(2026, 9, 11, 10, 0, 0))
        try:
            # 30分钟后开始 -> 处于 60 分钟窗口内
            assert VariantEvaluator.task_starts_within_runtime_window({"start_time": "2026-09-11T10:30:00"}) is True
            # 120分钟后开始 -> 超出 60 分钟窗口
            assert VariantEvaluator.task_starts_within_runtime_window({"start_time": "2026-09-11T12:30:00"}) is False
            # 过去的时间 -> 超出
            assert VariantEvaluator.task_starts_within_runtime_window({"start_time": "2026-09-11T09:00:00"}) is False
            # 非法时间 -> False
            assert VariantEvaluator.task_starts_within_runtime_window({"start_time": "not_a_date"}) is False
        finally:
            get_simulated_time().reset()


class TestUnitResolver:
    """测试 UnitResolver 实体机消歧与变体索引解析逻辑。"""

    def test_resolver_properties_and_index(self, engine):
        resolver = engine.resolver
        assert isinstance(resolver, UnitResolver)
        assert resolver.kb is engine.kb

        all_rovs = resolver.get_all_rovs()
        assert len(all_rovs) > 0
        assert any(r.get("variant_id") for r in all_rovs)

    def test_rov2type_and_class_key(self, engine):
        resolver = engine.resolver
        rov2type = resolver.get_ROV2type()
        assert isinstance(rov2type, dict)
        assert len(rov2type) > 0

        # resolve_robot_class_key
        assert resolver.resolve_robot_class_key("auv") == "auv"
        assert resolver.resolve_robot_class_key("AUV") == "auv"
        assert resolver.resolve_robot_class_key("工作级ROV") == "work_class_rov"

    def test_exact_variant_resolution(self, engine):
        resolver = engine.resolver
        all_rovs = resolver.get_all_rovs()
        sample = all_rovs[0]
        # 精确 full_name 解析
        matched = resolver.resolve_robot_variant_exact(sample["full_name"])
        assert matched is not None
        assert matched["variant_id"] == sample["variant_id"]

        # 不存在的变体
        assert resolver.resolve_robot_variant_exact("不存在的机型型号") is None

    def test_unit_resolution_and_text_matching(self, engine):
        resolver = engine.resolver
        fleet_units = engine.robot_fleet.get("fleet_units", [])
        if fleet_units:
            sample_unit = fleet_units[0]
            uid = sample_unit.get("unit_id")
            # 通过 unit_id 精确解析
            res_unit = resolver.resolve_robot_unit(uid)
            assert res_unit is not None
            assert res_unit["unit_id"] == uid
            assert "robot" in res_unit

            # 包含口语前缀比如 "采用1号机"
            d_name = sample_unit.get("display_name")
            if d_name:
                res_alias = resolver.resolve_robot_unit(f"使用{d_name}")
                assert res_alias is not None
                assert res_alias["unit_id"] == uid


class TestSelectionEngineFacade:
    """测试 RobotSelectionEngine 主门面代理与 4 级拓扑选择子图生成。"""

    def test_facade_submodules_delegation(self, engine):
        # 验证门面上的静态与类方法代理
        assert engine._validated_positive_number(50, error_code="E", field_name="f") == 50.0
        feas = engine.evaluate_static_robot_variant("sample", {"hard_params": {"max_depth_m": 1000}}, {"water_depth": 500})
        assert feas.eligible is True

        # 验证机型/单机代理
        assert len(engine.get_all_rovs()) > 0
        assert len(engine.get_ROV2type()) > 0

    def test_four_tier_feasible_domain(self, engine):
        domain = engine.get_feasible_robot_selection_domain("pipeline_inspection")
        assert domain["task_type_key"] == "pipeline_inspection"
        assert "classes" in domain
        classes = domain["classes"]
        assert len(classes) > 0

        first_class = classes[0]
        assert "class_id" in first_class
        assert "families" in first_class
        assert len(first_class["families"]) > 0

        first_family = first_class["families"][0]
        assert "variants" in first_family
        assert len(first_family["variants"]) > 0

        first_variant = first_family["variants"][0]
        assert "units" in first_variant

    def test_list_methods_consistency(self, engine):
        # 1. 类别列表
        classes = engine.list_robot_classes("pipeline_inspection")
        assert len(classes) > 0
        cid = classes[0]["class_id"]

        # 2. 系列列表
        families = engine.list_robot_families(cid, "pipeline_inspection")
        assert len(families) > 0
        fid = families[0]["family_id"]

        # 3. 变体列表
        variants = engine.list_robot_variants(cid, fid, "pipeline_inspection")
        assert len(variants) > 0
        vid = variants[0]["variant_id"]

        # 4. 单机列表
        units = engine.list_robot_units(cid, fid, vid, "pipeline_inspection")
        assert isinstance(units, list)
