"""
src/slots/codec — 槽位快照持久化、Schema 契约与版本迁移子包
"""

from .slot_snapshot_codec import (
    ALLOWED_INTERNAL_SLOTS,
    INTERNAL_SLOT_TYPES,
    LEGACY_SCHEMA_TYPES,
    SLOT_SNAPSHOT_SCHEMA_VERSION,
    SnapshotValidationError,
    SlotSnapshotCodec,
    VALID_SLOT_STATUSES,
    VALID_VALUE_TYPES,
    _robot_selection_lineage_contract_error,
    _validate_and_build_restored_slot,
    _validate_and_clone_slot_mapping,
    _validate_slot_value_type_compatibility,
    _validate_spec_slot_data,
    normalize_slot_value_type,
    validate_specification_object,
    validate_specification_selector_input,
)

__all__ = [
    "ALLOWED_INTERNAL_SLOTS",
    "INTERNAL_SLOT_TYPES",
    "LEGACY_SCHEMA_TYPES",
    "SLOT_SNAPSHOT_SCHEMA_VERSION",
    "SnapshotValidationError",
    "SlotSnapshotCodec",
    "VALID_SLOT_STATUSES",
    "VALID_VALUE_TYPES",
    "_robot_selection_lineage_contract_error",
    "_validate_and_build_restored_slot",
    "_validate_and_clone_slot_mapping",
    "_validate_slot_value_type_compatibility",
    "_validate_spec_slot_data",
    "normalize_slot_value_type",
    "validate_specification_object",
    "validate_specification_selector_input",
]
