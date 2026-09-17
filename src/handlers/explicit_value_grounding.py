"""Ground explicit WRITE values before any normalization or apply-plan is built.

Only task-schema coordinates and exact configured device selectors are handled.
All resulting candidates still pass the ordinary transaction and constraints.
"""
from __future__ import annotations

import re
from ..coord_parser import parse_coordinate_updates
from ..knowledge_retriever import RobotSelectionDataError

EQUIPMENT_KEYS = frozenset({'equipment_class', 'equipment_family', 'equipment_type',
                            'equipment_unit_id', 'equipment_name'})
_SELECT = re.compile(r'改为|改成|改用|换成|换为|选用|选择|使用|采用|配备')
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
            if kb._resolve_robot_unit_exact(bare_selector, task_type):
                selected.append(bare_selector)
        except RobotSelectionDataError:
            pass  # Ambiguous selectors remain on the normal clarification path.
    selector_text = '，'.join(selected)
    unit = kb.resolve_robot_unit_from_text(selector_text, task_type) if selector_text else None
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
