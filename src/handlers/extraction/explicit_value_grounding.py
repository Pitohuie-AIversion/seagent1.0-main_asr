"""Ground explicit WRITE values before any normalization or apply-plan is built.

Only explicit task-schema values and configured selectors are handled.
All resulting candidates still pass the ordinary transaction and constraints.
"""
from __future__ import annotations

import re
import unicodedata
from src.extraction.assertion_scope import task_edits_are_paused
from src.extraction.coord_parser import coordinate_update_issues, parse_coordinate_updates, protected_coordinate_fields
from src.temporal.duration_parser import parse_chinese_number
from src.temporal.relative_time_parser import DURATION_VALUE_PATTERN, RELATIVE_OFFSET_PATTERN
from ..equipment.equipment_assertion_grounding import ground_equipment_values
_CABLE_CONDITION = re.compile(r'如果|假如|假设|倘若|要是|除非|若|只要|只有|仅当|当.{0,8}时')
_CABLE_QUESTION = re.compile(
    r'[?？]|(?:吗|呢)(?:[。！!，,?？]|$)|是什么|有何|为何|为什么|如何|怎么|哪个|哪种|哪些|'
    r'能否|可否|行不行|可以吗|行吗|也行|或者|还是|怎么样|什么风险|什么区别'
)
_CABLE_NEGATION = re.compile(r'不要|别|取消|撤销|暂不|先不|不想|不改|不换|不使用|不用|不选|不采用|不是|并非|拒绝|不能|不可|不许|不必|无需|禁止|请勿|勿')
_CABLE_NON_UPDATE = re.compile(r'昨天|此前|之前|已完成|完成了|曾经|过去|历史')
_VALUE_FOLLOWUP = re.compile(r'^\s*(?:这样|这个(?:设置|取值)?|这么)?\s*(?:可以|行吗|合适|安全|是否|能否|可否|会怎样|会怎么样|有何|有什么|好不好|对不对|对吗|正确吗)')
_DEPTH_LABEL = r'(?:海水水深|作业水深|海况水深|海水深度|水深)'
_DEPTH_OTHER_FIELD = r'机器人|设备|ROV|AUV|支持船|母船|船只|井口|管缆|电缆|起点|终点|坐标|经纬度|载荷|工具|时间|开始|结束|油田|作业区域|工区'
_DEPTH_ASSIGNMENT = re.compile(
    r'(?:(?:请|帮我|把|将|目前|现在|本次|当前)\s*)*(?:(?:任务|作业)的?)?'
    + _DEPTH_LABEL + r'(?:\s*\(\s*米\s*\))?\s*'
    r'(?:(?:修改|调整|设置|设定|改|设|定)(?:为|成|到|在)?|设在|定在|按|在|为|是|[:：=])?\s*'
    r'(?:大约|大概|约为|约)?\s*'
    r'(?P<number>[-+]?(?:\d+(?:\.\d+)?|\.\d+)|[负零〇一二两三四五六七八九十百千万点]+)\s*'
    r'(?P<unit>千米|公里|km|米|公尺|m)?\s*(?:左右|上下)?', re.IGNORECASE,
)


def _field_edit_is_paused(clause, label):
    """Recognize a restriction on the whole field, not a rejected old value."""
    pause = r'(?:不要|别|不能|不可|不许|禁止|请勿|勿|暂不|先不|不)'
    edit = r'(?:修改|更改|改动|调整|变更|更新|改|动)'
    return bool(re.fullmatch(
        rf'\s*(?:请\s*)?(?:'
        rf'{pause}\s*{edit}\s*{label}'
        rf'|{label}\s*(?:(?:保持|维持)?不变|{pause}\s*{edit})'
        rf'|(?:保持|保留|维持)\s*(?:当前|原有|原来的)?{label}(?:不变)?'
        rf'|(?:取消|撤销)\s*(?:本轮|这次|刚才的)?{label}(?:的)?(?:{edit}|设置)?'
        rf'|{label}(?:的)?{edit}\s*(?:取消|撤销)'
        rf')\s*(?:了|吧)?\s*', clause,
    ))


