"""Scope explicit robot choices before model candidates can change the fleet."""
from __future__ import annotations

import re

from src.extraction.assertion_scope import task_edits_are_paused
from src.knowledge.models import _norm


EQUIPMENT_KEYS = frozenset({
    "equipment_class", "equipment_family", "equipment_type", "equipment_unit_id",
    "equipment_name", "equipment_model", "rov_description",
})
_LABEL = re.compile(r"机器人|设备(?:编号|型号|类型|名称)?|机型|ROV|AUV", re.IGNORECASE)
_SELECT = re.compile(
    r"改为|改成|改用|换成|换为|选用|选择|使用|采用|配备|配置|安排|派遣|派出|"
    r"指派|指定|部署|调派|就用|要用|选|用"
)
_FIELD_ASSIGNMENT = re.compile(
    r"\s*(?:(?:请|把|将|现在|当前|本次)\s*)*"
    r"(?:(?:机器人|设备)(?:的)?(?:系列|型号|类型|编号|名称)|机型)\s*"
    r"(?:为|是|[:：=])\s*(?P<selector>.+?)\s*",
)
_CONDITION = re.compile(r"如果|假如|假设|倘若|要是|除非|若|只要|只有|仅当|当.{0,8}时")
_HISTORY = re.compile(r"昨天|此前|之前|已完成|完成了|曾经|过去|历史")
_NEGATION = re.compile(
    r"不要|别|取消|撤销|暂不|先不|不想|不改|不换|不使用|不用|不选|不采用|"
    r"不是|并非|拒绝|不能|不可|不许|不必|无需|禁止|请勿|勿"
)
_QUESTION = re.compile(
    r"[?？]|(?:吗|呢)\s*$|是否|能否|可否|行不行|怎么样|如何|怎么|哪个|哪种|哪些|"
    r"什么风险|什么区别|或者|还是"
)
_FOLLOWUP = re.compile(
    r"^\s*(?:这样|这个(?:选择|设置)?|这么)?\s*"
    r"(?:可以|行吗|合适|安全|是否|能否|可否|会怎样|会怎么样|怎么样|有何|有什么|好不好|对吗)"
)
_GENERIC_CANCEL = re.compile(
    r"\s*(?:等等\s*)?(?:先|暂时)?(?:不要|别|取消|撤销|暂不|先不)"
    r"(?:修改|改动|改|执行|应用|这个修改|该修改)\s*"
)
_OTHER_FIELD = re.compile(r"水深|起点|终点|坐标|经纬度|支持船|支援船|管缆类型|电缆类型|井口|时间|载荷|工具")


def _catalog_mentions(text, aliases):
    """Use the catalog's longest-match boundaries, including ambiguous aliases."""
    normalized = _norm(text)
    spans = []
    units = set()
    for alias, targets in aliases:
        for match in re.finditer(re.escape(alias), normalized):
            start, end = match.span()
            if any(left <= start and end <= right for left, right in spans):
                continue
            if alias.isascii() and (
                (start and re.match(r"[a-z0-9_-]", normalized[start - 1]))
                or (end < len(normalized) and re.match(r"[a-z0-9_-]", normalized[end]))
            ):
                continue
            spans.append((start, end))
            units.update(targets)
    return units


def _catalog_aliases(kb, kind=None):
    return sorted(
        (
            (_norm(alias), {target for target in targets if not kind or target.startswith(kind + ":")})
            for alias, targets in kb.get_device_alias_index().items()
            if len(_norm(alias)) >= 2
            and (not kind or any(target.startswith(kind + ":") for target in targets))
        ),
        key=lambda item: len(item[0]), reverse=True,
    )


def _equipment_pause(clause):
    label = r"(?:机器人|设备|机型|ROV|AUV)(?:选择|选型|编号|型号|类型|名称)?"
    edit = r"(?:修改|更改|改动|调整|变更|更新|改|换)"
    return bool(re.fullmatch(
        rf"\s*(?:请\s*)?(?:"
        rf"(?:不要|别|请勿|勿|暂不|先不|不)\s*{edit}\s*{label}"
        rf"|{label}\s*(?:(?:保持|维持)?不变|(?:不要|别|不)\s*{edit})"
        rf"|(?:保持|保留|维持)\s*(?:当前|原有|原来的)?{label}(?:不变)?"
        rf"|(?:取消|撤销)\s*(?:本轮|这次|刚才的|当前)?{label}(?:的)?(?:{edit}|设置|选择)?"
        rf"|{label}(?:的)?{edit}\s*(?:取消|撤销)"
        rf")\s*(?:了|吧)?\s*", clause, re.IGNORECASE,
    ))


