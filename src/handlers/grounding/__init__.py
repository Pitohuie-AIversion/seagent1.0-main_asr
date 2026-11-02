"""
src/handlers/grounding — 槽位提交事实锚点、回复展示生成与幻觉清洗子包
"""

from .write_reply_grounder import WriteReplyGrounder
from .assertion_scrubber import (
    clean_hallucinations_and_syntax,
    remove_model_receipts,
    remove_write_off_topic_template,
    remove_write_welcome_template,
    scrub_uncommitted_slot_assertions,
    scrub_uncommitted_start_time_assertions,
    scrub_uncommitted_vessel_assertions,
)
from .field_grounder import (
    FACT_FIELD_ALIASES,
    ground_blocked_publish_instructions,
    ground_field_assertions,
    ground_payload_assertions,
    ground_payload_classification,
    ground_time_windows,
    ground_water_depth_assertions,
    remove_unsupported_now_gloss,
    time_claim_matches,
)
from .receipt_builder import (
    build_authoritative_receipt,
    compose_final_reply,
    filter_robot_selection_unresolved,
    project_legacy_equipment_class_candidate,
    repair_missing_field_block,
)

__all__ = [
    "WriteReplyGrounder",
    "clean_hallucinations_and_syntax",
    "remove_model_receipts",
    "remove_write_off_topic_template",
    "remove_write_welcome_template",
    "scrub_uncommitted_slot_assertions",
    "scrub_uncommitted_start_time_assertions",
    "scrub_uncommitted_vessel_assertions",
    "FACT_FIELD_ALIASES",
    "ground_blocked_publish_instructions",
    "ground_field_assertions",
    "ground_payload_assertions",
    "ground_payload_classification",
    "ground_time_windows",
    "ground_water_depth_assertions",
    "remove_unsupported_now_gloss",
    "time_claim_matches",
    "build_authoritative_receipt",
    "compose_final_reply",
    "filter_robot_selection_unresolved",
    "project_legacy_equipment_class_candidate",
    "repair_missing_field_block",
]
