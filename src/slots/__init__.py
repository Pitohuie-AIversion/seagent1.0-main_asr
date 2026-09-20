"""
slots - SEAgent 槽位存储、候选值仲裁与快照持久化域

包含：
- slot_store: 槽位主数据结构与状态机存储
- slot_snapshot_codec: 槽位快照的序列化/反序列化与安全校验
- slot_list_mutation: 列表槽位差量增删与更新合并引擎
- candidate_resolver: 槽位抽取候选值仲裁与确认
- normalization_contract: 槽位值标准化契约定义
- task_patch: 任务差量补丁实体
- task_slot_filter: 任务类型对应槽位过滤与映射
- visible_selection_provenance: 前端可见选项与候选来源溯源
"""

from .candidate_resolver import CandidateResolver
from .normalization_contract import (
    NormalizationApplyPlan,
    NormalizationContractError,
    NormalizedSlotApply,
    NormalizedTaskPatch,
    SlotNormalizationOutcome,
    normalize_task_patch,
    normalized_task_patch_to_apply_plan,
    validate_normalization_runtime_flags,
)
from .slot_list_mutation import (
    SlotListMutationEngine,
    normalize_payload_match_key,
)
from .slot_snapshot_codec import (
    ALLOWED_INTERNAL_SLOTS,
    INTERNAL_SLOT_TYPES,
    LEGACY_SCHEMA_TYPES,
    SLOT_SNAPSHOT_SCHEMA_VERSION,
    SlotSnapshotCodec,
    normalize_slot_value_type,
    validate_specification_object,
    validate_specification_selector_input,
)
from .slot_store import (
    Slot,
    SlotStore,
    ValidationAcknowledgement,
)
from .task_patch import (
    ListMutationPatch,
    SlotPatch,
    TaskPatch,
    build_task_patch,
)
from .task_slot_filter import TaskSlotFilter
from .visible_selection_provenance import (
    OrdinalReference,
    build_candidate_terms,
    parse_ordinal_reference,
    visible_ordinal_matches_candidate,
)

__all__ = [
    "Slot",
    "SlotStore",
    "ValidationAcknowledgement",
    "CandidateResolver",
    "NormalizationApplyPlan",
    "NormalizationContractError",
    "NormalizedSlotApply",
    "NormalizedTaskPatch",
    "SlotNormalizationOutcome",
    "normalize_task_patch",
    "normalized_task_patch_to_apply_plan",
    "validate_normalization_runtime_flags",
    "SlotListMutationEngine",
    "normalize_payload_match_key",
    "ALLOWED_INTERNAL_SLOTS",
    "INTERNAL_SLOT_TYPES",
    "LEGACY_SCHEMA_TYPES",
    "SLOT_SNAPSHOT_SCHEMA_VERSION",
    "SlotSnapshotCodec",
    "normalize_slot_value_type",
    "validate_specification_object",
    "validate_specification_selector_input",
    "TaskPatch",
    "SlotPatch",
    "ListMutationPatch",
    "build_task_patch",
    "TaskSlotFilter",
    "OrdinalReference",
    "build_candidate_terms",
    "parse_ordinal_reference",
    "visible_ordinal_matches_candidate",
]
