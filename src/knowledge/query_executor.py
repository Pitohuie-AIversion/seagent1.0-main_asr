"""
src/knowledge/query_executor.py — 强类型只读知识查询、海域环境检索与设备别名索引门面
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .query import DeviceAliasIndexer, EnvironmentRetriever, TypedQueryEngine

if TYPE_CHECKING:
    from ..knowledge_retriever import KnowledgeBase


class QueryExecutor:
    """强类型知识查询、海域环境检索与设备别名索引门面执行器。"""

    def __init__(self, kb: "KnowledgeBase"):
        self.kb = kb
        self._alias_indexer = DeviceAliasIndexer(kb)
        self._environment_retriever = EnvironmentRetriever(kb)
        self._typed_query_engine = TypedQueryEngine(
            kb=kb,
            alias_indexer=self._alias_indexer,
            environment_retriever=self._environment_retriever,
        )

    # -------------------------------------------------------------------------
    # 知识库基础属性代理
    # -------------------------------------------------------------------------
    @property
    def environment(self) -> dict:
        return self.kb.environment

    @property
    def assets(self) -> dict:
        return self.kb.assets

    @property
    def task_schemas(self) -> dict:
        return self.kb.task_schemas

    @property
    def constraints(self) -> list:
        return self.kb.constraints

    @property
    def robot_fleet(self) -> dict:
        return self.kb.robot_fleet

    @property
    def env_info(self) -> Any:
        return self.kb.env_info

    @property
    def state_info(self) -> Any:
        return self.kb.state_info

    # -------------------------------------------------------------------------
    # 船舶与任务模板基础查询
    # -------------------------------------------------------------------------
    def get_vessel(self, vessel_id: str) -> dict | None:
        """根据 ID 或别名查询船舶资产信息。"""
        vid_lower = vessel_id.lower().replace(" ", "")
        for v in self.assets.get("vessels", []):
            targets = [v["id"].lower()] + [a.lower().replace(" ", "") for a in v.get("aliases", [])]
            if any(vid_lower in t or t in vid_lower for t in targets):
                return v
        return None

    def get_task_schema(self, template_key: str) -> dict:
        """返回指定任务模板，兼容 main 的公开查询接口。"""
        return self.task_schemas.get("task_templates", {}).get(template_key, {})

    def get_task_type_map(self) -> dict[str, str]:
        """
        从 task_schemas.yaml 动态构建 {task_type_value: template_key} 反查字典。
        例如：{"管缆巡检": "pipeline_inspection",
               "采油树控制面板插入": "tree_valve_operation", ...}
        """
        mapping: dict[str, str] = {}
        for template_key, cfg in self.task_schemas.get("task_templates", {}).items():
            for value in cfg.get("task_type_values", []):
                mapping[value] = template_key
        return mapping

    def get_all_task_type_values(self) -> list[str]:
        """返回所有合法 task_type 值的平铺列表（供 LLM 提示和拒绝判断用）。"""
        values: list[str] = []
        for cfg in self.task_schemas.get("task_templates", {}).values():
            values.extend(cfg.get("task_type_values", []))
        return values

    def get_constraints(self) -> list[dict]:
        """返回所有业务规则与安全约束。"""
        return self.constraints

    # -------------------------------------------------------------------------
    # 海域环境与遥测状态（代理至 EnvironmentRetriever）
    # -------------------------------------------------------------------------
    def get_environment_for_coords(self, coords: dict) -> dict | None:
        """根据经纬度匹配所在油气田区域。"""
        return self._environment_retriever.get_environment_for_coords(coords)

    def get_environment_info_dict(self, coords: dict) -> dict:
        """根据坐标返回动态环境信息。"""
        return self._environment_retriever.get_environment_info_dict(coords)

    def get_robot_state_dict(self, equipment_selector: str) -> dict:
        """获取指定设备实体的当前遥测状态。"""
        return self._environment_retriever.get_robot_state_dict(equipment_selector)

    def get_unit_state_snapshot(self, unit_id: str) -> dict:
        """获取单机设备的快照状态。"""
        return self._environment_retriever.get_unit_state_snapshot(unit_id)

    def check_runtime_availability(self, unit_id: str, *, max_age_seconds: int = 600) -> dict:
        """检查单机设备运行时遥测可用性。"""
        return self._environment_retriever.check_runtime_availability(unit_id, max_age_seconds=max_age_seconds)

    def get_environment_alias_index(self) -> dict[str, list[str]]:
        """构建油气田、禁入保护区与DVL风险区的多维度别名索引。"""
        return self._environment_retriever.get_environment_alias_index()

    def _find_environment_entity_targets(self, user_message: str) -> tuple[str | None, list[str]]:
        """从用户文本中匹配最佳环境实体目标。"""
        return self._environment_retriever.find_environment_entity_targets(user_message)

    def _execute_environment_query(self, user_message: str, context: dict, response: dict) -> dict:
        """执行作业海域、油气田、禁入区与DVL风险区的结构化知识检索。"""
        return self._environment_retriever.execute_environment_query(user_message, context, response)

    # -------------------------------------------------------------------------
    # 设备别名索引与实体解析（代理至 DeviceAliasIndexer）
    # -------------------------------------------------------------------------
    def get_device_alias_index(self) -> dict[str, list[str]]:
        """按类别、系列、型号、单机分层构建设备别名索引。"""
        return self._alias_indexer.get_device_alias_index()

    def get_ambiguous_device_terms(self) -> set[str]:
        """返回映射到多个分层实体的设备别名。"""
        return self._alias_indexer.get_ambiguous_device_terms()

    def get_all_device_terms(self) -> set[str]:
        """返回可安全用于意图路由的非歧义设备词集合。"""
        return self._alias_indexer.get_all_device_terms()

    def _find_query_entity_targets(self, user_message: str) -> tuple[str | None, list[str]]:
        """从用户文本中匹配最佳设备实体目标。"""
        return self._alias_indexer.find_query_entity_targets(user_message)

    def _resolve_context_entity_targets(self, selector: str, task_type_key: str | None) -> list[str]:
        """根据上下文选择器解析实体 targets。"""
        return self._alias_indexer.resolve_context_entity_targets(selector, task_type_key)

    def _robots_for_entity_target(self, entity_target: str, task_type_key: str | None) -> tuple[str | None, list[dict]]:
        """根据实体 target 解析允许使用的具体机器人变体配置。"""
        return self._alias_indexer.robots_for_entity_target(entity_target, task_type_key)

    # -------------------------------------------------------------------------
    # 强类型查询调度与设备能力验证（代理至 TypedQueryEngine）
    # -------------------------------------------------------------------------
    def _resolve_typed_read_query(self, query_type: str, user_message: str, context: dict) -> tuple[str, str]:
        """用结构化主题和权威别名选择事实域，不重新猜测自然语言意图。"""
        return self._typed_query_engine.resolve_typed_read_query(query_type, user_message, context)

    def _payload_configuration_evidence(self) -> dict:
        """返回载荷配置与机载/扩展区分证据。"""
        return self._typed_query_engine.payload_configuration_evidence()

    def _execute_device_capability_query(self, user_message: str, context: dict, response: dict) -> dict:
        """执行机器人型号规格、水深能力与下潜限制匹配查询。"""
        return self._typed_query_engine.execute_device_capability_query(user_message, context, response)

    def execute_typed_query(self, query_type: str, user_message: str, context: dict | None = None) -> dict:
        """执行强类型只读知识查询，并返回稳定的结构化证据。"""
        return self._typed_query_engine.execute_typed_query(query_type, user_message, context)

    # -------------------------------------------------------------------------
    # 静态工具方法（与外部直接通过类对象调用的契约保持 100% 兼容）
    # -------------------------------------------------------------------------
    @staticmethod
    def _match_payload_catalog(payload_catalog: dict, user_message: str) -> list[dict]:
        """从载荷目录中根据名称与别名匹配候选载荷。"""
        return TypedQueryEngine.match_payload_catalog(payload_catalog, user_message)

    @staticmethod
    def _parse_depth_condition(user_message: str) -> dict:
        """从用户文本中解析水深约束条件。"""
        return TypedQueryEngine.parse_depth_condition(user_message)

    @staticmethod
    def _matches_depth_condition(max_depth: Any, condition: dict) -> bool:
        """判断最大水深是否满足给定的深度条件。"""
        return TypedQueryEngine.matches_depth_condition(max_depth, condition)