def _depth_history_clauses(clauses):
    historical = False
    for clause in clauses:
        if _CABLE_NON_UPDATE.search(clause):
            # A bare "昨天，..." scopes the following clauses. A historical
            # statement about another named field does not scope task depth.
            historical = bool(re.search(_DEPTH_LABEL, clause)
                              or not re.search(_DEPTH_OTHER_FIELD, clause, re.IGNORECASE))
        elif re.match(r'\s*(?:现在|目前|当前|本次)', clause):
            historical = False
        yield clause, historical


def _ground_water_depth(candidates, extraction, message):
    """Recover exact depth assignments and veto explicitly non-writing evidence."""
    selections = []
    protected = task_edits_are_paused(message)
    keep_depth = any(
        _field_edit_is_paused(clause, _DEPTH_LABEL)
        for sentence in re.split(r'[。；;！!？?\n]', message)
        for clause, historical in _depth_history_clauses(re.split(r'[，,]', sentence))
        if not historical
    )
    for sentence_match in re.finditer(r'([^。；;！!？?\n]+)([。；;！!？?\n]|$)', message):
        sentence = sentence_match[1]
        if not re.search(_DEPTH_LABEL, sentence):
            continue
        condition = _CABLE_CONDITION.search(sentence)
        conditional_tail = False
        if condition:
            before_condition = sentence[:condition.start()].rstrip('，, ')
            condition_text = sentence[condition.start():]
            previous_clause = re.split(r'[，,]', before_condition)[-1]
            # A later conditional branch about another field cannot veto an
            # already complete depth assignment. A condition on depth itself,
            # or a qualifier immediately after depth, stays protected.
            if (not re.search(_DEPTH_LABEL, before_condition)
                    or re.search(_DEPTH_LABEL, condition_text)
                    or (not re.search(r'[，,]\s*\S', condition_text)
                        and re.search(_DEPTH_LABEL, previous_clause))):
                protected = True
                continue
            sentence = before_condition
            conditional_tail = True
        clauses = re.split(r'[，,]', sentence)
        if not conditional_tail and sentence_match[2] in {'？', '?'}:
            clauses[-1] += sentence_match[2]
        scoped_clauses = list(_depth_history_clauses(clauses))
        # Bind generic questions/cancellations to their preceding subject.
        # A later robot question must not erase an independent depth edit.
        cancelled = queried = False
        previous_clause = ''
        previous_historical = False
        for clause, historical in scoped_clauses:
            if clause.strip() == '等等':
                continue
            if not historical and not previous_historical and re.search(_DEPTH_LABEL, previous_clause):
                cancelled |= bool(re.fullmatch(
                    r'\s*(?:先|暂时)?(?:不要|别|取消|撤销|暂不|先不)'
                    r'(?:修改|改动|改|执行|应用|这个修改|该修改)\s*', clause,
                ))
                queried |= bool(_VALUE_FOLLOWUP.search(clause) and _CABLE_QUESTION.search(clause)
                                and not re.search(_DEPTH_OTHER_FIELD, clause, re.IGNORECASE))
            previous_clause = clause
            previous_historical = historical
        if cancelled or queried or any(_CABLE_QUESTION.search(clause)
                                      and re.search(_DEPTH_LABEL, clause)
                                      for clause, historical in scoped_clauses[1:] if not historical):
            protected = True
            continue
        for clause, historical in scoped_clauses:
            if not re.search(_DEPTH_LABEL, clause):
                continue
            if historical:
                protected = True
                continue
            if re.search(_DEPTH_LABEL + r'\s*(?:保持)?不变|' + _DEPTH_LABEL + r'\s*(?:不要|别)(?:改|动)', clause):
                keep_depth = True
            if re.search(r'取消|撤销', clause):
                keep_depth = True
            if re.search(r'或者|还是', clause):
                keep_depth = True
            if (_CABLE_NEGATION.search(clause) or _CABLE_QUESTION.search(clause)
                    or _CABLE_NON_UPDATE.search(clause)
                    or re.search(r'只(?:是|想)?(?:讨论|咨询|了解)|保持|不变|保留|不要|别|未确定|不确定', clause)):
                protected = True
                continue
            match = _DEPTH_ASSIGNMENT.fullmatch(unicodedata.normalize('NFKC', clause).strip())
            if match:
                number = match['number']
                value = parse_chinese_number(number.removeprefix('负'))
                if value is not None:
                    if number.startswith('负'):
                        value = -value
                    if (match['unit'] or '').casefold() in {'千米', '公里', 'km'}:
                        value *= 1000
                    value_end = match.end('unit') if match['unit'] else match.end('number')
                    raw_value = match.string[match.start('number'):value_end]
                    selections.append((value, raw_value))
            elif re.search(r'或者|还是|[~～—]|\d\s*[-/]\s*\d|'
                           r'[0-9零〇一二两三四五六七八九十百千万点]\s*(?:千米|公里|km|米|m)?\s*'
                           r'(?:至|到)\s*[0-9零〇一二两三四五六七八九十百千万点]', clause):
                protected = True
                keep_depth = True
                extraction.setdefault('unresolved', []).append('本轮水深包含范围或备选值，请确认一个水深。')

    distinct = {value for value, _ in selections}
    if keep_depth or task_edits_are_paused(message):
        selections, distinct, protected = [], set(), True
    if len(distinct) > 1:
        protected = True
        extraction.setdefault('unresolved', []).append('本轮水深包含多个不同取值，请确认一个水深。')
    if len(distinct) == 1 or protected:
        candidates = [item for item in candidates if item.get('canonical_key') != 'water_depth']
    if len(distinct) == 1:
        value, raw = selections[-1]
        candidates.append(_candidate('water_depth', value, raw))
    return candidates


