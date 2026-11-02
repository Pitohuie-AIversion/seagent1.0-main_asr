"""
validation - SEAgent 安全门禁、规则引擎与约束验证域

包含：
- validator: 任务槽位合法性验证总门面
- rule_engine: 规则引擎与业务约束推导
- spatial_validator: 空间范围、禁入区与底质规则
- telemetry_gate: 机器人实时遥测、电量、传感器安全门禁
- task_request_guard: 复合/多任务前置拦截守卫
"""

from .rule_engine import (
    RuleEngine,
    START_TIME_PAST_GRACE_MINUTES,
)
from .spatial_validator import (
    SPATIAL_CHECKS,
    SpatialValidator,
)
from .task_request_guard import (
    TaskRequestAnalysis,
    analyze_task_request,
)
from .telemetry_gate import (
    TelemetryGate,
    display_threshold,
    matches_numeric_thresholds,
)
from .validator import (
    TaskValidator,
    ValidationResult,
    Violation,
)

__all__ = [
    "TaskValidator",
    "ValidationResult",
    "Violation",
    "RuleEngine",
    "START_TIME_PAST_GRACE_MINUTES",
    "SpatialValidator",
    "SPATIAL_CHECKS",
    "TelemetryGate",
    "display_threshold",
    "matches_numeric_thresholds",
    "TaskRequestAnalysis",
    "analyze_task_request",
]
