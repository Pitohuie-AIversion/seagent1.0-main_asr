"""
src/handlers/grounding/receipt_builder.py - 权威客观回执构建、候选投影与回复组装器
"""

from __future__ import annotations

import re
from typing import Any

from src.constants import FIELD_LABELS
from src.extraction import coord_parser
from .field_grounder import FACT_FIELD_ALIASES, ground_blocked_publish_instructions


def filter_robot_selection_unresolved(
    unresolved_items: list,
    accepted_updates: dict | None,
) -> list:
    """清理机器人选择迁移后的 unresolved 噪声。

    equipment_class 已经是内部派生元数据，不再是 schema 采集字段；同一个
    raw phrase 如果已被 family/type/unit 成功写入，下游层级对同 raw 的失败
    fan-out 也不应继续展示给用户。
    """
    if not unresolved_items:
        return []

    accepted_raws: set[str] = set()
    for key, info in (accepted_updates or {}).items():
        if key not in {
            "equipment_family",
            "equipment_type",
            "equipment_unit_id",
            "equipment_name",
        }:
            continue
        if isinstance(info, dict):
            raw = info.get("raw") or info.get("raw_value")
            if raw:
                accepted_raws.add(str(raw).strip().lower())

    filtered = []
    for item in unresolved_items:
        key = getattr(item, "field", None) or (item.get("field") if isinstance(item, dict) else None)
        if key == "equipment_class":
            continue
        raw = getattr(item, "raw", None) or (item.get("raw") if isinstance(item, dict) else None)
        raw_str = str(raw or "").strip().lower()
        if raw_str and raw_str in accepted_raws and key in {
            "equipment_family",
            "equipment_type",
            "equipment_unit_id",
        }:
            continue
        filtered.append(item)
    return filtered


def project_legacy_equipment_class_candidate(candidate: dict, kb: Any) -> dict:
    """将旧 equipment_class 候选安全投影为 family 候选，保留原始决策元数据。"""
    if not isinstance(candidate, dict):
        return candidate
    raw_class = candidate.get("value") or candidate.get("candidate")
    classes = kb.get_robot_classes() if kb else {}
    class_info = classes.get(str(raw_class), {}) if raw_class else {}
    families = class_info.get("families", [])
    if len(families) != 1:
        return candidate

    family_key = families[0]
    family_config = kb.get_robot_families().get(family_key, {}) if kb else {}
    projected = dict(candidate)
    projected["target_field"] = "equipment_family"
    projected["candidate"] = family_key
    projected["value"] = family_key
    projected["display_name"] = family_config.get("name") or family_key
    projected["canonical_class"] = raw_class
    projected["resolution_method"] = "legacy_class_to_single_family"
    return projected


def repair_missing_field_block(
    reply: str,
    missing_fields: list[dict] | None,
    visible: dict,
    task_state: dict,
) -> str:
    """Repair broken collection lists using actual missing slots, keeping useful questions."""
    if missing_fields is None:
        return reply
    missing = {item["key"]: item for item in missing_fields
               if isinstance(item, dict) and item.get("key") and item.get("type") not in ("auto", "fixed")}
    aliases = {alias: key for key, names in FACT_FIELD_ALIASES.items() for alias in names}
    aliases.update({label: key for key, label in FIELD_LABELS.items()})
    aliases.update({str(item.get("label") or key): key for key, item in missing.items()})
    aliases.update({"载荷": "payload", "水深": "water_depth"})
    labels = "|".join(re.escape(alias) for alias in sorted(aliases, key=len, reverse=True))
    heading = re.compile(r"(?:还需|仍需|需要).{0,15}(?:以下|下列)\s*(\d+)\s*个.{0,8}(?:参数|字段|信息)")
    numbered = re.compile(r"^\s*(\d+)[.)、]\s*(.+)$")
    field = re.compile(rf"^(?:{labels})(?=\s*[：:（(]|$)")
    lines = reply.splitlines()
    result = []
    i = 0
    while i < len(lines):
        title = heading.search(re.sub(r"[*_`]", "", lines[i]))
        if not title:
            result.append(lines[i])
            i += 1
            continue
        j, entries = i + 1, []
        while j < len(lines):
            if not lines[j].strip():
                j += 1
                continue
            plain_line = re.sub(r"[*_`]", "", lines[j])
            item = numbered.match(plain_line)
            name = field.match(item[2]) if item else None
            if item and name:
                raw_item = numbered.match(lines[j])
                body = raw_item[2] if raw_item else f"**{item[2]}**"
                entries.append((int(item[1]), aliases.get(name[0]), body))
                j += 1
                continue
            boundary = re.match(
                r"\s*(?:#{1,6}\s|[|]|✅|⚠️|当前已保存|仍需补充|任务尚未发布|"
                r"请(?:补充|提供|确认)(?:以上|上述)|请回复|请逐项|请核对|您可以|随后|接下来)",
                plain_line,
            )
            if entries and not boundary and not heading.search(plain_line):
                number, key, body = entries[-1]
                entries[-1] = (number, key, body + "\n" + lines[j])
                j += 1
                continue
            break
        keys = [key for _, key, _ in entries if key]
        saved = {**task_state, **visible}
        invalid = (
            [number for number, _, _ in entries] != list(range(1, int(title[1]) + 1))
            or len(keys) != len(set(keys))
            or any(key not in missing and saved.get(key) not in (None, [], "") for key in keys)
            or not missing
        )
        if not invalid:
            result.extend(lines[i:j])
            i = j
            continue
        if missing:
            selected = list(missing.items())[:3]
            result.append(f"接下来，还需要您补充以下 {len(selected)} 个参数：")
            originals = {key: body for _, key, body in entries if key in missing}
            for number, (key, item) in enumerate(selected, 1):
                label = item.get("label") or FIELD_LABELS.get(key, key)
                body = originals.get(key, f"**{label}**：请提供{label}。")
                result.append(f"{number}. {body}")
        i = j
    return "\n".join(result)