def _ground_wellhead_id(candidates, extraction, message):
    """Recover exact wellhead_id assignments and veto non-writing evidence."""
    wellhead_label = r'(?:井口编号|井口|井号)'
    selections = []
    protected = task_edits_are_paused(message)
    keep_wellhead = any(_field_edit_is_paused(clause, wellhead_label)
                       for clause in re.split(r'[，,。；;！!？?\n]', message))

    for sentence_match in re.finditer(r'([^。；;！!？?\n]+)([。；;！!？?\n]|$)', message):
        sentence = sentence_match[1]
        if not re.search(wellhead_label, sentence):
            continue
        condition = _CABLE_CONDITION.search(sentence)
        conditional_tail = False
        if condition:
            before_condition = sentence[:condition.start()].rstrip('，, ')
            condition_text = sentence[condition.start():]
            if (not re.search(wellhead_label, before_condition)
                    or re.search(wellhead_label, condition_text)
                    or not re.search(r'[，,]\s*\S', condition_text)):
                protected = True
                continue
            sentence = before_condition
            conditional_tail = True

        clauses = re.split(r'[，,]', sentence)
        if not conditional_tail and sentence_match[2] in {'？', '?'}:
            clauses[-1] += sentence_match[2]
        cancelled = any(
            re.fullmatch(
                r'\s*(?:先|暂时)?(?:不要|别|取消|撤销|暂不|先不|不想)'
                r'(?:执行|应用|确认|修改|更改|改动|改|这个修改|该修改)\s*', clause,
            )
            for clause in clauses
        )
        if cancelled or any(_CABLE_QUESTION.search(clause) and (
            re.search(wellhead_label, clause) or _VALUE_FOLLOWUP.search(clause)
        ) for clause in clauses[1:]):
            protected = True
            continue

        for clause in clauses:
            if not re.search(wellhead_label, clause):
                continue
            if re.search(wellhead_label + r'\s*(?:保持)?不变|' + wellhead_label + r'\s*(?:不要|别)(?:改|动)', clause):
                keep_wellhead = True
            if re.search(r'取消|撤销', clause):
                keep_wellhead = True
            if re.search(r'或者|还是', clause):
                keep_wellhead = True
            if (_CABLE_NEGATION.search(clause) or _CABLE_QUESTION.search(clause)
                    or _CABLE_NON_UPDATE.search(clause) or _VALUE_FOLLOWUP.search(clause)
                    or re.search(r'只(?:是|想)?(?:讨论|咨询|了解)|保持|不变|保留|未确定|不确定', clause)):
                protected = True
                continue

            p1 = (
                rf'(?:(?:请|帮我|把|将|目标|作业|本次|目前|现在|当前)\s*)*(?:任务|作业)?(?:的)?{wellhead_label}\s*'
                rf'(?:(?:修改|调整|设置|设定|改|设|定)(?:为|成|到|在)?|设在|定在|按|在|为|是|[:：=]|选用|选择|采用|改用|就用|用)?\s*'
                rf'(?P<val>[A-Za-z0-9#\-_]+(?:\s*号(?:井口|井)?)?|[一二两三四五六七八九十\d]+号(?:井口|井)?)\s*$'
            )
            p2 = (
                rf'(?:(?:请|帮我|把|将|目标|作业|本次)\s*)?(?:任务|作业)?(?:(?:修改|调整|设置|设定|改|设|定)(?:为|成|到|在)?|设在|定在|按|在|为|是|[:：=]|选用|选择|采用|改用|就用|用)\s*'
                rf'(?P<val>[A-Za-z0-9#\-_]+(?:\s*号)?)(?:井口|井)\s*$'
            )
            p3 = rf'^(?P<val>[A-Za-z0-9#\-_]+(?:\s*号)?)(?:井口|井)$'
            m = re.search(p1, clause.strip()) or re.search(p2, clause.strip()) or re.search(p3, clause.strip())
            if m:
                val = m.group('val').strip()
                if re.search(r'[A-Za-z]', val) and val.endswith('号'):
                    val = val[:-1].strip()
                selections.append((val, m.group(0)))
            elif re.search(r'或者|还是', clause):
                protected = True
                keep_wellhead = True
                extraction.setdefault('unresolved', []).append('本轮井口包含备选值，请确认一个井口编号。')

    distinct = {value for value, _ in selections}
    if keep_wellhead or task_edits_are_paused(message):
        selections, distinct, protected = [], set(), True
    if len(distinct) > 1:
        protected = True
        extraction.setdefault('unresolved', []).append('本轮井口包含多个不同取值，请确认一个井口编号。')
    if len(distinct) == 1 or protected:
        candidates = [item for item in candidates if item.get('canonical_key') != 'wellhead_id']
    if len(distinct) == 1:
        value, raw = selections[-1]
        candidates.append(_candidate('wellhead_id', value, raw))
    return candidates