def _supported_candidates(candidates, selector_text, kb):
    """An affirmative model/family choice cannot reauthorize a rejected unit."""
    aliases = _catalog_aliases(kb)
    supported = _catalog_mentions(selector_text, aliases)
    for robot in kb.get_all_rovs():
        if f"variant:{robot['variant_id']}" in supported:
            supported.update({f"family:{robot['family_id']}", f"class:{robot['robot_class']}"})
    positive = _norm(selector_text)
    result = []
    for candidate in candidates:
        if candidate.get("canonical_key") not in EQUIPMENT_KEYS:
            result.append(candidate)
            continue
        value = _norm(candidate.get("normalized_value"))
        targets = {target for alias, matches in aliases if alias == value for target in matches}
        if (targets & supported or (
            not targets and len(value) >= 3 and value in positive
        )):
            result.append(candidate)
    return result


def _selection_followup_question(clause, selections, aliases):
    """Bind approval questions that repeat the selected device or its label."""
    if not _QUESTION.search(clause):
        return False
    question = re.sub(
        r"^\s*(?:(?:这个|这台|该)\s*)?"
        r"(?:(?:机器人|设备)(?:的)?(?:系列|型号|类型|编号|名称|选择)?|机型|型号)\s*",
        "", clause,
    )
    if _FOLLOWUP.search(question):
        return True
    selected = _catalog_mentions("，".join(selections), aliases)
    normalized = _norm(clause)
    return any(
        targets & selected and normalized.startswith(alias)
        and _FOLLOWUP.search(normalized[len(alias):])
        for alias, targets in aliases
    )


def _equipment_evidence(message, kb):
    """Apply the same assertion scope to unit, model and family aliases."""
    aliases = _catalog_aliases(kb)

    def mentions(clause):
        return bool(_LABEL.search(clause) or _catalog_mentions(clause, aliases))

    selected = []
    protected = task_edits_are_paused(message)
    paused = protected or any(
        _equipment_pause(clause) for clause in re.split(r"[，,。；;！!？?\n]", message)
    )
    ambiguous = False
    for sentence_match in re.finditer(r"([^。；;！!？?\n]+)([。；;！!？?\n]|$)", message):
        sentence = sentence_match[1]
        clauses = re.split(r"[，,]", sentence)
        if sentence_match[2] in {"？", "?"}:
            clauses[-1] += sentence_match[2]
        condition = _CONDITION.search(sentence)
        trailing_condition = bool(condition and not re.search(r"[，,]\s*\S", sentence[condition.start():]))
        historical = False
        conditional = False
        sentence_selected = []
        last_device = False
        for clause in clauses:
            relevant = mentions(clause)
            if _GENERIC_CANCEL.fullmatch(clause) and last_device:
                paused = True
            if (last_device and _selection_followup_question(clause, sentence_selected, aliases)
                    and (relevant or not _OTHER_FIELD.search(clause))):
                protected = True
                sentence_selected.clear()
            if _CONDITION.search(clause):
                conditional = True
                if trailing_condition and last_device:
                    protected = True
                    sentence_selected.clear()
            if _HISTORY.search(clause):
                historical = True
            elif re.match(r"\s*(?:现在|目前|当前|本次)", clause):
                historical = False
            if not relevant:
                if _OTHER_FIELD.search(clause):
                    last_device = False
                continue
            last_device = True
            if re.search(r"取消|撤销", clause) and (
                _catalog_mentions(clause, aliases) or not _OTHER_FIELD.search(clause)
            ):
                paused = True
            if re.search(r"或者|还是", clause):
                ambiguous = True
            if (conditional or historical or _NEGATION.search(clause)
                    or _QUESTION.search(clause)
                    or re.search(r"只(?:是|想)?(?:讨论|咨询|了解)|保持|不变|保留|未确定|不确定", clause)):
                protected = True
                continue
            # Only the selected side of "从 A 改为 B" can authorize B.
            assignment = _FIELD_ASSIGNMENT.fullmatch(clause)
            match = _SELECT.search(clause)
            if assignment:
                sentence_selected.append(assignment["selector"])
            elif match:
                sentence_selected.append(clause[match.end():].strip())
            else:
                bare = clause.strip()
                if any(_norm(bare) == alias for alias, _ in aliases):
                    sentence_selected.append(bare)
        selected.extend(sentence_selected)
    return selected, protected, paused, ambiguous


def _selection_lineage(targets, kb):
    """Project only from a selected child to its uniquely known ancestors."""
    lineage = set(targets)
    for target in list(lineage):
        if target.startswith("unit:"):
            unit = kb._resolve_robot_unit_exact(target.split(":", 1)[1], None)
            if unit:
                lineage.add("variant:" + unit["robot"]["variant_id"])
    for robot in kb.get_all_rovs():
        if "variant:" + robot["variant_id"] in lineage:
            lineage.update({"family:" + robot["family_id"], "class:" + robot["robot_class"]})
    for target in list(lineage):
        if target.startswith("family:"):
            family = kb.resolve_robot_family(target.split(":", 1)[1], None)
            if family:
                lineage.add("class:" + family["robot_class"])
    return lineage


