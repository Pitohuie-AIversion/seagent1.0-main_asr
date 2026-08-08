"""task_patch.py — Extractor Result -> TaskPatch -> Legacy Adapter 中间协议层

定义 TaskPatch、SlotPatch、ListMutationPatch 不可变数据结构，
提供严格校验、Builder 与 Legacy Adapter。
解耦 Task Candidate 表示与 Downstream Normalizer/SlotStore/Transaction。
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Literal


class TaskPatchError(ValueError):
    """TaskPatch 基础异常。"""
    pass


class TaskPatchValidationError(TaskPatchError):
    """TaskPatch 结构或数据校验不合法异常。"""
    pass


class TaskPatchAdapterError(TaskPatchError):
    """TaskPatch Adapter 转换异常。"""
    pass


def _source_for_resolution_method(resolution_method: str | None) -> str:
    """内部辅助：对齐 DialogueManager 既有 source 映射语义。"""
    source_map = {
        "canonical_exact": "user_input",
        "alias_exact": "alias_mapping",
        "llm_semantic": "llm_semantic_match",
        "type_normalization": "user_input",
    }
    return source_map.get(resolution_method, "user_input")


def validate_confidence(confidence: Any, field_name: str = "confidence") -> float:
    """严格校验 confidence：数值、有限、0.0~1.0 范围，拒绝 bool/NaN/Inf。"""
    if isinstance(confidence, bool):
        raise TaskPatchValidationError(
            f"'{field_name}' 不得为 bool 类型，收到 {confidence!r}"
        )
    if not isinstance(confidence, (int, float)):
        raise TaskPatchValidationError(
            f"'{field_name}' 必须为 int 或 float，收到 {type(confidence)}"
        )
    val = float(confidence)
    if not math.isfinite(val):
        raise TaskPatchValidationError(
            f"'{field_name}' 必须为有限数值，收到 {val}"
        )
    if not (0.0 <= val <= 1.0):
        raise TaskPatchValidationError(
            f"'{field_name}' 必须处于 [0.0, 1.0] 范围内，收到 {val}"
        )
    return val


@dataclass(frozen=True)
class SlotPatch:
    key: str
    candidate_value: Any
    raw_value: Any
    confidence: float
    source: str
    resolution_method: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.key, str) or not self.key.strip():
            raise TaskPatchValidationError(
                f"SlotPatch key 必须为非空 str，收到 {self.key!r}"
            )
        validate_confidence(self.confidence, field_name=f"SlotPatch({self.key}).confidence")
        if not isinstance(self.source, str) or not self.source.strip():
            raise TaskPatchValidationError(
                f"SlotPatch source 必须为非空 str，收到 {self.source!r}"
            )


@dataclass(frozen=True)
class ListMutationPatch:
    field: str
    operation: Literal["add", "remove", "replace", "clear"]
    items: tuple[Any, ...]
    target_items: tuple[Any, ...]
    raw_text: str
    confidence: float
    source: str

    def __post_init__(self) -> None:
        if not isinstance(self.field, str) or not self.field.strip():
            raise TaskPatchValidationError(
                f"ListMutationPatch field 必须为非空 str，收到 {self.field!r}"
            )
        valid_ops = ("add", "remove", "replace", "clear")
        if self.operation not in valid_ops:
            raise TaskPatchValidationError(
                f"ListMutationPatch operation 必须为 {valid_ops} 之一，收到 {self.operation!r}"
            )
        if not isinstance(self.items, tuple):
            raise TaskPatchValidationError(
                f"ListMutationPatch items 必须为 tuple，收到 {type(self.items)}"
            )
        if not isinstance(self.target_items, tuple):
            raise TaskPatchValidationError(
                f"ListMutationPatch target_items 必须为 tuple，收到 {type(self.target_items)}"
            )
        validate_confidence(self.confidence, field_name=f"ListMutationPatch({self.field}).confidence")
        if not isinstance(self.source, str) or not self.source.strip():
            raise TaskPatchValidationError(
                f"ListMutationPatch source 必须为非空 str，收到 {self.source!r}"
            )

        # 针对 operation 的严格结构契约校验
        if self.operation == "add":
            if len(self.items) == 0:
                raise TaskPatchValidationError("ListMutationPatch(add) items 不得为空")
            if len(self.target_items) > 0:
                raise TaskPatchValidationError("ListMutationPatch(add) target_items 必须为空")
        elif self.operation == "remove":
            if len(self.items) == 0:
                raise TaskPatchValidationError("ListMutationPatch(remove) items 不得为空")
            if len(self.target_items) > 0:
                raise TaskPatchValidationError("ListMutationPatch(remove) target_items 必须为空")
        elif self.operation == "replace":
            if len(self.items) == 0:
                raise TaskPatchValidationError("ListMutationPatch(replace) items 不得为空")
            if len(self.target_items) == 0:
                raise TaskPatchValidationError("ListMutationPatch(replace) target_items 不得为空")
        elif self.operation == "clear":
            if len(self.items) > 0:
                raise TaskPatchValidationError("ListMutationPatch(clear) items 必须为空")
            if len(self.target_items) > 0:
                raise TaskPatchValidationError("ListMutationPatch(clear) target_items 必须为空")


@dataclass(frozen=True)
class TaskPatch:
    schema_version: int
    slot_updates: tuple[SlotPatch, ...]
    list_mutations: tuple[ListMutationPatch, ...]
    unresolved: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.schema_version, int) or self.schema_version != 1:
            raise TaskPatchValidationError(
                f"TaskPatch schema_version 目前仅支持 1，收到 {self.schema_version!r}"
            )
        if not isinstance(self.slot_updates, tuple):
            raise TaskPatchValidationError(
                f"TaskPatch slot_updates 必须为 tuple，收到 {type(self.slot_updates)}"
            )
        for s in self.slot_updates:
            if not isinstance(s, SlotPatch):
                raise TaskPatchValidationError(
                    f"TaskPatch slot_updates 元素必须为 SlotPatch，收到 {type(s)}"
                )
        if not isinstance(self.list_mutations, tuple):
            raise TaskPatchValidationError(
                f"TaskPatch list_mutations 必须为 tuple，收到 {type(self.list_mutations)}"
            )
        for m in self.list_mutations:
            if not isinstance(m, ListMutationPatch):
                raise TaskPatchValidationError(
                    f"TaskPatch list_mutations 元素必须为 ListMutationPatch，收到 {type(m)}"
                )
        if not isinstance(self.unresolved, tuple):
            raise TaskPatchValidationError(
                f"TaskPatch unresolved 必须为 tuple，收到 {type(self.unresolved)}"
            )
        for u in self.unresolved:
            if not isinstance(u, str) or not u.strip():
                raise TaskPatchValidationError(
                    f"TaskPatch unresolved 元素必须为非空 str，收到 {u!r}"
                )


def build_task_patch(
    extraction_result: dict[str, Any],
    allowed_keys: set[str] | frozenset[str] | None = None,
) -> TaskPatch:
    """解析 ParameterExtractor 输出 dict 并构造成严格校验后的 TaskPatch 对象。"""
    if not isinstance(extraction_result, dict):
        raise TaskPatchValidationError(
            f"extraction_result 必须为 dict 类型，收到 {type(extraction_result)}"
        )

    raw_candidates = extraction_result.get("slot_candidates")
    if not isinstance(raw_candidates, list):
        raise TaskPatchValidationError(
            f"extraction_result['slot_candidates'] 必须为 list 类型，收到 {type(raw_candidates)}"
        )

    slot_updates_list: list[SlotPatch] = []
    for cand in raw_candidates:
        if not isinstance(cand, dict):
            raise TaskPatchValidationError(
                f"slot_candidates 元素必须为 dict 类型，收到 {type(cand)}"
            )

        canonical_key = cand.get("canonical_key")
        if not isinstance(canonical_key, str) or not canonical_key.strip():
            raise TaskPatchValidationError(
                f"candidate canonical_key 缺失或非合法字符串，收到 {canonical_key!r}"
            )

        key = canonical_key.strip()
        # 兼容 key 映射：equipment_model -> equipment_type
        if key == "equipment_model":
            key = "equipment_type"

        if allowed_keys is not None and key not in allowed_keys:
            raise TaskPatchValidationError(
                f"candidate key '{key}' 不在允许的 key 白名单中: {allowed_keys}"
            )

        cand_val = cand.get("normalized_value")
        raw_val = cand.get("raw_value", cand_val)
        confidence = validate_confidence(
            cand.get("confidence", 1.0),
            field_name=f"candidate({key}).confidence",
        )
        res_method = cand.get("resolution_method")
        source = cand.get("source") or _source_for_resolution_method(res_method)

        slot_updates_list.append(
            SlotPatch(
                key=key,
                candidate_value=cand_val,
                raw_value=raw_val,
                confidence=confidence,
                source=source,
                resolution_method=res_method,
            )
        )

    raw_mutations = extraction_result.get("list_mutations", [])
    if not isinstance(raw_mutations, list):
        raise TaskPatchValidationError(
            f"extraction_result['list_mutations'] 必须为 list 类型，收到 {type(raw_mutations)}"
        )

    list_mutations_list: list[ListMutationPatch] = []
    for mut in raw_mutations:
        if not isinstance(mut, dict):
            raise TaskPatchValidationError(
                f"list_mutations 元素必须为 dict 类型，收到 {type(mut)}"
            )
        field = mut.get("field", "payload")
        op = mut.get("operation")
        items_raw = mut.get("items", [])
        target_raw = mut.get("target_items", [])
        if not isinstance(items_raw, (list, tuple)):
            raise TaskPatchValidationError(
                f"list_mutation items 必须为 list 或 tuple，收到 {type(items_raw)}"
            )
        if not isinstance(target_raw, (list, tuple)):
            raise TaskPatchValidationError(
                f"list_mutation target_items 必须为 list 或 tuple，收到 {type(target_raw)}"
            )

        conf = validate_confidence(
            mut.get("confidence", 0.95),
            field_name=f"mutation({field}).confidence",
        )
        src = mut.get("source", "user_input")
        raw_t = str(mut.get("raw_text", ""))

        list_mutations_list.append(
            ListMutationPatch(
                field=str(field),
                operation=op,
                items=tuple(items_raw),
                target_items=tuple(target_raw),
                raw_text=raw_t,
                confidence=conf,
                source=str(src),
            )
        )

    raw_unresolved = extraction_result.get("unresolved", [])
    if not isinstance(raw_unresolved, list):
        raise TaskPatchValidationError(
            f"extraction_result['unresolved'] 必须为 list 类型，收到 {type(raw_unresolved)}"
        )

    # 保序去重并剔除空字符串
    unresolved_clean: list[str] = []
    for item in raw_unresolved:
        s_item = str(item).strip() if item is not None else ""
        if s_item and s_item not in unresolved_clean:
            unresolved_clean.append(s_item)

    return TaskPatch(
        schema_version=1,
        slot_updates=tuple(slot_updates_list),
        list_mutations=tuple(list_mutations_list),
        unresolved=tuple(unresolved_clean),
    )


def task_patch_to_legacy_updates(
    patch: TaskPatch,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]], list[str]]:
    """将 TaskPatch 纯粹无副作用转换回 DialogueManager / Legacy Transaction 所需的数据结构。

    返回:
        (stage_updates, list_mutations, unresolved)
    """
    if not isinstance(patch, TaskPatch):
        raise TaskPatchAdapterError(
            f"task_patch_to_legacy_updates 参数必须为 TaskPatch，收到 {type(patch)}"
        )

    stage_updates: dict[str, dict[str, Any]] = {}
    for slot_patch in patch.slot_updates:
        source = slot_patch.source or _source_for_resolution_method(slot_patch.resolution_method)
        stage_updates[slot_patch.key] = {
            "value": slot_patch.candidate_value,
            "raw_value": slot_patch.raw_value,
            "confidence": slot_patch.confidence,
            "source": source,
        }

    legacy_mutations: list[dict[str, Any]] = [
        {
            "field": m.field,
            "operation": m.operation,
            "items": list(m.items),
            "target_items": list(m.target_items),
            "raw_text": m.raw_text,
            "confidence": m.confidence,
            "source": m.source,
        }
        for m in patch.list_mutations
    ]

    legacy_unresolved: list[str] = list(patch.unresolved)

    return stage_updates, legacy_mutations, legacy_unresolved