def _candidate(key, value, raw):
    return {'canonical_key': key, 'raw_key': key, 'raw_value': raw,
            'normalized_value': value, 'confidence': 1.0,
            'resolution_method': 'explicit_text_exact'}


def _ground_support_vessel(candidates, extraction, message, allowed):
    """Use asserted ship choices and veto model guesses from protected evidence."""
    label = r'(?:支持船(?:编号)?|船只|母船|作业船)'
    known = {re.sub(r'\s+', '', str(value)).casefold(): value for value in allowed}
    aliases = {
        '681': '海洋石油681', '286': '海洋石油286', '708': '海洋石油708',
        'oceanic': 'DSV-Oceanic', 'dsvoceanic': 'DSV-Oceanic',
    }
    lookup = {**{key: value for key, value in aliases.items() if value in allowed}, **known}
    assignment = re.compile(
        rf'\s*(?:(?:请|现在|目前|当前|本次|确认|将|把)\s*)*(?:{label}|船)?\s*'
        r'(?:修改为|设置为|设为|改成|改为|改用|换成|使用|选择|选用|就用|要用|配|用|为|是|[：:])?\s*(.+?)\s*'
    )

    def selection(clause):
        match = assignment.fullmatch(clause)
        return lookup.get(re.sub(r'\s+', '', match[1]).casefold()) if match else None

    def mentions_vessel(clause):
        # Short numeric answers are recognized only as whole selections. A
        # question about "水深286米" must not affect the ship selected before it.
        parts = re.split(r'[，,]', clause)
        if len(parts) > 1:
            return any(mentions_vessel(part) for part in parts)
        return bool(
            re.search(label, clause)
            or selection(clause.strip('？?'))
            or selection(re.sub(
                r'^(?:(?:昨天|此前|之前|曾经|不要|不用|别|或者|还是)\s*)+',
                '', clause.strip('？?'),
            ))
            or any(re.search(r'(?<![A-Za-z0-9])' + re.escape(alias) + r'(?![A-Za-z0-9])',
                             re.sub(r'\s+', '', clause).casefold())
                   for alias in lookup if not alias.isdigit())
        )

    clauses = re.split(r'[，,。；;！!？?\n]', message)
    paused = task_edits_are_paused(message) or any(
        _field_edit_is_paused(clause, label)
        or (re.search(r'取消|撤销|不要执行', clause) and re.search(label, clause))
        for clause in clauses
    )
    protected, ambiguous = paused, False
    selections = []
    for sentence_match in re.finditer(r'([^。；;！!？?\n]+)([。；;！!？?\n]|$)', message):
        sentence = sentence_match[1]
        if not mentions_vessel(sentence):
            continue
        condition = _CABLE_CONDITION.search(sentence)
        conditional_tail = False
        if condition:
            before_condition = sentence[:condition.start()].rstrip('，, ')
            condition_text = sentence[condition.start():]
            if (not mentions_vessel(before_condition) or mentions_vessel(condition_text)
                    or not re.search(r'[，,]\s*\S', condition_text)):
                protected = True
                continue
            sentence, conditional_tail = before_condition, True
        parts = [clause.strip() for clause in re.split(r'[，,]', sentence) if clause.strip()]
        if not conditional_tail and sentence_match[2] in {'？', '?'}:
            parts[-1] += sentence_match[2]
        # Generic follow-ups belong to the immediately preceding subject.
        # A cancellation of an intervening depth edit is not a vessel veto.
        previous = ''
        cancelled = False
        queried = False
        for clause in parts:
            if clause == '等等':
                continue
            generic_cancel = re.fullmatch(
                r'(?:请|先|暂时)?(?:不要|别|取消|撤销|暂不|先不)'
                r'(?:执行|应用|确认|修改|更改|改动|改|这个修改|该修改|这个变更|该变更)', clause,
            )
            if mentions_vessel(previous):
                cancelled |= bool(generic_cancel)
                queried |= bool(
                    _VALUE_FOLLOWUP.search(clause) and _CABLE_QUESTION.search(clause)
                    and not re.search(
                        r'水深|机器人|设备|ROV|AUV|井口|管缆|电缆|起点|终点|载荷|工具|时间|开始|结束',
                        clause, re.IGNORECASE,
                    )
                )
            previous = clause
        if cancelled or queried:
            protected = True
            continue
        historical = False
        historical_choice = False
        for clause in parts:
            if _CABLE_NON_UPDATE.search(clause):
                historical = True
                historical_choice = False
            elif (re.search(r'现在|当前|本次', clause)
                  or (historical_choice and re.search(r'改为|改成|改用|换成', clause))):
                historical = False
            if not mentions_vessel(clause):
                continue
            historical_choice |= historical
            if re.search(r'或者|还是', clause):
                ambiguous = True
            if (historical or _CABLE_NEGATION.search(clause)
                    or _CABLE_QUESTION.search(clause)
                    or re.search(r'只(?:是|想)?(?:讨论|咨询|了解)|保持|不变|保留|未确定|不确定', clause)):
                protected = True
                continue
            value = selection(clause)
            if value is not None:
                selections.append((value, clause))
            elif re.search(label + r'\s*(?:改成|改为|改用|换成|使用|选择|选用|就用|要用|配|用|为|是|[：:])', clause):
                # An explicitly named unknown ship cannot be normalized into
                # a different configured vessel by the model.
                protected = True
    distinct = {value for value, _ in selections}
    if len(distinct) > 1 or ambiguous:
        extraction.setdefault('unresolved', []).append('本轮支持船包含多个不同选项，请确认一艘支持船。')
        ambiguous = True
    if paused or ambiguous:
        distinct, protected = set(), True
    if protected or distinct:
        candidates = [candidate for candidate in candidates if candidate.get('canonical_key') != 'support_vessel']
    if len(distinct) == 1:
        value, raw = selections[-1]
        candidates.append(_candidate('support_vessel', value, raw))
    return candidates