def conflicts_with_current_equipment_selection(message, selected, kb):
    """A prior recommendation cannot replace a different current exact choice."""
    selections, _, paused, ambiguous = _equipment_evidence(message, kb)
    if paused or ambiguous or not selections:
        return False
    aliases = _catalog_aliases(kb)
    current = _catalog_mentions("，".join(selections), aliases)
    offered = {target for alias, targets in aliases if alias == _norm(selected) for target in targets}
    if not current or not offered:
        return False
    levels = {"class": 0, "family": 1, "variant": 2, "unit": 3}
    level = min(max(levels[target.split(":", 1)[0]] for target in current),
                max(levels[target.split(":", 1)[0]] for target in offered))
    current = {target for target in _selection_lineage(current, kb)
               if levels[target.split(":", 1)[0]] == level}
    offered = {target for target in _selection_lineage(offered, kb)
               if levels[target.split(":", 1)[0]] == level}
    return bool(current and offered and current.isdisjoint(offered))


def _ground_catalog_parent(candidates, extraction, selector_text, kb):
    targets = _catalog_mentions(selector_text, _catalog_aliases(kb))
    if any(target.startswith("unit:") for target in targets):
        return candidates
    variants = {target.split(":", 1)[1] for target in targets if target.startswith("variant:")}
    families = {target.split(":", 1)[1] for target in targets if target.startswith("family:")}
    classes = {target.split(":", 1)[1] for target in targets if target.startswith("class:")}
    if len(variants) > 1 or len(families) > 1:
        extraction.setdefault("unresolved", []).append("本轮设备包含多个备选或不同选择，请确认一个设备。")
        return [item for item in candidates if item.get("canonical_key") not in EQUIPMENT_KEYS]
    updates = {}
    if len(variants) == 1:
        robot = next((robot for robot in kb.get_all_rovs() if robot["variant_id"] in variants), None)
        if not robot or (families and families != {robot["family_id"]}) or (
            classes and classes != {robot["robot_class"]}
        ):
            return candidates
        updates = {"equipment_family": robot["family_full_name"], "equipment_type": robot["full_name"]}
    elif len(families) == 1:
        family = kb.resolve_robot_family(next(iter(families)), None)
        if not family or (classes and classes != {family["robot_class"]}):
            return candidates
        updates = {"equipment_family": family["full_name"]}
    if not updates:
        return candidates
    candidates = [item for item in candidates if item.get("canonical_key") not in EQUIPMENT_KEYS]
    candidates.extend({
        "raw_key": key, "canonical_key": key, "raw_value": selector_text,
        "normalized_value": value, "confidence": 1.0, "resolution_method": "explicit_text_exact",
    } for key, value in updates.items())
    return candidates


def ground_equipment_values(candidates, extraction, message, kb):
    """Ground catalog choices without bypassing ordinary task compatibility."""
    selected, protected, paused, ambiguous = _equipment_evidence(message, kb)
    aliases = _catalog_aliases(kb, "unit")

    selector_text = "，".join(selected)
    # Generic suffix aliases remain on the normal clarification path. Multiple
    # independent exact units, or explicit alternatives, cannot authorize one.
    concrete_aliases = [(alias, targets) for alias, targets in aliases if len(targets) == 1]
    concrete_units = _catalog_mentions(selector_text, concrete_aliases)
    # Catalog normalization drops spaces; keep them as boundaries when two
    # complete ASCII identifiers are listed next to one another.
    for token in selector_text.split():
        concrete_units.update(_catalog_mentions(token, concrete_aliases))
    ambiguous |= len(concrete_units) > 1
    if ambiguous:
        extraction.setdefault("unresolved", []).append("本轮设备包含多个备选或不同选择，请确认一个设备。")
    if paused or ambiguous:
        return [item for item in candidates if item.get("canonical_key") not in EQUIPMENT_KEYS]

    unit = kb.resolve_robot_unit_from_text(selector_text, None) if selector_text else None
    if unit:
        compact = re.sub(r"\s+", "", selector_text).casefold()
        variants = {
            robot["variant_id"] for robot in kb.get_all_rovs()
            if any(re.sub(r"\s+", "", str(alias)).casefold() in compact
                   for alias in [robot["full_name"], robot["variant_id"], *robot.get("aliases", [])]
                   if alias and len(str(alias)) >= 3)
        }
        # A conflicting explicit variant belongs in the ordinary cascade.
        if not variants or variants == {unit["robot"]["variant_id"]}:
            candidates = [item for item in candidates if item.get("canonical_key") not in EQUIPMENT_KEYS]
            candidates.append({
                "raw_key": "equipment_unit_id", "canonical_key": "equipment_unit_id",
                "raw_value": selector_text, "normalized_value": unit["unit_id"],
                "confidence": 1.0, "resolution_method": "explicit_text_exact",
            })
            return candidates
    if protected:
        candidates = _supported_candidates(candidates, selector_text, kb)
    return _ground_catalog_parent(candidates, extraction, selector_text, kb)
