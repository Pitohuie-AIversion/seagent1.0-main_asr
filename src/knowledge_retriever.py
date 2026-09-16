"""
knowledge_retriever.py — 知识库加载与按需检索
不使用向量数据库，基于任务状态进行规则化知识片段选取。
知识总量在10000字以内，精准注入比全量注入更高效。
"""

from typing import Any
from .environment_info import EnvironmentInfo
from .state_info import RobotStateInfo

from .knowledge.models import (
    CONFIG_DIR,
    PAYLOAD_GROUP_KEYS,
    SEABED_TYPE_CN,
    TELEMETRY_VALUE_CN,
    RobotSelectionDataError,
    RobotVariantFeasibility,
    _flatten_payload_items,
    _load,
    _norm,
    _payload_match_key,
    format_seabed_type,
    format_telemetry_value,
    normalize_payload_groups,
    normalize_supported_payloads,
    robot_selection_result_contract_error,
)
from .knowledge import (
    HierarchyGraphManager,
    PromptGrounder,
    QueryExecutor,
    RobotSelectionEngine,
)


class KnowledgeBase:
    def __init__(self):
        self.load_all()  # 改成调用方法

    # ✅ 新增：热重载配置
    def load_all(self):
        self.task_schemas: dict = _load("task_schemas.yaml")
        self.robot_fleet: dict = _load("robot_fleet.yaml")
        self.assets: dict = _load("assets.yaml")
        self.constraints: list = _load("constraints.yaml")["constraints"]
        self.environment: dict = _load("oilfield.yaml")

        self.env_info = EnvironmentInfo()
        self.state_info = RobotStateInfo()
        self._robot_variants_cache: list[dict] | None = None
        self._hierarchy_mgr = HierarchyGraphManager(self)
        self._build_hierarchy_graph()
        self._selection_engine = RobotSelectionEngine(self)
        self._prompt_grounder = PromptGrounder(self)
        self._query_executor = QueryExecutor(self)
        self.ROV2type = self.get_ROV2type()

    def _build_hierarchy_graph(self) -> None:
        """基于 NetworkX (networkx) 建立层级有向图。"""
        self.hierarchy_graph = self._hierarchy_mgr.build_hierarchy_graph()

    def _normalize_node_id(self, item: str, source_level: str | None = None) -> str:
        """支持前缀或裸 Key 的节点名标准化。"""
        return self._hierarchy_mgr.normalize_node_id(item, source_level)

    def get_ancestor_by_level(self, node_id: str, target_level: str, source_level: str | None = None) -> str | None:
        """通过 NetworkX 图求取特定级别的唯一上级祖先节点。"""
        return self._hierarchy_mgr.get_ancestor_by_level(node_id, target_level, source_level)

    def get_descendants_by_level(self, node_id: str, target_level: str, source_level: str | None = None) -> list[str]:
        """通过 NetworkX 图求取特定级别的所有下级后代节点。"""
        return self._hierarchy_mgr.get_descendants_by_level(node_id, target_level, source_level)

    def is_valid_cascade_path(self, ancestor_node: str, descendant_node: str, ancestor_level: str | None = None, descendant_level: str | None = None) -> bool:
        """通过 NetworkX 图校验 Ancestor 到 Descendant 的连通路径合法性。"""
        return self._hierarchy_mgr.is_valid_cascade_path(ancestor_node, descendant_node, ancestor_level, descendant_level)

    # ──────────────────────────────────────────────────────────────────────────
    # 新机器人索引：robot_classes -> robot_families -> model_variants -> fleet_units
    # ──────────────────────────────────────────────────────────────────────────

    def _validate_task_type_key(self, task_type_key: str | None) -> dict | None:
        return self._selection_engine._validate_task_type_key(task_type_key)

    def _validate_fleet_units_integrity(self) -> None:
        self._selection_engine._validate_fleet_units_integrity()

    def _resolve_class_key(self, value: str | None) -> str | None:
        return self._selection_engine._resolve_class_key(value)

    def _robot_class_display_name(self, class_id: str | None) -> str:
        return self._selection_engine._robot_class_display_name(class_id)

    def _resolve_family_key(self, value: str | None) -> str | None:
        return self._selection_engine._resolve_family_key(value)

    def _validate_model_variants_integrity(self) -> None:
        self._selection_engine._validate_model_variants_integrity()

    def _extract_and_validate_variant_spec(
        self,
        robot_class_id: str,
        family_id: str,
        variant_id: str,
        variant: dict,
    ) -> dict:
        return self._selection_engine._extract_and_validate_variant_spec(
            robot_class_id, family_id, variant_id, variant
        )

    @staticmethod
    def _validated_positive_number(
        value: Any,
        *,
        error_code: str,
        field_name: str,
    ) -> float:
        return RobotSelectionEngine._validated_positive_number(
            value, error_code=error_code, field_name=field_name
        )

    @classmethod
    def evaluate_static_robot_variant(
        cls,
        variant_id: str,
        variant_cfg: dict,
        task_state: dict | None,
    ) -> RobotVariantFeasibility:
        return RobotSelectionEngine.evaluate_static_robot_variant(
            variant_id, variant_cfg, task_state
        )

    @staticmethod
    def _task_starts_within_runtime_window(
        task_state: dict | None,
        *,
        time_window_minutes: int = 60,
    ) -> bool:
        return RobotSelectionEngine._task_starts_within_runtime_window(
            task_state, time_window_minutes=time_window_minutes
        )

    def validate_robot_selection_from_task_state(
        self,
        task_state: dict,
        *,
        require_unit: bool = False,
    ) -> dict | None:
        return self._selection_engine.validate_robot_selection_from_task_state(
            task_state, require_unit=require_unit
        )

    def get_feasible_robot_selection_domain(
        self,
        task_type_key: str | None,
        task_state: dict | None = None,
        purpose: str = "interactive",
    ) -> dict:
        return self._selection_engine.get_feasible_robot_selection_domain(
            task_type_key, task_state=task_state, purpose=purpose
        )

    def list_robot_classes(self, task_type_key: str | None = None) -> list[dict]:
        return self._selection_engine.list_robot_classes(task_type_key)

    def list_robot_families(
        self,
        robot_class: str,
        task_type_key: str | None = None,
    ) -> list[dict]:
        return self._selection_engine.list_robot_families(robot_class, task_type_key)

    def list_robot_specifications(
        self,
        robot_class: str,
        family: str,
        task_type_key: str | None = None,
    ) -> list[dict]:
        return self._selection_engine.list_robot_specifications(robot_class, family, task_type_key)

    def list_robot_variants(
        self,
        robot_class: str,
        family: str,
        task_type_key: str | None = None,
    ) -> list[dict]:
        return self._selection_engine.list_robot_variants(robot_class, family, task_type_key)

    def list_robot_units(
        self,
        robot_class: str,
        family: str,
        specification: dict | Any,
        task_type_key: str | None = None,
    ) -> list[dict]:
        return self._selection_engine.list_robot_units(
            robot_class, family, specification, task_type_key
        )

    def validate_static_robot_selection(
        self,
        robot_class: str,
        family: str,
        specification: dict | Any,
        unit_id: str,
        task_type_key: str | None = None,
    ) -> dict:
        return self._selection_engine.validate_static_robot_selection(
            robot_class, family, specification, unit_id, task_type_key
        )

    def get_robot_classes(self) -> dict:
        return self._selection_engine.get_robot_classes()

    def get_robot_class_labels(self) -> list[str]:
        return self._selection_engine.get_robot_class_labels()

    def get_task_allowed_robot_classes(self, task_type_key: str | None) -> list[str]:
        return self._selection_engine.get_task_allowed_robot_classes(task_type_key)

    def get_task_required_capabilities(self, task_type_key: str | None) -> list[str]:
        return self._selection_engine.get_task_required_capabilities(task_type_key)

    def robot_matches_task(self, robot: dict | None, task_type_key: str | None) -> bool:
        return self._selection_engine.robot_matches_task(robot, task_type_key)

    def get_robot_families_for_classes(
        self,
        robot_class_keys: list[str],
        required_capabilities: list[str] | None = None,
    ) -> list[tuple[str, dict]]:
        return self._selection_engine.get_robot_families_for_classes(
            robot_class_keys, required_capabilities
        )

    def get_robot_families_for_task(self, task_type_key: str | None) -> list[tuple[str, dict]]:
        return self._selection_engine.get_robot_families_for_task(task_type_key)

    def get_task_allowed_robot_family_names(self, task_type_key: str | None) -> list[str]:
        return self._selection_engine.get_task_allowed_robot_family_names(task_type_key)

    def resolve_robot_family_id(
        self,
        family_selector: str,
        task_type_key: str | None = None,
    ) -> str | None:
        return self._selection_engine.resolve_robot_family_id(family_selector, task_type_key)

    def resolve_robot_family(
        self,
        family_selector: str,
        task_type_key: str | None = None,
    ) -> dict | None:
        return self._selection_engine.resolve_robot_family(family_selector, task_type_key)

    def get_model_variants_for_family(self, family_id: str) -> list[tuple[str, dict]]:
        return self._selection_engine.get_model_variants_for_family(family_id)

    def get_model_variants_for_task(self, task_type_key: str | None) -> list[tuple[str, dict]]:
        return self._selection_engine.get_model_variants_for_task(task_type_key)

    def get_fleet_units_for_variant(self, variant_id: str) -> list[dict]:
        return self._selection_engine.get_fleet_units_for_variant(variant_id)

    def _build_robot_variant(
        self,
        robot_classes: dict,
        family_id: str,
        family: dict,
        variant_id: str,
        variant: dict,
    ) -> dict:
        return self._selection_engine._build_robot_variant(
            robot_classes, family_id, family, variant_id, variant
        )

    def _build_robot_variant_index(self) -> list[dict]:
        return self._selection_engine._build_robot_variant_index()

    def get_all_rovs(self) -> list[dict]:
        return self._selection_engine.get_all_rovs()

    def get_task_allowed_robot_variants(
        self,
        task_type_key: str | None,
        family_selector: str | None = None,
        class_selector: str | None = None,
    ) -> list[dict]:
        return self._selection_engine.get_task_allowed_robot_variants(
            task_type_key, family_selector=family_selector, class_selector=class_selector
        )

    def get_ROV2type(self) -> dict:
        return self._selection_engine.get_ROV2type()

    # ──────────────────────────────────────────────────────────────────────────
    # 按任务状态选取相关知识片段
    # ──────────────────────────────────────────────────────────────────────────

    def get_supported_task(self) -> list:
        return self._prompt_grounder.get_supported_task()

    def get_context_for_state(self, task_state: dict) -> str:
        return self._prompt_grounder.get_context_for_state(task_state)

    def _robot_category_overview(self) -> str:
        return self._prompt_grounder._robot_category_overview()

    def _task_rov_constraint(self, task_type: str) -> str:
        return self._prompt_grounder._task_rov_constraint(task_type)

    def _rovs_by_category(self, category_value: str, task_type: str | None = None) -> str:
        return self._prompt_grounder._rovs_by_category(category_value, task_type=task_type)

    def _rovs_for_task(self, task_type: str, class_selector: str | None = None) -> str:
        return self._prompt_grounder._rovs_for_task(task_type, class_selector=class_selector)

    def _get_rov_info(self, model_or_alias: str) -> str | None:
        return self._prompt_grounder._get_rov_info(model_or_alias)

    def _find_rov(self, name: str) -> dict | None:
        return self._selection_engine._find_rov(name)

    @staticmethod
    def _match_robot_variants(
        robots: list[dict],
        selector: str,
    ) -> dict | None:
        return RobotSelectionEngine._match_robot_variants(robots, selector)

    def _resolve_robot_variant_exact(self, selector: str) -> dict | None:
        return self._selection_engine._resolve_robot_variant_exact(selector)

    def _resolve_robot_unit_exact(
        self,
        unit_selector: str,
        task_type_key: str | None = None,
    ) -> dict | None:
        return self._selection_engine._resolve_robot_unit_exact(unit_selector, task_type_key)

    def resolve_robot_unit(
        self,
        unit_selector: str,
        task_type_key: str | None = None,
        variant_selector: str | None = None,
    ) -> dict | None:
        return self._selection_engine.resolve_robot_unit(
            unit_selector, task_type_key, variant_selector
        )

    def resolve_robot_unit_from_text(
        self,
        text: str,
        task_type_key: str | None = None,
    ) -> dict | None:
        return self._selection_engine.resolve_robot_unit_from_text(text, task_type_key)

    def find_rov_by_description(self, description: str) -> list[dict]:
        return self._selection_engine.find_rov_by_description(description)

    def get_rov(self, model_name: str) -> dict | None:
        return self._selection_engine.get_rov(model_name)

    def get_rov_for_task(
        self,
        model_name: str,
        task_type: str | None,
        family_selector: str | None = None,
    ) -> dict | None:
        return self._selection_engine.get_rov_for_task(
            model_name, task_type, family_selector=family_selector
        )

    def _resolve_robot_class_key(self, value: str) -> str:
        return self._selection_engine._resolve_robot_class_key(value)

    def _cable_types_overview(self) -> str:
        return self._prompt_grounder._cable_types_overview()

    def _payload_suggestions(self, task_type: str) -> str:
        return self._prompt_grounder._payload_suggestions(task_type)

    def _vessels_overview(self) -> str:
        return self._prompt_grounder._vessels_overview()

    def get_vessel(self, vessel_id: str) -> dict | None:
        return self._query_executor.get_vessel(vessel_id)

    def get_task_schema(self, template_key: str) -> dict:
        return self._query_executor.get_task_schema(template_key)

    def get_task_type_map(self) -> dict[str, str]:
        return self._query_executor.get_task_type_map()

    def get_all_task_type_values(self) -> list[str]:
        return self._query_executor.get_all_task_type_values()

    def get_environment_for_coords(self, coords: dict) -> dict | None:
        return self._query_executor.get_environment_for_coords(coords)

    def get_constraints(self) -> list[dict]:
        return self._query_executor.get_constraints()

    def get_environment_info_dict(self, coords: dict) -> dict:
        return self._query_executor.get_environment_info_dict(coords)

    def get_robot_state_dict(self, equipment_selector: str) -> dict:
        return self._query_executor.get_robot_state_dict(equipment_selector)

    def get_unit_state_snapshot(self, unit_id: str) -> dict:
        return self._query_executor.get_unit_state_snapshot(unit_id)

    def check_runtime_availability(self, unit_id: str, *, max_age_seconds: int = 600) -> dict:
        return self._query_executor.check_runtime_availability(unit_id, max_age_seconds=max_age_seconds)

    def get_device_alias_index(self) -> dict[str, list[str]]:
        return self._query_executor.get_device_alias_index()

    def get_ambiguous_device_terms(self) -> set[str]:
        return self._query_executor.get_ambiguous_device_terms()

    def get_all_device_terms(self) -> set[str]:
        return self._query_executor.get_all_device_terms()

    def get_environment_alias_index(self) -> dict[str, list[str]]:
        return self._query_executor.get_environment_alias_index()

    def _find_environment_entity_targets(self, user_message: str) -> tuple[str | None, list[str]]:
        return self._query_executor._find_environment_entity_targets(user_message)

    def _resolve_typed_read_query(
        self,
        query_type: str,
        user_message: str,
        context: dict,
    ) -> tuple[str, str]:
        return self._query_executor._resolve_typed_read_query(query_type, user_message, context)

    @staticmethod
    def _match_payload_catalog(
        payload_catalog: dict,
        user_message: str,
    ) -> list[dict]:
        return QueryExecutor._match_payload_catalog(payload_catalog, user_message)

    def execute_typed_query(
        self,
        query_type: str,
        user_message: str,
        context: dict | None = None,
    ) -> dict:
        return self._query_executor.execute_typed_query(query_type, user_message, context=context)

    def _execute_environment_query(
        self,
        user_message: str,
        context: dict,
        response: dict,
    ) -> dict:
        return self._query_executor._execute_environment_query(user_message, context, response)

    def _execute_device_capability_query(
        self,
        user_message: str,
        context: dict,
        response: dict,
    ) -> dict:
        return self._query_executor._execute_device_capability_query(user_message, context, response)

    def _find_query_entity_targets(self, user_message: str) -> tuple[str | None, list[str]]:
        return self._query_executor._find_query_entity_targets(user_message)

    def _resolve_context_entity_targets(
        self,
        selector: str,
        task_type_key: str | None,
    ) -> list[str]:
        return self._query_executor._resolve_context_entity_targets(selector, task_type_key)

    def _robots_for_entity_target(
        self,
        entity_target: str,
        task_type_key: str | None,
    ) -> tuple[str | None, list[dict]]:
        return self._query_executor._robots_for_entity_target(entity_target, task_type_key)

    @staticmethod
    def _parse_depth_condition(user_message: str) -> dict:
        return QueryExecutor._parse_depth_condition(user_message)

    @staticmethod
    def _matches_depth_condition(max_depth: Any, condition: dict) -> bool:
        return QueryExecutor._matches_depth_condition(max_depth, condition)
