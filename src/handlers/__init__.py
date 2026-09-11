"""
src/handlers - SEAgent 分层状态机（HSM）与生命周期处理器包
"""

from .base import BaseDialogueHandler, DialogueContext, HandlerResult
from .conversation_router import ConversationRouterHandler
from .slot_filling import SlotFillingHandler
from .constraint_decision import ConstraintDecisionHandler
from .task_commit import TaskCommitHandler
from .equipment_cascade import EquipmentCascadeResolver
from .equipment_scoping import EquipmentScopingHandler
from .equipment_collapse import EquipmentCollapseHandler
from .task_transition import TaskTransitionManager
from .payload_mutation import PayloadMutationManager
from .write_reply_grounder import WriteReplyGrounder
from .oilfield_confirmation import OilfieldConfirmationHandler
from .slot_transaction import SlotTransactionManager
from .off_topic_gate import check_off_topic_gate, is_off_topic_output
from .grounded_catalog import GroundedCatalogHandler
from .telemetry_status import TelemetryStatusHandler

__all__ = [
    "BaseDialogueHandler",
    "DialogueContext",
    "HandlerResult",
    "ConversationRouterHandler",
    "SlotFillingHandler",
    "ConstraintDecisionHandler",
    "TaskCommitHandler",
    "EquipmentCascadeResolver",
    "EquipmentScopingHandler",
    "EquipmentCollapseHandler",
    "TaskTransitionManager",
    "PayloadMutationManager",
    "WriteReplyGrounder",
    "OilfieldConfirmationHandler",
    "SlotTransactionManager",
    "check_off_topic_gate",
    "is_off_topic_output",
    "GroundedCatalogHandler",
    "TelemetryStatusHandler",
]
