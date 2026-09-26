"""Ground explicit WRITE values before any normalization or apply-plan is built.

Only task-schema coordinates and exact configured device or vessel selectors are handled.
All resulting candidates still pass the ordinary transaction and constraints.
"""
from __future__ import annotations

import re
from src.extraction.coord_parser import parse_coordinate_updates
from ..knowledge_retriever import RobotSelectionDataError

EQUIPMENT_KEYS = frozenset({'equipment_class', 'equipment_family', 'equipment_type',
                            'equipment_unit_id', 'equipment_name', 'equipment_model', 'rov_description'})
_SELECT = re.compile(r'改为|改成|改用|换成|换为|选用|选择|使用|采用|配备|安排|派遣|派出|指派')
_NON_ASSERTION = re.compile(r'不要|不想|不改|不换|不使用|不用|别|如果|假如|是否|能否|可以吗|怎么样|不变|保持|保留')


def _candidate(key, value, raw):
    return {'canonical_key': key, 'raw_key': key, 'raw_value': raw,
            'normalized_value': value, 'confidence': 1.0,
            'resolution_method': 'explicit_text_exact'}


def ground_explicit_values(extraction, message, *, kb, fields, current_state, task_type):
    """Replace model guesses only when this WRITE contains an explicit value."""
    coord_fields = {f['key'] for f in fields if f.get('type') == 'coord'}
    # Do not seed with model proposals: even a range-valid guess may swap axes.
    coords = parse_coordinate_updates(message, coord_fields, current_state=current_state)
    candidates = extraction.get('slot_candidates', [])
    if coords:
        candidates = [c for c in candidates if c.get('canonical_key') not in coords]
        candidates.extend(_candidate(k, v, message) for k, v in coords.items())

    # A simultaneous list mutation must not erase a plainly stated support ship.
    # Accept only a complete affirmative value from this task's configured domain;
    # questions, hypothetical choices and conflicting ship selections stay with
    # the normal model/clarification path.
    vessel_field = next((field for field in fields if field.get('key') == 'support_vessel'), None)
    if vessel_field:
        allowed = vessel_field.get('allowed_values') or []
        known = {re.sub(r'\s+', '', str(value)).casefold(): value for value in allowed}
        selections = []
        for sentence in re.split(r'[。；;！!\n]', message):
            # Commas do not end a condition or revoke its scope. Keep separate
            # sentences independent so an unrelated condition cannot veto a
            # later affirmative ship selection.
            if re.search(r'如果|假如|假设|倘若|要是|除非|若是', sentence):
                continue
            clauses = re.split(r'[，,]', sentence)
            cancelled = any(
                re.search(r'不要|别|取消|撤销|暂不|先不|不想', clause)
                and (
                    re.search(r'支持船|船只', clause)
                    or re.match(r'\s*(?:先|暂时)?(?:不要|别|取消|撤销|暂不|先不|不想)(?:执行|应用|确认|修改|更改|改动|改|这个|该|上述|刚才)', clause)
                )
                for clause in clauses
            )
            if cancelled:
                continue
            for clause in clauses:
                if _NON_ASSERTION.search(clause):
                    continue
                match = re.fullmatch(
                    r'\s*(?:支持船(?:编号)?|船只)\s*(?:改成|改为|改用|换成|使用|选择|选用|用|为|是|[：:])?\s*(.+?)\s*',
                    clause,
                )
                if match:
                    value = known.get(re.sub(r'\s+', '', match.group(1)).casefold())
                    if value is not None:
                        selections.append((value, clause.strip()))
        if len({value for value, _ in selections}) == 1:
            value, raw = selections[-1]
            candidates = [candidate for candidate in candidates if candidate.get('canonical_key') != 'support_vessel']
            candidates.append(_candidate('support_vessel', value, raw))

    # Restrict matching to the selected side of "从 A 改为 B". Merely mentioning
    # an old device, a negated choice or a hypothetical must never select it.
    selected = []
    for clause in re.split(r'[，,。；;！!？?\n]', message):
        if _NON_ASSERTION.search(clause):
            continue
        match = _SELECT.search(clause)
        if match:
            selected.append(clause[match.end():].strip())
    if not selected and not _NON_ASSERTION.search(message):
        bare_selector = message.strip().rstrip('。.!！')
        try:
            if kb._resolve_robot_unit_exact(bare_selector, None):
                selected.append(bare_selector)
        except RobotSelectionDataError:
            pass  # Ambiguous selectors remain on the normal clarification path.
    selector_text = '，'.join(selected)
    # Entity evidence must include incompatible units: task compatibility is
    # checked after grounding, so it cannot turn an explicit choice into another robot.
    unit = kb.resolve_robot_unit_from_text(selector_text, None) if selector_text else None
    if unit:
        # A separately stated incompatible variant is a conflict, not permission
        # to silently discard one of the user's choices.
        compact = re.sub(r'\s+', '', selector_text).casefold()
        variants = {robot['variant_id'] for robot in kb.get_all_rovs()
                    if any(re.sub(r'\s+', '', str(alias)).casefold() in compact
                           for alias in [robot['full_name'], robot['variant_id'], *robot.get('aliases', [])]
                           if alias and len(str(alias)) >= 3)}
        if not variants or variants == {unit['robot']['variant_id']}:
            candidates = [c for c in candidates if c.get('canonical_key') not in EQUIPMENT_KEYS]
            # One authoritative leaf selector lets the existing cascade derive
            # family and variant in the post-update evaluation context.
            candidates.append(_candidate('equipment_unit_id', unit['unit_id'], selector_text))
    extraction['slot_candidates'] = candidates
    return extraction