def build_authoritative_receipt(
    *,
    accepted_updates: dict,
    unresolved_inputs: list,
    missing_fields: list[dict] | None = None,
    display_updates: dict | None = None,
    task_state: dict | None = None,
    constraint_context: dict | None = None,
    actual_model_reply: str = "",
) -> tuple[str, list[str], dict, list, bool, bool, list[str], list]:
    """构建权威客观回执文本及元数据。"""
    context = constraint_context or {}
    violations = context.get("violations") or []
    severities = {
        v.get("severity") if isinstance(v, dict) else getattr(v, "severity", None)
        for v in violations
    }
    has_hard = "hard" in severities or str(context.get("type", "")).startswith("hard")
    has_soft = "soft" in severities or str(context.get("type", "")).startswith("soft")
    visible = {key: value for key, value in accepted_updates.items() if key in FIELD_LABELS}
    resolved_labels = {FIELD_LABELS[key] for key in visible}
    unresolved = []
    for item in unresolved_inputs:
        text = str(item).strip()
        if not text:
            continue
        if any(label in text for label in resolved_labels) and (
            "无法解析" in text or "Invalid datetime format" in text
        ):
            continue
        if has_hard or has_soft:
            text = ground_blocked_publish_instructions(
                text, hard=has_hard, include_guidance=False,
            )
        if text and text not in unresolved:
            unresolved.append(text)

    parts = []
    if visible:
        committed = [
            f"{FIELD_LABELS[key]}：{'已清空' if value == [] else coord_parser.format_slot_display_value(key, (display_updates or {}).get(key, value))}"
            for key, value in visible.items()
        ]
        parts.append("✅ 已记录：" + "；".join(committed) + "。")
    else:
        parts.append("本轮未写入任务状态。")
    if unresolved:
        cleaned_unresolved = [str(u).rstrip("。；;，,\n ") for u in unresolved if u]
        parts.append("⚠️ 未写入或仍需确认：" + "；".join(cleaned_unresolved) + "。")

    retained = []
    retained_keys = ["task_type", "wellhead_id", "equipment_type", "payload", "start_time", "end_time", "support_vessel"]
    retained_keys.extend(key for key, aliases in FACT_FIELD_ALIASES.items()
                         if any(alias in str(actual_model_reply) for alias in aliases))
    if "水深" in str(actual_model_reply):
        retained_keys.append("water_depth")
    for key in dict.fromkeys(retained_keys):
        value = (task_state or {}).get(key)
        if key in visible or value is None:
            continue
        retained.append(f"{FIELD_LABELS[key]}：{coord_parser.format_slot_display_value(key, value)}")
    if retained:
        parts.append("当前已保存：" + "；".join(retained) + "。")

    for violation in violations:
        def field(name: str, default: str = "") -> str:
            return violation.get(name, default) if isinstance(violation, dict) else getattr(violation, name, default)
        severity = field("severity")
        has_hard = has_hard or severity == "hard"
        has_soft = has_soft or severity == "soft"
        message = field("message")
        code = field("constraint_id") or field("code")
        if message and severity == "hard":
            detail = f"⚠️ {code + '：' if code else ''}{message}"
            if detail not in parts:
                parts.append(detail)

    labels = [str(item.get("label") or item.get("key"))
              for item in (missing_fields or [])
              if isinstance(item, dict) and (item.get("label") or item.get("key"))]
    if labels:
        parts.append("仍需补充：" + "、".join(labels[:3]) + "。")
    if has_hard or str(context.get("type", "")).startswith("hard"):
        parts.append("任务尚未发布，请先修正上述硬约束问题。")
    elif has_soft or str(context.get("type", "")).startswith("soft"):
        parts.append("任务尚未发布，仍有软警告待处理。请查看右侧看板后修改相关参数；如接受这些软警告，可明确回复“忽略软警告”后继续。")
    elif missing_fields is not None and not labels:
        if unresolved:
            parts.append("任务尚未发布，请先处理上述未写入或待确认的信息。")
        else:
            parts.append("所有必填字段已收集完成，任务尚未发布。请核对草稿后再确认发布。")

    receipt = "\n".join(parts)
    return receipt, parts, visible, unresolved, has_hard, has_soft, labels, violations


def compose_final_reply(
    scrubbed: str,
    parts: list[str],
    receipt: str,
    *,
    blocked_hard: bool,
    blocked_soft: bool,
) -> str:
    """融合自然语言文本与权威客观回执。"""
    if blocked_hard and "请先修正上述硬约束问题，再继续校验；当前不能确认发布。" in scrubbed:
        parts = ["任务尚未发布。" if part == "任务尚未发布，请先修正上述硬约束问题。" else part
                 for part in parts]
    elif blocked_soft and "如接受这些软警告，请先明确回复“忽略软警告”，待系统重新校验后再确认发布。" in scrubbed:
        parts = ["任务尚未发布，仍有软警告待处理。请查看右侧看板。" if part.startswith("任务尚未发布，仍有软警告待处理。") else part
                 for part in parts]

    deduped_parts = [part for part in parts if part not in scrubbed]

    final_receipt = "\n".join(deduped_parts)
    if not scrubbed:
        return receipt

    if scrubbed in ("本轮未写入任务状态。", "本轮未写入任务状态"):
        return receipt

    combined = f"{scrubbed}\n{final_receipt}".strip()
    combined = re.sub(r"([。！？])\1+", r"\1", combined)
    return combined
