"""
src/handlers - SEAgent 分层状态机（HSM）与生命周期处理器包
"""

# Level 1 核心主生命周期处理器
from .base import BaseDialogueHandler, DialogueContext, HandlerResult
from .conversation_router import ConversationRouterHandler
from .slot_filling import SlotFillingHandler
from .constraint_decision import ConstraintDecisionHandler
from .task_commit import TaskCommitHandler

# 子包模块及能力导入 (Level 2 & 3 从属领域)
from .equipment import (
    EquipmentCascadeResolver,
    EquipmentCollapseHandler,
    EquipmentScopingHandler,
)
from .lifecycle import (
    ExecutionControlHandler,
    TaskTransitionManager,
    is_business_identity_query,
    is_confirmation_only,
    is_final_publish_confirmation,
    is_ignore_warning,
    is_payload_modification_request,
    is_user_cancelled,
    is_user_confirmed,
    is_user_requested_modification,
)
from .extraction import (
    ExtractionPipelineResult,
    OilfieldConfirmationHandler,
    PayloadMutationManager,
    SlotExtractionPipeline,
    SlotTransactionManager,
    ground_explicit_values,
)
from .grounding import WriteReplyGrounder
from .routing import (
    GroundedCatalogHandler,
    TelemetryStatusHandler,
    check_off_topic_gate,
    is_off_topic_output,
)

# 公开子包命名空间
from . import equipment, extraction, grounding, lifecycle, routing

__all__ = [
    # Subpackages
    "equipment",
    "extraction",
    "grounding",
    "lifecycle",
    "routing",
    # Level 1
    "BaseDialogueHandler",
    "DialogueContext",
    "HandlerResult",
    "ConversationRouterHandler",
    "SlotFillingHandler",
    "ConstraintDecisionHandler",
    "TaskCommitHandler",
    # Submodule symbols
    "EquipmentCascadeResolver",
    "EquipmentScopingHandler",
    "EquipmentCollapseHandler",
    "TaskTransitionManager",
    "PayloadMutationManager",
    "WriteReplyGrounder",
    "OilfieldConfirmationHandler",
    "SlotTransactionManager",
    "ground_explicit_values",
    "check_off_topic_gate",
    "is_off_topic_output",
    "GroundedCatalogHandler",
    "TelemetryStatusHandler",
    "ExecutionControlHandler",
    "SlotExtractionPipeline",
    "ExtractionPipelineResult",
    "is_business_identity_query",
    "is_confirmation_only",
    "is_final_publish_confirmation",
    "is_ignore_warning",
    "is_payload_modification_request",
    "is_user_cancelled",
    "is_user_confirmed",
    "is_user_requested_modification",
]
