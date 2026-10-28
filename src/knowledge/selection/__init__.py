"""
src/knowledge/selection — 机器人4级拓扑选型、规格校验、可用域计算与机体消歧子领域
"""

from .models import (
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
from .hierarchy_graph import HierarchyGraphManager
from .variant_evaluator import VariantEvaluator
from .unit_resolver import UnitResolver
from .selection_engine import RobotSelectionEngine
from .prompt_grounder import PromptGrounder

__all__ = [
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
