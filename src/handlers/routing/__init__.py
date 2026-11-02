"""
src/handlers/routing — 闲聊过滤、目录问答、实时遥测状态与拟态时间推演子包
"""

from .off_topic_gate import (
    check_off_topic_gate,
    is_off_topic_output,
)
from .grounded_catalog import GroundedCatalogHandler
from .telemetry_status import TelemetryStatusHandler
from .hypothetical_time_preview import (
    build_hypothetical_time_reply,
)
from .knowledge_reply_grounder import ground_payload_requirement_reply
from .task_structure_grounding import (
    answer_task_structure_query,
    build_task_structure_evidence,
    ground_task_structure_reply,
)

__all__ = [
    "check_off_topic_gate",
    "is_off_topic_output",
    "GroundedCatalogHandler",
    "TelemetryStatusHandler",
    "build_hypothetical_time_reply",
    "ground_payload_requirement_reply",
    "answer_task_structure_query",
    "build_task_structure_evidence",
    "ground_task_structure_reply",
]
