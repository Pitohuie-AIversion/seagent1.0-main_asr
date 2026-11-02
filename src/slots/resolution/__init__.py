"""
src/slots/resolution — 候选值消歧、归一化契约与可见来源溯源子包
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
from .visible_selection_provenance import (
    OrdinalReference,
    build_candidate_terms,
    parse_ordinal_reference,
    visible_ordinal_matches_candidate,
)

__all__ = [
    "CandidateResolver",
    "NormalizationApplyPlan",
    "NormalizationContractError",
    "NormalizedSlotApply",
    "NormalizedTaskPatch",
    "SlotNormalizationOutcome",
    "normalize_task_patch",
    "normalized_task_patch_to_apply_plan",
    "validate_normalization_runtime_flags",
    "OrdinalReference",
    "build_candidate_terms",
    "parse_ordinal_reference",
    "visible_ordinal_matches_candidate",
]
