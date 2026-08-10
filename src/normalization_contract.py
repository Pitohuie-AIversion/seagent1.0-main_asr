"""normalization_contract.py — TaskPatch -> NormalizationContract -> NormalizedTaskPatch

定义纯数据、无副作用规范化中间协议与底层数据契约。
解耦 TaskPatch 候选输入与 Downstream DialogueManager / SlotStore / Transaction Runtime。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .normalizer import FieldNormalizer
from .task_patch import (
    ListMutationPatch,
    SlotPatch,
    TaskPatch,
    validate_confidence,
)


class NormalizationContractError(ValueError):
    """Normalization Contract 基础异常。"""

    pass


NORMALIZATION_RUNTIME_PASSTHROUGH_KEYS: frozenset[str] = frozenset({
    "task_type",
    "task_type_key",
    "equipment_class",
    "equipment_family",
    "equipment_type",
    "equipment_name",
    "equipment_unit_id",
    "emergency_mode",
    "rov_description",
    "oilfield_name",
})


def validate_normalization_runtime_flags(
    task_patch_enabled: bool,
    normalization_v2_enabled: bool,
) -> None:
    """验证 Feature Flag 矩阵合法性。D 组合 (false/true) 必须 FAIL CLOSED。"""
    if normalization_v2_enabled and not task_patch_enabled:
        raise NormalizationContractError(
            "normalization_contract_v2=True 时必须同时开启 task_patch_v2=True，非法 Feature Flag 组合"
        )


@dataclass(frozen=True)
class SlotNormalizationOutcome:
    key: str
    success: bool
    normalized_value: Any | None
    candidate_value: Any
    raw_value: Any
    confidence: float
    source: str
    resolution_method: str | None = None
    error_code: str | None = None
    error_message: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.key, str) or not self.key.strip():
            raise NormalizationContractError(
                f"SlotNormalizationOutcome key 必须为非空 str，收到 {self.key!r}"
            )
        if not isinstance(self.success, bool):
            raise NormalizationContractError(
                f"SlotNormalizationOutcome success 必须为严格 bool 类型，收到 {type(self.success)}"
            )
        if self.candidate_value is None or self.candidate_value == "":
            raise NormalizationContractError(
                f"SlotNormalizationOutcome candidate_value 不得为 None 或空字符串，收到 {self.candidate_value!r}"
            )
        if self.raw_value is None or (
            isinstance(self.raw_value, str) and not self.raw_value.strip()
        ):
            raise NormalizationContractError(
                f"SlotNormalizationOutcome raw_value 必须存在且非空，收到 {self.raw_value!r}"
            )
        validate_confidence(
            self.confidence,
            field_name=f"SlotNormalizationOutcome({self.key}).confidence",
        )
        if not isinstance(self.source, str) or not self.source.strip():
            raise NormalizationContractError(
                f"SlotNormalizationOutcome source 必须为非空 str，收到 {self.source!r}"
            )
        if self.resolution_method is not None:
            if (
                not isinstance(self.resolution_method, str)
                or not self.resolution_method.strip()
            ):
                raise NormalizationContractError(
                    f"SlotNormalizationOutcome resolution_method 若存在则必须为非空 str，收到 {self.resolution_method!r}"
                )

        if self.success:
            if self.normalized_value is None:
                raise NormalizationContractError(
                    f"SlotNormalizationOutcome(success=True) 时 normalized_value 不得为 None"
                )
            if self.error_code is not None:
                raise NormalizationContractError(
                    f"SlotNormalizationOutcome(success=True) 时 error_code 必须为 None，收到 {self.error_code!r}"
                )
            if self.error_message is not None:
                raise NormalizationContractError(
                    f"SlotNormalizationOutcome(success=True) 时 error_message 必须为 None，收到 {self.error_message!r}"
                )
        else:
            if self.normalized_value is not None:
                raise NormalizationContractError(
                    f"SlotNormalizationOutcome(success=False) 时 normalized_value 必须为 None，收到 {self.normalized_value!r}"
                )
            if (
                not isinstance(self.error_code, str)
                or not self.error_code.strip()
            ):
                raise NormalizationContractError(
                    f"SlotNormalizationOutcome(success=False) 时 error_code 必须为非空 str，收到 {self.error_code!r}"
                )
            if (
                not isinstance(self.error_message, str)
                or not self.error_message.strip()
            ):
                raise NormalizationContractError(
                    f"SlotNormalizationOutcome(success=False) 时 error_message 必须为非空 str，收到 {self.error_message!r}"
                )


@dataclass(frozen=True)
class NormalizedTaskPatch:
    schema_version: int
    slot_outcomes: tuple[SlotNormalizationOutcome, ...]
    passthrough_slot_updates: tuple[SlotPatch, ...]
    list_mutations: tuple[ListMutationPatch, ...]
    unresolved: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.schema_version, int) or self.schema_version != 1:
            raise NormalizationContractError(
                f"NormalizedTaskPatch schema_version 目前仅支持 1，收到 {self.schema_version!r}"
            )

        if not isinstance(self.slot_outcomes, tuple):
            raise NormalizationContractError(
                f"NormalizedTaskPatch slot_outcomes 必须为 tuple，收到 {type(self.slot_outcomes)}"
            )
        outcome_keys: set[str] = set()
        for o in self.slot_outcomes:
            if not isinstance(o, SlotNormalizationOutcome):
                raise NormalizationContractError(
                    f"NormalizedTaskPatch slot_outcomes 元素必须为 SlotNormalizationOutcome，收到 {type(o)}"
                )
            if o.key in outcome_keys:
                raise NormalizationContractError(
                    f"NormalizedTaskPatch slot_outcomes 包含重复 key: '{o.key}'"
                )
            outcome_keys.add(o.key)

        if not isinstance(self.passthrough_slot_updates, tuple):
            raise NormalizationContractError(
                f"NormalizedTaskPatch passthrough_slot_updates 必须为 tuple，收到 {type(self.passthrough_slot_updates)}"
            )
        passthrough_keys: set[str] = set()
        for p in self.passthrough_slot_updates:
            if not isinstance(p, SlotPatch):
                raise NormalizationContractError(
                    f"NormalizedTaskPatch passthrough_slot_updates 元素必须为 SlotPatch，收到 {type(p)}"
                )
            if p.key in passthrough_keys:
                raise NormalizationContractError(
                    f"NormalizedTaskPatch passthrough_slot_updates 包含重复 key: '{p.key}'"
                )
            passthrough_keys.add(p.key)

        overlap = outcome_keys & passthrough_keys
        if overlap:
            raise NormalizationContractError(
                f"NormalizedTaskPatch 槽位 key 不得同时存在于 slot_outcomes 与 passthrough_slot_updates 中: {sorted(overlap)}"
            )

        if not isinstance(self.list_mutations, tuple):
            raise NormalizationContractError(
                f"NormalizedTaskPatch list_mutations 必须为 tuple，收到 {type(self.list_mutations)}"
            )
        for m in self.list_mutations:
            if not isinstance(m, ListMutationPatch):
                raise NormalizationContractError(
                    f"NormalizedTaskPatch list_mutations 元素必须为 ListMutationPatch，收到 {type(m)}"
                )

        if not isinstance(self.unresolved, tuple):
            raise NormalizationContractError(
                f"NormalizedTaskPatch unresolved 必须为 tuple，收到 {type(self.unresolved)}"
            )
        for u in self.unresolved:
            if not isinstance(u, str) or not u.strip():
                raise NormalizationContractError(
                    f"NormalizedTaskPatch unresolved 元素必须为非空 str，收到 {u!r}"
                )


def normalize_task_patch(
    patch: TaskPatch,
    field_definitions: list[dict[str, Any]],
    current_state: dict[str, Any],
    allowed_values_resolver: Callable[
        [dict[str, Any], dict[str, Any]],
        list[Any] | None,
    ],
    *,
    passthrough_keys: set[str] | frozenset[str],
) -> NormalizedTaskPatch:
    """根据 schema field_definitions 规范化 TaskPatch 中的候选槽位，生成纯数据 NormalizedTaskPatch。"""
    if not isinstance(patch, TaskPatch):
        raise NormalizationContractError(
            f"patch 参数必须为 TaskPatch，收到 {type(patch)}"
        )

    seen_patch_keys: set[str] = set()
    slot_patch_by_key: dict[str, SlotPatch] = {}
    for sp in patch.slot_updates:
        if sp.key in seen_patch_keys:
            raise NormalizationContractError(
                f"TaskPatch slot_updates 包含重复 slot key: '{sp.key}'"
            )
        seen_patch_keys.add(sp.key)
        slot_patch_by_key[sp.key] = sp

    if not isinstance(field_definitions, list):
        raise NormalizationContractError(
            f"field_definitions 必须为 list，收到 {type(field_definitions)}"
        )

    seen_schema_keys: set[str] = set()
    schema_field_map: dict[str, dict[str, Any]] = {}
    for fdef in field_definitions:
        if not isinstance(fdef, dict):
            raise NormalizationContractError(
                f"field_definitions 元素必须为 dict，收到 {type(fdef)}"
            )
        key = fdef.get("key")
        if not isinstance(key, str) or not key.strip():
            raise NormalizationContractError(
                f"field_definition key 必须为非空 str，收到 {key!r}"
            )
        ftype = fdef.get("type")
        if not isinstance(ftype, str) or not ftype.strip():
            raise NormalizationContractError(
                f"field_definition('{key}') type 必须为非空 str，收到 {ftype!r}"
            )
        if key in seen_schema_keys:
            raise NormalizationContractError(
                f"field_definitions 中包含重复 key: '{key}'"
            )
        seen_schema_keys.add(key)
        schema_field_map[key] = fdef

    if not isinstance(current_state, dict):
        raise NormalizationContractError(
            f"current_state 必须为 dict，收到 {type(current_state)}"
        )
    temp_state = dict(current_state)

    if not isinstance(passthrough_keys, (set, frozenset)):
        raise NormalizationContractError(
            f"passthrough_keys 必须为 set 或 frozenset，收到 {type(passthrough_keys)}"
        )

    for key in slot_patch_by_key:
        if key not in schema_field_map and key not in passthrough_keys:
            raise NormalizationContractError(
                f"SlotPatch key '{key}' 既不在 schema field_definitions 中，也不在 passthrough_keys 白名单中"
            )

    normalizer = FieldNormalizer()
    outcomes: list[SlotNormalizationOutcome] = []

    # 规范化核心语义：按 field_definitions 的定义顺序进行遍历，以保持 temp_state 的链式依赖
    for fdef in field_definitions:
        key = fdef["key"]
        if key in passthrough_keys:
            continue
        if key not in slot_patch_by_key:
            continue

        slot_patch = slot_patch_by_key[key]
        field_type = fdef.get("type", "string")

        if field_type in ("auto", "fixed"):
            raise NormalizationContractError(
                f"字段 '{key}' 的 type '{field_type}' 不允许通过 TaskPatch 进行规范化更新"
            )

        try:
            allowed = allowed_values_resolver(fdef, temp_state)
        except Exception as exc:
            raise NormalizationContractError(
                f"allowed_values_resolver failed for field {key!r}"
            ) from exc

        normalized = normalizer.normalize(
            slot_patch.candidate_value,
            allowed,
            field_type,
        )

        if normalized is not None:
            temp_state[key] = normalized
            outcomes.append(
                SlotNormalizationOutcome(
                    key=key,
                    success=True,
                    normalized_value=normalized,
                    candidate_value=slot_patch.candidate_value,
                    raw_value=slot_patch.raw_value,
                    confidence=slot_patch.confidence,
                    source=slot_patch.source,
                    resolution_method=slot_patch.resolution_method,
                    error_code=None,
                    error_message=None,
                )
            )
        else:
            cand = slot_patch.candidate_value
            if cand is None or cand == "":
                error_code = "empty_value"
            elif field_type == "number":
                error_code = "invalid_number"
            elif field_type == "datetime":
                error_code = "invalid_datetime"
            elif field_type == "coord":
                error_code = "invalid_coord"
            elif field_type in ("string", "tasktype") and allowed:
                error_code = "invalid_enum"
            elif field_type == "list":
                error_code = "invalid_list"
            elif field_type not in (
                "number",
                "coord",
                "datetime",
                "list",
                "string",
                "tasktype",
                "raw",
            ):
                error_code = "unsupported_field_type"
            else:
                error_code = "normalization_failed"

            error_msg = f"无法将 '{cand}' 规范化为合法的 {field_type} 类型"

            outcomes.append(
                SlotNormalizationOutcome(
                    key=key,
                    success=False,
                    normalized_value=None,
                    candidate_value=slot_patch.candidate_value,
                    raw_value=slot_patch.raw_value,
                    confidence=slot_patch.confidence,
                    source=slot_patch.source,
                    resolution_method=slot_patch.resolution_method,
                    error_code=error_code,
                    error_message=error_msg,
                )
            )

    passthrough_updates: list[SlotPatch] = [
        sp for sp in patch.slot_updates if sp.key in passthrough_keys
    ]

    return NormalizedTaskPatch(
        schema_version=1,
        slot_outcomes=tuple(outcomes),
        passthrough_slot_updates=tuple(passthrough_updates),
        list_mutations=patch.list_mutations,
        unresolved=patch.unresolved,
    )


@dataclass(frozen=True)
class NormalizedSlotApply:
    key: str
    value: Any
    raw_value: Any
    confidence: float
    source: str

    def __post_init__(self) -> None:
        if not isinstance(self.key, str) or not self.key.strip():
            raise NormalizationContractError(
                f"NormalizedSlotApply key 必须为非空 str，收到 {self.key!r}"
            )
        validate_confidence(
            self.confidence,
            field_name=f"NormalizedSlotApply({self.key}).confidence",
        )
        if not isinstance(self.source, str) or not self.source.strip():
            raise NormalizationContractError(
                f"NormalizedSlotApply source 必须为非空 str，收到 {self.source!r}"
            )


@dataclass(frozen=True)
class NormalizationApplyPlan:
    successful_updates: tuple[NormalizedSlotApply, ...]
    failures: tuple[SlotNormalizationOutcome, ...]
    passthrough_slot_updates: tuple[SlotPatch, ...]
    list_mutations: tuple[ListMutationPatch, ...]
    unresolved: tuple[str, ...]
    normalized_schema_keys: frozenset[str]

    def __post_init__(self) -> None:
        if not isinstance(self.successful_updates, tuple):
            raise NormalizationContractError(
                f"NormalizationApplyPlan successful_updates 必须为 tuple，收到 {type(self.successful_updates)}"
            )
        for s in self.successful_updates:
            if not isinstance(s, NormalizedSlotApply):
                raise NormalizationContractError(
                    f"NormalizationApplyPlan successful_updates 元素必须为 NormalizedSlotApply，收到 {type(s)}"
                )

        if not isinstance(self.failures, tuple):
            raise NormalizationContractError(
                f"NormalizationApplyPlan failures 必须为 tuple，收到 {type(self.failures)}"
            )
        for f in self.failures:
            if not isinstance(f, SlotNormalizationOutcome):
                raise NormalizationContractError(
                    f"NormalizationApplyPlan failures 元素必须为 SlotNormalizationOutcome，收到 {type(f)}"
                )

        if not isinstance(self.passthrough_slot_updates, tuple):
            raise NormalizationContractError(
                f"NormalizationApplyPlan passthrough_slot_updates 必须为 tuple，收到 {type(self.passthrough_slot_updates)}"
            )
        for p in self.passthrough_slot_updates:
            if not isinstance(p, SlotPatch):
                raise NormalizationContractError(
                    f"NormalizationApplyPlan passthrough_slot_updates 元素必须为 SlotPatch，收到 {type(p)}"
                )

        if not isinstance(self.list_mutations, tuple):
            raise NormalizationContractError(
                f"NormalizationApplyPlan list_mutations 必须为 tuple，收到 {type(self.list_mutations)}"
            )
        for m in self.list_mutations:
            if not isinstance(m, ListMutationPatch):
                raise NormalizationContractError(
                    f"NormalizationApplyPlan list_mutations 元素必须为 ListMutationPatch，收到 {type(m)}"
                )

        if not isinstance(self.unresolved, tuple):
            raise NormalizationContractError(
                f"NormalizationApplyPlan unresolved 必须为 tuple，收到 {type(self.unresolved)}"
            )
        for u in self.unresolved:
            if not isinstance(u, str) or not u.strip():
                raise NormalizationContractError(
                    f"NormalizationApplyPlan unresolved 元素必须为非空 str，收到 {u!r}"
                )

        if not isinstance(self.normalized_schema_keys, (set, frozenset)):
            raise NormalizationContractError(
                f"NormalizationApplyPlan normalized_schema_keys 必须为 frozenset 或 set，收到 {type(self.normalized_schema_keys)}"
            )


def normalized_task_patch_to_apply_plan(
    patch: NormalizedTaskPatch,
) -> NormalizationApplyPlan:
    """把 NormalizedTaskPatch 转换为无副作用 Runtime NormalizationApplyPlan。"""
    if not isinstance(patch, NormalizedTaskPatch):
        raise NormalizationContractError(
            f"patch 参数必须为 NormalizedTaskPatch，收到 {type(patch)}"
        )

    successful_applies: list[NormalizedSlotApply] = []
    failures: list[SlotNormalizationOutcome] = []
    normalized_keys: set[str] = set()

    for outcome in patch.slot_outcomes:
        normalized_keys.add(outcome.key)
        if outcome.success:
            successful_applies.append(
                NormalizedSlotApply(
                    key=outcome.key,
                    value=outcome.normalized_value,
                    raw_value=outcome.raw_value,
                    confidence=outcome.confidence,
                    source=outcome.source,
                )
            )
        else:
            failures.append(outcome)

    return NormalizationApplyPlan(
        successful_updates=tuple(successful_applies),
        failures=tuple(failures),
        passthrough_slot_updates=patch.passthrough_slot_updates,
        list_mutations=patch.list_mutations,
        unresolved=patch.unresolved,
        normalized_schema_keys=frozenset(normalized_keys),
    )