def ground_explicit_values(extraction, message, *, kb, fields, current_state, task_type):
    """Replace model guesses only when this WRITE contains an explicit value."""
    coord_fields = {f['key'] for f in fields if f.get('type') == 'coord'}
    # Do not seed with model proposals: even a range-valid guess may swap axes.
    coords = parse_coordinate_updates(message, coord_fields, current_state=current_state)
    candidates = extraction.get('slot_candidates', [])
    protected_coords = protected_coordinate_fields(message, coord_fields, current_state=current_state)
    candidates = [candidate for candidate in candidates if candidate.get('canonical_key') not in protected_coords]
    issues = coordinate_update_issues(message, coord_fields, current_state=current_state)
    if issues:
        extraction.setdefault('unresolved', []).extend(issues)
    if coords:
        candidates = [c for c in candidates if c.get('canonical_key') not in coords]
        candidates.extend(_candidate(k, v, message) for k, v in coords.items())

    if any(field.get('key') == 'water_depth' for field in fields):
        candidates = _ground_water_depth(candidates, extraction, message)

    if any(field.get('key') == 'wellhead_id' for field in fields):
        candidates = _ground_wellhead_id(candidates, extraction, message)

    # Ground explicit cable_type when stated plainly in task context.
    cable_field = next((field for field in fields if field.get('key') == 'cable_type'), None)
    if cable_field:
        allowed = cable_field.get('allowed_values') or []
        cable_alias_map = {
            '海底油气管道': ['海底油气管道', '油气管道', '油气管线', '输油管道', '输气管道', '海底管道'],
            '电力电缆': ['电力电缆', '海底电力电缆', '海底电缆', '海缆', '电力缆', '电力'],
            '光纤通信缆': ['光纤通信缆', '海底光纤通信缆', '通信光缆', '海底光缆', '光纤电缆', '通信电缆', '光纤缆', '通信缆', '光缆', '光纤'],
        }
        selections = []
        protected_cable = task_edits_are_paused(message)
        cable_mention = re.compile(r'管缆类型|电缆类型|缆线类型|管道|电缆|通信缆|光缆|海缆|电力|光纤')
        cable_paused = any(re.fullmatch(
            r'(?:请)?(?:(?:管缆|电缆|缆线)类型(?:保持)?(?:不变|不要改|别改)|'
            r'(?:不要|别|不)(?:修改|更改|改动|调整|改)(?:管缆|电缆|缆线)类型)',
            re.sub(r'\s+', '', clause),
        ) for clause in re.split(r'[，,。；;！!？?\n]', message))
        cable_paused |= any(_field_edit_is_paused(clause, r'(?:管缆|电缆|缆线)类型')
                            for clause in re.split(r'[，,。；;！!？?\n]', message))
        cable_paused |= any(
            re.search(r'取消|撤销', clause) and cable_mention.search(clause)
            for clause in re.split(r'[，,。；;！!？?\n]', message)
        )
        task_suffix = r'(?:巡检|埋设|敷设|开沟|检修|后埋|施工)(?:任务|作业)?(?:吧|了)?'
        action_prefix = r'(?:请|帮我|那帮我(?:安排)?|安排(?:一次)?|选择|选用|使用|采用|就用|要用|用|设置|设为|改为|改成|换成|换为|执行|进行|开展)'

        def explicit_cable_selection(clause: str, alias: str) -> bool:
            """Match a complete current-turn assignment, not an alias substring."""
            compact = re.sub(r'\s+', '', clause).strip('：:')
            value = re.escape(re.sub(r'\s+', '', alias))
            # Spoken input may append the cable answer to a complete time
            # schedule with whitespace instead of punctuation.
            if re.fullmatch(
                rf'\s*(?:任务|作业)?\s*{RELATIVE_OFFSET_PATTERN}\s*(?:开始|开工)'
                rf'\s*(?:持续|时长)\s*{DURATION_VALUE_PATTERN}\s+{value}\s*',
                clause,
            ):
                return True
            field = (
                rf'(?:现在|目前|当前|本次)?(?:{action_prefix})?把?(?:管缆|电缆|缆线)类型'
                rf'(?:为|是|设为|改为|改成|改用|换成|换为|选用|选择|使用|采用|就用|要用|用|：|:)?{value}'
            )
            action = rf'(?:{action_prefix})?{value}(?:{task_suffix})?'
            return bool(
                re.fullmatch(field, compact)
                or re.fullmatch(action, compact)
                or compact == re.sub(r'\s+', '', alias)
            )

        for sentence in re.split(r'[。；;！!\n]', message):
            condition = _CABLE_CONDITION.search(sentence)
            if condition:
                before_condition = sentence[:condition.start()].rstrip('，, ')
                condition_text = sentence[condition.start():]
                if (not cable_mention.search(before_condition)
                        or cable_mention.search(condition_text)
                        or not re.search(r'[，,]\s*\S', condition_text)):
                    protected_cable |= bool(cable_mention.search(sentence))
                    continue
                sentence = before_condition
            # Keep question delimiters attached to the preceding clause. This
            # permits an assignment after a separate question in one turn.
            tokens = re.split(r'([，,？?])', sentence)
            clause_items = []
            pending = ''
            for token in tokens:
                if token in {'，', ',', '？', '?'}:
                    if pending.strip():
                        clause_items.append((pending, token in {'？', '?'}))
                    pending = ''
                else:
                    pending += token
            if pending.strip():
                clause_items.append((pending, False))
            query_positions = [
                index for index, (clause, ends_question) in enumerate(clause_items)
                if ends_question or _CABLE_QUESTION.search(clause)
            ]
            # If a trailing clause asks whether the preceding assignment is
            # acceptable, none of that assignment is a WRITE. A question at
            # the beginning still permits a later independent assignment.
            trailing_query = min((index for index in query_positions if index > 0 and (
                cable_mention.search(clause_items[index][0]) or _VALUE_FOLLOWUP.search(clause_items[index][0])
            )), default=None)
            cancelled = any(
                (re.search(r'取消|撤销', clause) and cable_mention.search(clause))
                or re.fullmatch(
                    r'\s*(?:先|暂时)?(?:不要|别|取消|撤销|暂不|先不)'
                    r'(?:修改|改动|改|执行|应用|这个修改|该修改|这个管缆修改)\s*', clause,
                )
                for clause, _ in clause_items
            )
            if cancelled:
                protected_cable |= bool(cable_mention.search(sentence))
                continue
            historical_context = False
            for index, (clause, ends_question) in enumerate(clause_items):
                if _CABLE_NON_UPDATE.search(clause):
                    historical_context = True
                    protected_cable |= bool(cable_mention.search(clause))
                    continue
                if (re.match(r'\s*(?:现在|目前|当前|本次)', clause)
                        or re.search(r'(?:管缆|电缆|缆线)类型\s*(?:改为|改成|改用|换成|换为)', clause)):
                    historical_context = False
                if historical_context:
                    protected_cable |= bool(cable_mention.search(clause))
                    continue
                if (ends_question or _CABLE_QUESTION.search(clause)
                        or (trailing_query is not None and index < trailing_query)
                        or _CABLE_NEGATION.search(clause)):
                    protected_cable |= bool(cable_mention.search(clause))
                    continue
                if re.search(r'只(?:是|想)?(?:讨论|咨询|了解)|保持|不变|保留', clause) and cable_mention.search(clause):
                    protected_cable = True
                    continue
                mentioned_types = {
                    canonical_val for canonical_val in allowed
                    if any(alias in clause for alias in cable_alias_map.get(canonical_val, [canonical_val]))
                }
                if len(mentioned_types) > 1:
                    cable_paused = True
                    extraction.setdefault('unresolved', []).append('本轮管缆类型包含多个不同取值，请确认一种类型。')
                    continue
                for canonical_val in allowed:
                    aliases = sorted(cable_alias_map.get(canonical_val, [canonical_val]), key=len, reverse=True)
                    for alias in aliases:
                        if explicit_cable_selection(clause, alias):
                            selections.append((canonical_val, clause.strip()))
                            break
        distinct_selected = {val for val, _ in selections}
        if task_edits_are_paused(message) or cable_paused:
            distinct_selected = set()
            protected_cable = True
        if len(distinct_selected) > 1:
            protected_cable = True
            extraction.setdefault('unresolved', []).append('本轮管缆类型包含多个不同取值，请确认一种类型。')
        if protected_cable:
            candidates = [candidate for candidate in candidates if candidate.get('canonical_key') != 'cable_type']
        if len(distinct_selected) == 1:
            value, raw = selections[-1]
            candidates = [candidate for candidate in candidates if candidate.get('canonical_key') != 'cable_type']
            candidates.append(_candidate('cable_type', value, raw))

    # Ground the ship independently of simultaneous list or scalar updates.
    vessel_field = next((field for field in fields if field.get('key') == 'support_vessel'), None)
    if vessel_field:
        candidates = _ground_support_vessel(
            candidates, extraction, message, vessel_field.get('allowed_values') or [],
        )

    candidates = ground_equipment_values(candidates, extraction, message, kb)

    # Ground explicit immediate start_time when user plainly requests "现在开始/即刻开始/立刻开始"
    start_time_field = next((field for field in fields if field.get('key') == 'start_time'), None)
    if start_time_field and not any(c.get('canonical_key') == 'start_time' and c.get('normalized_value') for c in candidates):
        from src.temporal.temporal_parser import TemporalParser

        immediate_patterns = [
            r'(?:(?:请|帮我)\s*)?(?:任务|作业|工作|巡检)?\s*(?:从)?(?:现在|即刻|立刻|马上|此时|当即|即时)\s*(?:就)?(?:开始|开展|执行|开工|起步|做|进行)',
            r'(?:开始|起步|开工)时间\s*(?:定在|设为|为|是)?\s*(?:现在|即刻|立刻|马上)',
            r'(?:现在|即刻|立刻|马上)(?:开始)?'
        ]
        immediate_suffix = r'(?:执行(?:任务|作业)|任务|作业)?(?:吧)?'
        matched_immediate = False
        match_raw = ""
        immediate_paused = (
            TemporalParser._time_edits_are_paused(message)
            or 'start_time' in TemporalParser._cancelled_time_targets(message)
            or any(
                _field_edit_is_paused(clause, r'(?:开始时间|起始时间|开工时间|开始)')
                for clause in re.split(r'[，,。；;！!？?\n]', message)
            )
        )
        for sentence_match in re.finditer(r'[^。；;！!\n]+', message):
            if immediate_paused:
                break
            sentence = sentence_match[0]
            if re.search(r'如果|假如|假设|倘若|要是|除非|若是', sentence):
                continue
            clauses = re.split(r'[，,]', sentence)
            cancelled = any(
                index > 0 and re.fullmatch(
                    r'\s*(?:请|先|暂时)?(?:取消|撤销)(?:这个|该|上述|刚才(?:的)?)?(?:修改|调整|改动|变更)\s*',
                    clause,
                ) and any(re.fullmatch(pattern + immediate_suffix, clauses[index - 1].strip())
                          for pattern in immediate_patterns)
                for index, clause in enumerate(clauses)
            )
            if cancelled or any(_VALUE_FOLLOWUP.search(clause) and _CABLE_QUESTION.search(clause)
                                for clause in clauses[1:]):
                continue
            for clause_match in re.finditer(r'[^，,]+', sentence):
                clause_strip = clause_match[0].strip()
                if TemporalParser._nonassertive_time_context(
                    message, sentence_match.start() + clause_match.start(),
                    sentence_match.start() + clause_match.end(),
                ):
                    continue
                if any(re.fullmatch(p + immediate_suffix, clause_strip) for p in immediate_patterns):
                    matched_immediate = True
                    match_raw = clause_strip
                    break
            if matched_immediate:
                break

        if matched_immediate:
            from src.temporal.simulated_time import get_current_datetime
            now_dt = get_current_datetime().replace(microsecond=0)
            iso_start = now_dt.isoformat(timespec='seconds')
            candidates = [c for c in candidates if c.get('canonical_key') != 'start_time']
            candidates.append(_candidate('start_time', iso_start, match_raw))

    extraction['slot_candidates'] = candidates

    # Payload recovery runs once in PayloadMutationManager, where assertion
    # scope and additive/full-list operations share the same interpretation.
    return extraction
