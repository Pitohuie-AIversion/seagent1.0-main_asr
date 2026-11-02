"""
src/knowledge - SEAgent 领域知识检索、机器人拓扑选型与规格评估包
"""

from .query import DeviceAliasIndexer, EnvironmentRetriever, TypedQueryEngine
from .query_executor import QueryExecutor
from .selection import (
    CONFIG_DIR,
    PAYLOAD_GROUP_KEYS,
    SEABED_TYPE_CN,
    TELEMETRY_VALUE_CN,
    HierarchyGraphManager,
    PromptGrounder,
    RobotSelectionDataError,
    RobotSelectionEngine,
    RobotVariantFeasibility,
    UnitResolver,
    VariantEvaluator,
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

__all__ = [
    "QueryExecutor",
    "DeviceAliasIndexer",
    "EnvironmentRetriever",
    "TypedQueryEngine",
    "RobotSelectionEngine",
    "VariantEvaluator",
    "UnitResolver",
    "HierarchyGraphManager",
    "PromptGrounder",
    "RobotSelectionDataError",
    "RobotVariantFeasibility",
    "CONFIG_DIR",
    "PAYLOAD_GROUP_KEYS",
    "SEABED_TYPE_CN",
    "TELEMETRY_VALUE_CN",
    "format_seabed_type",
    "format_telemetry_value",
    "normalize_payload_groups",
    "normalize_supported_payloads",
    "robot_selection_result_contract_error",
]
