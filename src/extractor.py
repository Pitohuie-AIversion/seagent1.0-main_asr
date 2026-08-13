"""
extractor.py — 参数提取器
每轮对话后，用 LLM 从最新用户消息中提取或更新任务参数。
使用低温度、结构化 prompt，返回严格的结构化候选列表。
"""

import json
import re
from datetime import datetime, timedelta
from pathlib import Path
import yaml

from .llm_client import LLMClient
from .model_profile import ModelRole, _is_unsupported_role_keyword_error
from .normalizer import FieldNormalizer
from .slot_store import normalize_payload_match_key

_CONFIG_DIR = Path(__file__).parent.parent / "config"

def _load_payload_catalog() -> dict:
    try:
        with open(_CONFIG_DIR / "assets.yaml", encoding="utf-8") as f:
            assets = yaml.safe_load(f) or {}
        return assets.get("payload_catalog", {})
    except Exception:
        return {}


MAX_EXTRACTION_USER_HISTORY = 6

EXTRACTION_TASK = """\
你是一个严格的任务参数候选抽取器。

【极重要：输出边界】
你当前不是对话助手，而是结构化候选抽取器。
- 只允许输出一个 JSON object，不得输出任何自然语言解释。
- 必须严格遵守以下输出 JSON 结构。
- 可能提供的最近历史消息只用于识别当前回复所指的字段或编号选项；只有最新 user 消息能触发本轮字段更新。

【输出格式】
{{
  "slot_candidates": [
    {{
      "raw_key": "作业类型",
      "canonical_key": "task_type",
      "raw_value": "巡检",
      "normalized_value": "管缆巡检",
      "confidence": 0.95
    }},
    {{
      "raw_key": "作业类型标识",
      "canonical_key": "task_type_key",
      "raw_value": "巡检",
      "normalized_value": "pipeline_inspection",
      "confidence": 0.95
    }}
  ],
  "unresolved": []
}}

【提取规则】
1. 对于任务类型：
{task_type_rules}
2. 如果无法识别任何任务字段，slot_candidates 和 unresolved 都返回空列表。
3. 只支持上述任务；用户明确描述了不支持的任务时，不提取 task_type，并把任务描述写入 unresolved。
4. 如果最新用户消息中对同一字段多次修正，以最后出现的候选为准。
5. 最新用户消息使用“第一个”“第二个”“选2”等编号选择时，只能根据最近历史中明确列出的选项映射。
6. 如用户明确说任务紧急（"紧急"、"急"、"加急"等），提取 canonical_key: "emergency_mode" 且 normalized_value: true。
7. 本阶段只允许输出 task_type、task_type_key、emergency_mode。
"""

EXTRACTION_SYSTEM = """\
你是一个严格的参数候选抽取器，专门从用户的自然语言中提取水下ROV作业任务参数。

【极重要：输出边界】
你当前不是对话助手，而是结构化候选抽取器。
- 只允许输出一个 JSON object，不得输出任何自然语言解释。
- 即使当前任务已确认、已发布、已锁定，只要用户本轮明确补充、修改或确认字段，也必须抽取为候选列表。
- 如果用户本轮没有任何字段更新，返回 slot_candidates 为空列表的 JSON。
- 可能提供的最近历史消息只用于识别当前回复所指的字段或编号选项；只有最新 user 消息能触发本轮字段更新。

【输出格式】
{{
  "slot_candidates": [
    {{
      "raw_key": "水深",
      "canonical_key": "water_depth",
      "raw_value": "大约三百米",
      "normalized_value": "300",
      "confidence": 0.95
    }}
  ],
  "unresolved": []
}}

【提取规则】
1. 只提取用户明确提供或可以高置信度推断的信息，不猜测。
2. 每一个提取的字段，必须包含 raw_key（用户所用的词）、canonical_key（规范化字段名）、raw_value（用户说原始值）、normalized_value（转换后的标准化值，例如数字、日期等）和 confidence（置信度）。
3. 最新用户消息是本轮候选值的唯一文本来源；当前任务状态只用于避免重复提取。
4. 如果最新用户消息中对同一字段出现多个候选或多次反悔/修正，以文本中最后出现的候选为准。
5. 对于时间信息：将口语时间转换为 YYYY-MM-DDTHH:MM:SS 格式，无时间部分时补 T00:00:00；"现在/当前/立即"等表达必须基于【当前时间】换算。
6. 对于坐标：normalized_value 提取为 {{"lat": float, "lon": float}} 格式，统一十进制度。
7. 对于水深：统一转换为米（m）为单位的数值，例如"1千米"→1000，"500m"→500。
8. 对于任务类型：
{task_type_rules}
9. 对于ROV型号：如用户描述模糊（如"深水工作ROV"、"轻型观察"），提取 canonical_key: "rov_description" 字段，不要强行映射型号名。
10. 严格区分机器人系列与型号：equipment_family 只能填写 robot_families 的系列全名；equipment_type 只能填写该系列 model_variants 的型号全名。用户只明确系列时不得猜测型号；只明确型号时可由后端根据 family_id 补齐系列。
11. 若确定ROV型号，可自动识别出ROV类型：{ROV2type}
12. 机器人能力、最大水深、载荷、功率、尺寸、状态、任务阈值和作业限制必须以所需字段、允许值、ROV2type和后续知识库/约束校验为准；不得凭通用知识补全或改写配置中没有的信息。
13. 如用户明确说任务紧急（"紧急"、"急"、"加急"等），提取 canonical_key: "emergency_mode" 且 normalized_value: true。
14. 最新用户消息使用“第一个”“第二个”“选2”等编号选择时，只能根据最近历史中明确列出的选项映射。
15. 只根据所需字段中定义的key提取，不新增其他字段。
16. 任务维度中无法识别或无法映射的片段写入 unresolved；普通寒暄不写入 unresolved。无法识别任何字段时返回空 slot_candidates。

【枚举字段抽取边界】
- raw_value 必须保留用户原始表达。
- normalized_value 可以填写模型初步判断，但不代表已经通过后端标准值校验。
- 用户可以使用 allowed_values 对应的 aliases、简称、展示名、自然语言描述或上下文指代；不要因为用户没有逐字复制标准名称就判定无效。
- 不确定时不要猜测标准候选，保持用户原表达，由后端结合 aliases 和 allowed_values 解析。

【当前时间】{today}

【所需字段及其描述，key为字段名，label为字段描述】
{required}

【当前任务状态（已知字段，避免重复提取）】
{current_state}
"""


def _catalog_from_task_type_map(task_type_map: dict[str, str] | None) -> list[dict[str, object]]:
    groups: dict[str, list[str]] = {}
    for task_type_value, template_key in (task_type_map or {}).items():
        groups.setdefault(template_key, []).append(task_type_value)
    return [
        {
            "task_type_key": template_key,
            "display_name": values[0] if len(values) == 1 else template_key,
            "task_type_values": values,
        }
        for template_key, values in groups.items()
    ]


def _normalize_task_type_catalog(
    task_type_catalog: list[dict] | None,
    task_type_map: dict[str, str] | None,
) -> list[dict[str, object]]:
    raw_catalog = task_type_catalog or _catalog_from_task_type_map(task_type_map)
    catalog: list[dict[str, object]] = []
    for item in raw_catalog:
        if not isinstance(item, dict):
            continue
        template_key = str(item.get("task_type_key") or "").strip()
        if not template_key:
            continue
        values = [
            str(value).strip()
            for value in (item.get("task_type_values") or [])
            if str(value).strip()
        ]
        catalog.append(
            {
                "task_type_key": template_key,
                "display_name": str(item.get("display_name") or template_key).strip(),
                "task_type_values": values,
            }
        )
    return catalog


def _build_task_type_rules(task_type_catalog: list[dict]) -> str:
    lines = []
    for item in task_type_catalog:
        tkey = item["task_type_key"]
        display_name = item.get("display_name") or tkey
        values = list(item.get("task_type_values") or [])
        values_str = " / ".join(str(v) for v in values)
        lines.append(
            f'   - task_type_key: "{tkey}"；'
            f'display_name: {display_name}；'
            f'task_type_values: {values_str}'
        )
    lines.append(
        '   - 先根据用户语义选择 task_type_key；'
        '再且仅能从对应 task_type_values 中选择 task_type'
    )
    lines.append(
        '   - 如果该 task_type_key 只有一个 task_type_values，'
        '可以输出该唯一 task_type'
    )
    lines.append(
        '   - 如果 task_type_values 有多个且用户语义不足以唯一选择，'
        '只输出 task_type_key，不要猜测或伪造 task_type'
    )
    lines.append(
        '   - 用户描述的任务类型不在上述范围内时，不提取任何 task_type 字段'
    )
    return "\n".join(lines)


def _task_type_catalog_indexes(task_type_catalog: list[dict]) -> tuple[dict[str, dict], dict[str, str], dict[str, str]]:
    by_key: dict[str, dict] = {}
    display_to_key: dict[str, str] = {}
    value_to_key: dict[str, str] = {}
    for item in task_type_catalog:
        template_key = str(item.get("task_type_key") or "")
        if not template_key:
            continue
        by_key[template_key] = item
        display = item.get("display_name")
        if display:
            display_to_key[FieldNormalizer.make_match_key(display)] = template_key
        for value in item.get("task_type_values") or []:
            value_to_key[FieldNormalizer.make_match_key(value)] = template_key
    return by_key, display_to_key, value_to_key


def _match_catalog_value(value: object, values: list[str]) -> str | None:
    needle = FieldNormalizer.make_match_key(value)
    if not needle:
        return None
    matches = [
        candidate
        for candidate in values
        if FieldNormalizer.make_match_key(candidate) == needle
    ]
    return matches[0] if len(matches) == 1 else None


def _resolve_template_key_from_candidate(
    candidate: dict,
    task_type_catalog: list[dict],
) -> tuple[str | None, str | None]:
    by_key, display_to_key, value_to_key = _task_type_catalog_indexes(task_type_catalog)
    for input_key in ("normalized_value", "raw_value"):
        value = candidate.get(input_key)
        if value is None or value == "":
            continue
        value_str = str(value).strip()
        match_key = FieldNormalizer.make_match_key(value)
        if value_str in by_key:
            return value_str, "task_type_key_exact"
        if match_key in display_to_key:
            return display_to_key[match_key], "template_display_name"
        if match_key in value_to_key:
            return value_to_key[match_key], "template_from_task_type_value"
    return None, None


def _enforce_task_type_catalog_candidates(
    candidates: list[dict],
    task_type_catalog: list[dict],
) -> tuple[list[dict], list[str]]:
    if not task_type_catalog:
        return candidates, []

    by_key, display_to_key, value_to_key = _task_type_catalog_indexes(task_type_catalog)
    normalized: list[dict] = []
    unresolved: list[str] = []
    resolved_template_key: str | None = None
    has_task_type = False

    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        canonical_key = str(candidate.get("canonical_key") or "").strip()
        if canonical_key not in ("task_type", "task_type_key"):
            normalized.append(candidate)
            continue

        if canonical_key == "task_type_key":
            template_key, method = _resolve_template_key_from_candidate(candidate, task_type_catalog)
            if not template_key or template_key not in by_key:
                unresolved.append(f"task_type_key 表达 {candidate.get('raw_value')!r} 不属于当前合法任务模板。")
                continue
            copy_candidate = dict(candidate)
            copy_candidate["normalized_value"] = template_key
            copy_candidate["resolution_method"] = method or "task_type_key_exact"
            normalized.append(copy_candidate)
            resolved_template_key = template_key
            continue

        # canonical_key == "task_type"
        matched_value = None
        matched_template_key = None
        for input_key in ("normalized_value", "raw_value"):
            value = candidate.get(input_key)
            if value is None or value == "":
                continue
            match_key = FieldNormalizer.make_match_key(value)
            if match_key in value_to_key:
                candidate_template_key = value_to_key[match_key]
                values = list(by_key[candidate_template_key].get("task_type_values") or [])
                matched_value = _match_catalog_value(value, values)
                matched_template_key = candidate_template_key
                break
            if match_key in display_to_key:
                matched_template_key = display_to_key[match_key]
                break

        if not matched_value:
            if matched_template_key and not resolved_template_key:
                resolved_template_key = matched_template_key
                key_candidate = dict(candidate)
                key_candidate["raw_key"] = "作业类型标识"
                key_candidate["canonical_key"] = "task_type_key"
                key_candidate["normalized_value"] = matched_template_key
                key_candidate["resolution_method"] = "template_display_name"
                normalized.append(key_candidate)
            elif resolved_template_key:
                allowed_values = by_key[resolved_template_key].get("task_type_values") or []
                unresolved.append(
                    f"task_type 表达 {candidate.get('raw_value')!r} 不属于 {resolved_template_key} 的合法候选：{allowed_values}。"
                )
            continue

        if resolved_template_key and matched_template_key != resolved_template_key:
            unresolved.append(
                f"task_type 表达 {candidate.get('raw_value')!r} 与 task_type_key={resolved_template_key} 不一致。"
            )
            continue

        copy_candidate = dict(candidate)
        copy_candidate["normalized_value"] = matched_value
        copy_candidate["resolution_method"] = "canonical_exact"
        normalized.append(copy_candidate)
        has_task_type = True
        if not resolved_template_key and matched_template_key:
            resolved_template_key = matched_template_key
            key_candidate = dict(candidate)
            key_candidate["raw_key"] = "作业类型标识"
            key_candidate["canonical_key"] = "task_type_key"
            key_candidate["normalized_value"] = matched_template_key
            key_candidate["resolution_method"] = "template_from_task_type_value"
            normalized.append(key_candidate)

    if resolved_template_key and not has_task_type:
        values = list(by_key[resolved_template_key].get("task_type_values") or [])
        if len(values) == 1:
            normalized.append(
                {
                    "raw_key": "任务类型",
                    "canonical_key": "task_type",
                    "raw_value": values[0],
                    "normalized_value": values[0],
                    "confidence": 1.0,
                    "resolution_method": "single_schema_candidate",
                }
            )

    return normalized, unresolved


class ParameterExtractor:
    def __init__(self, llm: LLMClient):
        self.llm = llm

    def extract_updates(
        self,
        user_message: str,
        current_state: dict,
        task_type_key: str | None,
        task_type_map: dict[str, str] | None = None,
        task_type_catalog: list[dict] | None = None,
        required: list[dict] | None = None,
        ROV2type: list[dict] | None = None,
        conversation_history: list[dict] | None = None,
    ) -> dict:
        from .simulated_time import get_current_datetime
        now = get_current_datetime()
        today_str = now.isoformat()

        known = {k: v for k, v in current_state.items() if v is not None}
        normalized_task_type_catalog = _normalize_task_type_catalog(
            task_type_catalog,
            task_type_map,
        )
        task_type_rules = _build_task_type_rules(normalized_task_type_catalog)

        if task_type_key is None:
            system_prompt = EXTRACTION_TASK.format(
                task_type_rules=task_type_rules,
            )
        else:
            required_json = json.dumps(required, ensure_ascii=False, indent=2) if required else "[]"
            system_prompt = EXTRACTION_SYSTEM.format(
                today=today_str,
                current_state=json.dumps(known, ensure_ascii=False, indent=2),
                task_type_rules=task_type_rules,
                required=required_json,
                ROV2type=ROV2type,
            )

        extraction_context = self._select_extraction_history(
            user_message,
            required,
            conversation_history,
        )
        messages = [
            {"role": "system", "content": system_prompt},
            *extraction_context,
            {"role": "user", "content": user_message},
        ]

        try:
            result = self.llm.extract_json(messages, max_tokens=800, role=ModelRole.EXTRACTOR)
        except TypeError as exc:
            if not _is_unsupported_role_keyword_error(exc):
                raise
            result = self.llm.extract_json(messages, max_tokens=800)

        if not isinstance(result, dict):
            result = {}

        allowed_keys = self._allowed_candidate_keys(task_type_key, required)
        raw_candidates = result.get("slot_candidates")
        if not isinstance(raw_candidates, list):
            # 兼容模型偶尔返回的扁平 JSON，但仍执行字段白名单检查。
            raw_candidates = [
                {
                    "raw_key": key,
                    "canonical_key": key,
                    "raw_value": value,
                    "normalized_value": value,
                    "confidence": 1.0,
                }
                for key, value in result.items()
                if key not in ("intent", "unresolved", "slot_candidates")
            ]

        unresolved = result.get("unresolved")
        if not isinstance(unresolved, list):
            unresolved = []

        raw_candidates, catalog_unresolved = _enforce_task_type_catalog_candidates(
            raw_candidates,
            normalized_task_type_catalog,
        )

        normalized_candidates, resolver_unresolved = self._normalize_candidates(
            raw_candidates,
            allowed_keys,
            required or [],
            current_state,
            conversation_history or [],
        )
        normalized_candidates = self._merge_explicit_enum_candidates(
            user_message,
            normalized_candidates,
            required or [],
            allowed_keys,
            conversation_history or [],
        )
        normalized_candidates = self._merge_explicit_datetime_candidates(
            user_message,
            normalized_candidates,
            required or [],
            allowed_keys,
            now,
            conversation_history or [],
        )

        list_mutations, mutation_unresolved = self._detect_payload_mutation(
            user_message,
            current_state,
            required or [],
        )

        if list_mutations:
            normalized_candidates = [
                cand for cand in normalized_candidates
                if cand.get("canonical_key") != "payload"
            ]

        all_unresolved = [*unresolved, *catalog_unresolved, *resolver_unresolved, *mutation_unresolved]

        return {
            "slot_candidates": normalized_candidates,
            "unresolved": [
                str(item).strip()
                for item in all_unresolved
                if str(item).strip()
            ],
            "list_mutations": list_mutations,
        }

    @classmethod
    def _detect_payload_mutation(
        cls,
        user_message: str,
        current_state: dict,
        required: list[dict],
    ) -> tuple[list[dict], list[str]]:
        """识别 payload 增量操作协议（add/remove/replace/clear/ambiguous）。"""
        text = str(user_message or "").strip()
        if not text:
            return [], []

        existing_payload_val = current_state.get("payload")
        has_existing_payload = False
        if isinstance(existing_payload_val, list) and len(existing_payload_val) > 0:
            has_existing_payload = True
        elif isinstance(existing_payload_val, str) and len(existing_payload_val.strip()) > 0:
            has_existing_payload = True

        allowed_values = []
        for field in required:
            if field.get("key") == "payload":
                allowed_values = field.get("allowed_values") or []
                if not allowed_values and field.get("allowed_values_ref"):
                    ref = str(field["allowed_values_ref"])
                    if ref.startswith("payload_options.") or ref in ("supported_payloads", "onboard_payloads", "all_payloads"):
                        try:
                            from .knowledge_retriever import KnowledgeBase
                            from .output_builder import OutputBuilder
                            kb = getattr(cls, "kb", None) or KnowledgeBase()
                            task_key = current_state.get("task_type_key") or ""
                            allowed_values = OutputBuilder(kb).resolve_allowed_values(field, str(task_key), current_state) or []
                        except Exception:
                            pass
                break

        specific_items = cls._find_payload_items_in_text(text, allowed_values)
        has_specific_item_in_text = bool(specific_items)
        has_scoped_indicator = any(ind in text for ind in ("里的", "中的", "内的", "列表中的", "列表里的", "但保留", "保留"))

        clear_patterns = [
            r"^(?:清空|清掉)\s*(?:所有|全部|整个)?\s*(?:载荷|工具|payload)$",
            r"(?:清空|清掉)\s*(?:所有|全部|整个)?\s*(?:载荷|工具|payload)",
            r"(?:删除|放弃)\s*(?:所有|全部|整个)\s*(?:载荷|工具|payload)",
            r"(?:所有|全部|整个)\s*(?:载荷|工具|payload)\s*(?:都不要|都不用|不需要|都清空|都不带)(?:了)?",
            r"不要任何\s*(?:载荷|工具|payload)",
        ]
        is_clear_intent = any(re.search(pat, text, re.IGNORECASE) for pat in clear_patterns)

        if is_clear_intent and not (has_specific_item_in_text and (has_scoped_indicator or not text.strip().endswith(("载荷", "工具", "payload", "都不要了", "都不用", "不需要", "都清空", "都不带")))):
            return [
                {
                    "field": "payload",
                    "operation": "clear",
                    "items": [],
                    "target_items": [],
                    "raw_text": text,
                    "confidence": 0.95,
                    "source": "user_input",
                }
            ], []

        replace_a = re.search(
            r"(?:把|将)\s*(?P<target>.+?)\s*(?:换成|替换成|替换为|改成)\s*(?P<new>.+)",
            text,
        )
        if replace_a:
            target_str = replace_a.group("target").strip()
            new_str = replace_a.group("new").strip()
            targets = cls._find_payload_items_in_text(target_str, allowed_values) or [target_str]
            news = cls._find_payload_items_in_text(new_str, allowed_values) or [new_str]
            return [
                {
                    "field": "payload",
                    "operation": "replace",
                    "items": news,
                    "target_items": targets,
                    "raw_text": text,
                    "confidence": 0.95,
                    "source": "user_input",
                }
            ], []

        replace_b = re.search(
            r"用\s*(?P<new>.+?)\s*(?:替换掉|替换为|替换|换掉|替代)\s*(?P<target>.+)",
            text,
        )
        if replace_b:
            new_str = replace_b.group("new").strip()
            target_str = replace_b.group("target").strip()
            news = cls._find_payload_items_in_text(new_str, allowed_values) or [new_str]
            targets = cls._find_payload_items_in_text(target_str, allowed_values) or [target_str]
            return [
                {
                    "field": "payload",
                    "operation": "replace",
                    "items": news,
                    "target_items": targets,
                    "raw_text": text,
                    "confidence": 0.95,
                    "source": "user_input",
                }
            ], []

        remove_match = re.search(
            r"(?:去掉|移除|删除|取消|不需要|不要|放弃|清掉|不带)\s*(?:载荷中的|载荷里的|工具中的|工具里的|载荷|工具)?\s*(?P<item>.+)",
            text,
        )
        if remove_match:
            item_raw = remove_match.group("item").strip()
            remove_target_text = item_raw
            for keep_sep in ("但保留", "除了", "保留"):
                if keep_sep in remove_target_text:
                    remove_target_text = remove_target_text.split(keep_sep)[0].strip()
            found_payload = cls._find_payload_items_in_text(remove_target_text, allowed_values)
            has_payload_keyword = any(kw in text for kw in ("载荷", "工具", "payload"))
            if found_payload or has_payload_keyword:
                removed_items = found_payload or [remove_target_text]
                return [
                    {
                        "field": "payload",
                        "operation": "remove",
                        "items": removed_items,
                        "target_items": [],
                        "raw_text": text,
                        "confidence": 0.95,
                        "source": "user_input",
                    }
                ], []

        add_match = re.search(
            r"(?:再加一个|加一个|再加|添加|增加|还要|加上|还需要|配备|多带一个|带上|携带)\s*(?P<item>.+)",
            text,
        )
        if add_match:
            item_raw = add_match.group("item").strip()
            added_items = cls._find_payload_items_in_text(text, allowed_values) or [item_raw]
            return [
                {
                    "field": "payload",
                    "operation": "add",
                    "items": added_items,
                    "target_items": [],
                    "raw_text": text,
                    "confidence": 0.95,
                    "source": "user_input",
                }
            ], []

        matched_items = cls._find_payload_items_in_text(text, allowed_values)

        if not has_existing_payload and matched_items:
            return [
                {
                    "field": "payload",
                    "operation": "add",
                    "items": matched_items,
                    "target_items": [],
                    "raw_text": text,
                    "confidence": 0.95,
                    "source": "user_input",
                }
            ], []

        if has_existing_payload and matched_items:
            unresolved_msg = f"已有载荷列表不为空，表达“{text}”缺乏明确的增删改指令。"
            return [], [unresolved_msg]

        return [], []

    @classmethod
    def _find_payload_items_in_text(
        cls,
        text: str,
        allowed_values: list[str] | None = None,
    ) -> list[str]:
        """从文本中找出匹配 allowed_values 或 assets.yaml payload_catalog 的规范载荷名称列表。"""
        if not text:
            return []

        matches: list[tuple[int, int, str, int]] = []
        allowed_spans: set[tuple[int, int]] = set()

        if allowed_values:
            for val in allowed_values:
                if not isinstance(val, str) or not val:
                    continue
                stripped_val = normalize_payload_match_key(val)
                cands = [val]
                if stripped_val and stripped_val != val:
                    cands.append(stripped_val)
                for cand in cands:
                    start = 0
                    while True:
                        idx = text.find(cand, start)
                        if idx < 0:
                            break
                        span = (idx, idx + len(cand))
                        matches.append((idx, idx + len(cand), val, 1))
                        allowed_spans.add(span)
                        start = idx + 1

        catalog = _load_payload_catalog()
        for cat_id, info in catalog.items():
            name = info.get("name")
            if not name:
                continue
            aliases = info.get("aliases") or []
            candidates_to_check = [name, *aliases]
            candidates_to_check.sort(key=len, reverse=True)
            for cand in candidates_to_check:
                if not cand:
                    continue
                start = 0
                while True:
                    idx = text.find(cand, start)
                    if idx < 0:
                        break
                    span = (idx, idx + len(cand))
                    if span not in allowed_spans:
                        target_name = name
                        if allowed_values:
                            cand_key = normalize_payload_match_key(cand)
                            for a_val in allowed_values:
                                if isinstance(a_val, str) and normalize_payload_match_key(a_val) == cand_key:
                                    target_name = a_val
                                    break
                        matches.append((idx, idx + len(cand), target_name, 2))
                    start = idx + 1

        matches.sort(key=lambda m: (-(m[1] - m[0]), m[3]))
        non_overlapping: list[tuple[int, int, str, int]] = []
        for m in matches:
            m_start, m_end, m_name, m_prio = m
            is_subspan = False
            for prev_start, prev_end, _, _ in non_overlapping:
                if m_start >= prev_start and m_end <= prev_end:
                    is_subspan = True
                    break
            if not is_subspan:
                non_overlapping.append(m)

        non_overlapping.sort(key=lambda m: m[0])

        found: list[str] = []
        for _, _, name, _ in non_overlapping:
            if name not in found:
                found.append(name)

        return found

    @staticmethod
    def _allowed_candidate_keys(
        task_type_key: str | None,
        required: list[dict] | None,
    ) -> set[str]:
        """根据当前抽取阶段生成字段白名单。"""
        if task_type_key is None:
            return {"task_type", "task_type_key", "emergency_mode"}

        keys = {
            str(field.get("key"))
            for field in required or []
            if field.get("key")
        }
        # 这些是收集流程使用的控制/中间字段，不一定直接出现在输出 schema 中。
        keys.update(
            {
                "task_type",
                "task_type_key",
                "emergency_mode",
                "rov_description",
                "equipment_name",
                "raw_oilfield_name",
                "oilfield_name",
            }
        )
        return keys

    @staticmethod
    def _is_recently_asked_field(
        field: dict,
        conversation_history: list[dict] | None,
    ) -> bool:
        """判断字段是否是对话历史中 assistant 最近追问过的字段。"""
        if not conversation_history:
            return False
        last_assistant_msg: str | None = None
        for turn in reversed(conversation_history):
            if isinstance(turn, dict) and turn.get("role") == "assistant":
                content = turn.get("content")
                if isinstance(content, str) and content.strip():
                    last_assistant_msg = content
                    break
        if not last_assistant_msg:
            return False
        key = str(field.get("key") or "").strip()
        raw_label = str(field.get("label") or key)
        label_variants = []
        if raw_label:
            label_variants.append(raw_label)
            stripped = re.sub(r"[（(][^）)]*[）)]", "", raw_label).strip()
            if stripped and stripped != raw_label:
                label_variants.append(stripped)
        if key and key not in label_variants:
            label_variants.append(key)
        alias_candidates = [v for v in label_variants if v]
        if not alias_candidates:
            return False
        return any(v in last_assistant_msg for v in alias_candidates)

    @staticmethod
    def _merge_explicit_enum_candidates(
        user_message: str,
        candidates: list[dict],
        required: list[dict],
        allowed_keys: set[str],
        conversation_history: list[dict] | None = None,
    ) -> list[dict]:
        """Recover schema enum values explicitly present but omitted by the LLM."""
        merged = list(candidates)
        existing_keys = {
            str(candidate.get("canonical_key") or "")
            for candidate in candidates
            if isinstance(candidate, dict)
        }
        text = str(user_message or "")
        # LLM 返回空 candidates（明确表示"没有提取到任何东西"）时，兜底补齐要非常严格：
        #   只有字段存在"显式赋值语法"（label 为/是/:/=/... value）时才允许补齐；
        #   裸拼接（如"管缆类型海底油气管道"）不触发兜底，尊重 LLM 的空提取判定。
        # 当 LLM 至少返回了 1 个 candidate（提取到了一些字段）时，兜底门槛可以放低到
        #   "只要显式出现了字段名"就允许补齐（因为 LLM 只是漏了一个明显字段，不是完全没理解）。
        llm_returned_none = len(candidates) == 0

        for field in required:
            key = str(field.get("key") or "").strip()
            if (
                not key
                or key not in allowed_keys
                or key in existing_keys
                or field.get("type") == "list"
            ):
                continue

            matches: list[tuple[int, int, str]] = []
            raw_label = str(field.get("label") or key)
            canonical_key = str(key)

            label_variants: list[str] = []
            if raw_label:
                label_variants.append(raw_label)
                # 去括号单位版本："水深（米）" → "水深"
                stripped = re.sub(r"[（(][^）)]*[）)]", "", raw_label).strip()
                if stripped and stripped != raw_label:
                    label_variants.append(stripped)
                # 纯中文/字母前缀版本（最后一个括号前内容）
                paren_pos = re.search(r"[（(]", raw_label)
                if paren_pos:
                    prefix = raw_label[:paren_pos.start()].strip()
                    if prefix and prefix not in label_variants:
                        label_variants.append(prefix)
            if canonical_key and canonical_key not in label_variants:
                label_variants.append(canonical_key)

            # 找最佳匹配（文本中最早出现、长度最长的标签变体）
            best_label: str = ""
            best_label_pos: int = -1
            for variant in label_variants:
                if not variant:
                    continue
                pos = text.find(variant)
                if pos < 0:
                    continue
                if (
                    best_label_pos < 0
                    or pos < best_label_pos
                    or (pos == best_label_pos and len(variant) > len(best_label))
                ):
                    best_label = variant
                    best_label_pos = pos

            has_field_ref = best_label_pos >= 0
            label = best_label or raw_label

            # 显式赋值语法：字段名后紧跟 (为|是|:|：|=|等于|改成|...) 等赋值连接符，允许可选空白
            has_explicit_assign_syntax = False
            if has_field_ref and bool(best_label):
                tail = text[best_label_pos + len(best_label):]
                if re.match(r"^\s*(?:为|是|[:：=]|等于|改成|调整为|设为|修改为|换|设置为|替换为|就用|选|使用|用)", tail):
                    has_explicit_assign_syntax = True
            # 无字段名时的"值级"显式赋值语法：如"原来是A，现在改为B" 这种全局赋值连接符后恰好跟了字段允许值 → 视为该字段的显式赋值
            if not has_explicit_assign_syntax and not has_field_ref:
                assign_words = ("改成", "调整为", "设为", "修改为", "改为", "换成", "替换为", "就用", "用", "选", "使用", "设置为", "换")
                assign_pos = -1
                for w in assign_words:
                    p = text.rfind(w)
                    if p >= 0 and p > assign_pos:
                        assign_pos = p + len(w)
                if assign_pos >= 0:
                    tail_after_assign = text[assign_pos:].strip()
                    allowed_list = field.get("allowed_values") or []
                    for allowed_value in allowed_list:
                        if (
                            isinstance(allowed_value, str)
                            and allowed_value
                            and tail_after_assign.startswith(allowed_value)
                        ):
                            has_explicit_assign_syntax = True
                            break

            # 字段专用的"隐式但无歧义"赋值语法（目前用于 water_depth：水深 300 米 / 水深300m / 水深改成500米）
            field_specific_implicit_ok = False
            number_match_value: str | None = None
            field_recently_asked = ParameterExtractor._is_recently_asked_field(
                field, conversation_history
            )
            tasktype_exact_reply_ok = False
            if key == "task_type":
                allowed_list = field.get("allowed_values") or []
                tasktype_exact_reply_ok = any(
                    isinstance(allowed_value, str)
                    and allowed_value
                    and text.strip() == allowed_value
                    for allowed_value in allowed_list
                )
            if key == "water_depth":
                if has_field_ref and bool(best_label):
                    tail = text[best_label_pos + len(best_label):]
                    num_m = re.match(
                        r"^\s*(?:(?:改成|调整为|设为|修改为|换|设置为|替换为)?\s*)?(\d+(?:\.\d+)?)\s*(?:米|m|公尺|M)",
                        tail,
                        re.IGNORECASE,
                    )
                    if num_m:
                        field_specific_implicit_ok = True
                        number_match_value = num_m.group(1)
                elif field_recently_asked:
                    # 追问场景放宽：用户只回复"500米"/"500m"/"500"（不带label），也能匹配
                    num_m = re.search(
                        r"(\d+(?:\.\d+)?)\s*(?:米|m|公尺|M)?",
                        text,
                        re.IGNORECASE,
                    )
                    if num_m:
                        field_specific_implicit_ok = True
                        number_match_value = num_m.group(1)

            # BUG#4 修复：检测"label+allowed_value 裸拼接"匹配（无赋值连接符但值紧跟字段名）
            has_naked_concat_match = False
            naked_concat_value: str | None = None
            if (
                llm_returned_none
                and has_field_ref
                and not has_explicit_assign_syntax
                and not field_specific_implicit_ok
                and bool(best_label)
            ):
                tail = text[best_label_pos + len(best_label):].strip()
                allowed_list = field.get("allowed_values") or []
                for allowed_value in allowed_list:
                    if isinstance(allowed_value, str) and allowed_value and tail.startswith(allowed_value):
                        has_naked_concat_match = True
                        naked_concat_value = allowed_value
                        matches.append((best_label_pos + len(best_label), len(allowed_value), allowed_value))
                        break

            # 综合门槛：区分 LLM 是否返回了候选（SSOT 不变量 vs 规则兜底智能）
            if llm_returned_none:
                if has_field_ref:
                    threshold_ok = (
                        has_explicit_assign_syntax
                        or field_specific_implicit_ok
                        or tasktype_exact_reply_ok
                        or (has_naked_concat_match and field_recently_asked)
                    )
                else:
                    threshold_ok = (
                        has_explicit_assign_syntax
                        or field_specific_implicit_ok
                        or tasktype_exact_reply_ok
                        or field_recently_asked
                    )
            else:
                threshold_ok = has_field_ref or tasktype_exact_reply_ok
            if not threshold_ok:
                continue

            # 枚举 allowed_values 精确匹配（非数值型字段）
            for allowed_value in field.get("allowed_values") or []:
                if not isinstance(allowed_value, str) or not allowed_value:
                    continue
                if has_naked_concat_match and allowed_value == naked_concat_value:
                    continue
                pos = text.rfind(allowed_value)
                if pos >= 0:
                    matches.append((pos, len(allowed_value), allowed_value))

            # BUG#1 修复：water_depth 数值型字段兜底解析
            if (
                key == "water_depth"
                and not matches
                and number_match_value is not None
            ):
                try:
                    num_val = float(number_match_value)
                    int_val = int(num_val) if num_val.is_integer() else num_val
                    merged.append(
                        {
                            "raw_key": raw_label or key,
                            "canonical_key": key,
                            "raw_value": number_match_value,
                            "normalized_value": str(int_val),
                            "confidence": 0.95,
                            "resolution_method": "rule_number_with_unit",
                        }
                    )
                    existing_keys.add(key)
                    continue
                except (ValueError, TypeError):
                    pass

            if not matches:
                continue

            _, _, value = max(matches)
            merged.append(
                {
                    "raw_key": raw_label or key,
                    "canonical_key": key,
                    "raw_value": value,
                    "normalized_value": value,
                    "confidence": 1.0,
                    "resolution_method": (
                        "rule_naked_concat" if has_naked_concat_match and value == naked_concat_value
                        else "canonical_exact"
                    ),
                }
            )
            existing_keys.add(key)

        return merged

    @classmethod
    def _merge_explicit_datetime_candidates(
        cls,
        user_message: str,
        candidates: list[dict],
        required: list[dict],
        allowed_keys: set[str],
        now: datetime,
        conversation_history: list[dict] | None = None,
    ) -> list[dict]:
        """Recover explicitly labelled, unambiguous task times omitted by the LLM."""
        merged = list(candidates)
        existing_keys = {
            str(candidate.get("canonical_key") or "")
            for candidate in candidates
            if isinstance(candidate, dict)
        }
        required_by_key = {
            str(field.get("key") or ""): field
            for field in required
            if field.get("key")
        }

        label_aliases: dict[str, tuple[str, ...]] = {
            "start_time": ("任务开始时间", "开始时间", "启动时间", "起始时间", "任务起始时间", "开始"),
            "end_time": ("任务结束时间", "结束时间", "完成时间", "终止时间", "任务完成时间", "结束"),
        }

        day_seg = r"(?:今天|今日|明天|明日|后天|后日|大后天|大后天|本周[一二三四五六日天]|下周[一二三四五六日天]|[一二三四五六日天])?"
        time_seg = r"(?:上午|下午|中午|晚上|凌晨|早上|早晨|傍晚|午后)?"
        clock_seg = (
            r"(?:(?:\d{1,2}|[零〇一二两三四五六七八九十]+)[点时]"
            r"(?:半|(?:\d{1,2}|[零〇一二两三四五六七八九十]+)分|三刻|一刻)?)"
        )
        iso_seg = r"\d{4}-\d{1,2}-\d{1,2}(?:[T ]\d{1,2}:\d{2}(?::\d{2})?)?"
        rel_seg = r"(?:\d+|[零〇一二两三四五六七八九十]+)(?:个)?小时后"
        now_seg = r"现在|当前|立即|马上|立刻"
        value_pattern = rf"(?:{iso_seg}|{rel_seg}|{now_seg}|{day_seg}{time_seg}{clock_seg})"

        for key, aliases in label_aliases.items():
            field = required_by_key.get(key)
            if (
                key not in allowed_keys
                or key in existing_keys
                or not field
                or field.get("type") != "datetime"
            ):
                continue

            matched_alias: str | None = None
            match: re.Match | None = None
            text = str(user_message or "")
            matched_value_text: str | None = None
            for alias in aliases:
                if not alias or alias not in text:
                    continue
                # 模式 A：label + value（标准顺序，如"任务开始时间明天上午9点"）
                m_forward = re.search(
                    rf"{re.escape(alias)}\s*(?:为|是|[:：]|在|于|时间是)?\s*(?P<value>{value_pattern})",
                    text,
                    flags=re.IGNORECASE,
                )
                if m_forward:
                    match = m_forward
                    matched_alias = alias
                    matched_value_text = m_forward.group("value")
                    break
                # 模式 B：value + label（反向顺序，如"明天上午9点开始" / "后天中午12点结束"）
                # 只对简短 alias（≤4字）做反向匹配，避免把句末无关的时间词误匹配
                if len(alias) <= 4:
                    m_backward = re.search(
                        rf"(?P<value>{value_pattern})\s*{re.escape(alias)}(?!时间)",
                        text,
                        flags=re.IGNORECASE,
                    )
                    if m_backward:
                        match = m_backward
                        matched_alias = alias
                        matched_value_text = m_backward.group("value")
                        break

            # 追问场景放宽：没有匹配到 label，但该字段最近被 assistant 追问过 → 允许提取纯 value_pattern（裸值回答）
            if match is None:
                # 先尝试通用的 label/key 子串匹配
                recently_asked_generic = ParameterExtractor._is_recently_asked_field(
                    field, conversation_history
                )
                # 针对 start_time / end_time 的口语化追问宽松匹配
                recently_asked_semantic = False
                if conversation_history:
                    last_assistant_msg = None
                    for turn in reversed(conversation_history):
                        if isinstance(turn, dict) and turn.get("role") == "assistant":
                            c = turn.get("content")
                            if isinstance(c, str) and c.strip():
                                last_assistant_msg = c
                                break
                    if last_assistant_msg:
                        if key == "start_time":
                            start_pat = r"(?:任务|作业|本次)?.*(?:什么时候|何时|几点|什么时间|啥时候).*(?:开始|启动|执行|进行)|开始.*(?:时间|时候|几点)|(?:任务|作业)启动|(?:时间|时候).*(?:开始|启动)"
                            if re.search(start_pat, last_assistant_msg) or any(
                                w in last_assistant_msg for w in ("开始时间", "启动时间", "起始时间", "什么时候开始", "几点开始")
                            ):
                                recently_asked_semantic = True
                        elif key == "end_time":
                            end_pat = r"(?:任务|作业|本次)?.*(?:什么时候|何时|几点|什么时间|啥时候).*(?:结束|完成|终止)|结束.*(?:时间|时候|几点)|完成.*(?:时间|时候)|(?:任务|作业)完成|(?:时间|时候).*(?:结束|完成)"
                            if re.search(end_pat, last_assistant_msg) or any(
                                w in last_assistant_msg for w in ("结束时间", "完成时间", "终止时间", "什么时候结束", "几点结束", "什么时候完成")
                            ):
                                recently_asked_semantic = True
                if recently_asked_generic or recently_asked_semantic:
                    bare_match = re.search(rf"(?P<value>{value_pattern})", text)
                    if bare_match:
                        match = bare_match
                        matched_value_text = bare_match.group("value")

            if match is None or matched_value_text is None:
                continue

            raw_value = matched_value_text
            normalized_value = cls._normalize_explicit_datetime(raw_value, now)
            if normalized_value is None:
                continue

            merged.append(
                {
                    "raw_key": matched_alias or key,
                    "canonical_key": key,
                    "raw_value": raw_value,
                    "normalized_value": normalized_value,
                    "confidence": 1.0,
                    "resolution_method": "rule_explicit_datetime",
                }
            )
            existing_keys.add(key)

        return merged

    @classmethod
    def _normalize_explicit_datetime(cls, raw_value: str, now: datetime) -> str | None:
        text = str(raw_value or "").strip()
        local_now = now.replace(tzinfo=None, microsecond=0)
        if text in {"现在", "当前", "立即", "马上", "立刻"}:
            return local_now.isoformat(timespec="seconds")

        relative_match = re.fullmatch(
            r"(?P<hours>\d+|[零〇一二两三四五六七八九十]+)(?:个)?小时后",
            text,
        )
        if relative_match:
            hours = cls._parse_explicit_integer(relative_match.group("hours"))
            if hours is None:
                return None
            return (local_now + timedelta(hours=hours)).isoformat(timespec="seconds")

        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            parsed = None
        if parsed is not None:
            if parsed.tzinfo is not None:
                return None
            return parsed.replace(microsecond=0).isoformat(timespec="seconds")

        spoken = cls._parse_spoken_datetime(text, local_now)
        if spoken is not None:
            return spoken.isoformat(timespec="seconds")

        return None

    @classmethod
    def _parse_spoken_datetime(cls, text: str, local_now: datetime) -> datetime | None:
        text = text.strip()
        if not text:
            return None

        target_date = local_now.date()

        day_match = re.match(
            r"^(今天|今日|明天|明日|后天|后日|大后天)",
            text,
        )
        if day_match:
            day_text = day_match.group(1)
            offset = {
                "今天": 0, "今日": 0,
                "明天": 1, "明日": 1,
                "后天": 2, "后日": 2,
                "大后天": 3,
            }.get(day_text, 0)
            target_date = local_now.date() + timedelta(days=offset)
            text = text[len(day_match.group(0)):]
        else:
            week_match = re.match(r"^(?:本|下)?周(?P<wd>[一二三四五六日天])", text)
            if week_match:
                cn = week_match.group("wd")
                weekday_index = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}[cn]
                this_weekday = local_now.weekday()
                delta = (weekday_index - this_weekday) % 7
                prefix = text[:week_match.end(0)]
                if prefix.startswith("下"):
                    delta += 7
                target_date = local_now.date() + timedelta(days=delta)
                text = text[week_match.end(0):]

        period_hour_base = 0
        period_match = re.match(r"^(上午|下午|中午|晚上|凌晨|早上|早晨|傍晚|午后)", text)
        if period_match:
            period = period_match.group(1)
            period_map = {
                "凌晨": 0, "早上": 6, "早晨": 7, "上午": 8,
                "中午": 12, "午后": 13, "下午": 14, "傍晚": 17, "晚上": 19,
            }
            period_hour_base = period_map.get(period, 0)
            text = text[len(period):]

        clock_match = re.match(
            r"^(?P<hour>\d{1,2}|[零〇一二两三四五六七八九十]+)[点时]"
            r"(?P<min>半|三刻|一刻|(?:\d{1,2}|[零〇一二两三四五六七八九十]+)分)?$",
            text,
        )
        if not clock_match:
            return None
        hour_raw = clock_match.group("hour")
        min_raw = clock_match.group("min") or "0"

        hour = cls._parse_explicit_integer(hour_raw)
        if hour is None:
            return None
        if period_hour_base != 0 and hour < 12:
            if period_hour_base in (12, 13, 14, 17, 19) and hour < 12:
                hour = hour + 12 if period_hour_base != 12 else 12
            elif period_hour_base != 0:
                hour = period_hour_base if hour <= period_hour_base else hour

        if min_raw == "半":
            minute = 30
        elif min_raw == "三刻":
            minute = 45
        elif min_raw == "一刻":
            minute = 15
        elif min_raw == "0":
            minute = 0
        else:
            min_num_str = min_raw.rstrip("分")
            minute = cls._parse_explicit_integer(min_num_str) or 0

        if not (0 <= hour <= 23):
            return None
        if not (0 <= minute <= 59):
            return None

        return datetime(
            year=target_date.year,
            month=target_date.month,
            day=target_date.day,
            hour=hour,
            minute=minute,
            second=0,
        )

    @staticmethod
    def _parse_explicit_integer(text: str) -> int | None:
        if text.isdigit():
            return int(text)
        digits = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3,
                  "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
        if text == "十":
            return 10
        if "十" in text:
            if text.count("十") != 1:
                return None
            left, right = text.split("十", 1)
            if (left and left not in digits) or (right and right not in digits):
                return None
            tens = digits[left] if left else 1
            ones = digits[right] if right else 0
            return tens * 10 + ones
        return digits.get(text)

    def _normalize_candidates(
        self,
        candidates: list,
        allowed_keys: set[str],
        required: list[dict],
        current_state: dict,
        conversation_history: list[dict],
    ) -> tuple[list[dict], list[str]]:
        """校验候选结构；同一字段多次出现时保留最后一次修正。"""
        aliases = {
            "equipment_model": "equipment_type",
            "raw_oilfield_name": "oilfield_name",
        }
        required_by_key = {
            str(field.get("key")): field
            for field in required or []
            if field.get("key")
        }
        normalized_by_key: dict[str, dict] = {}
        unresolved: list[str] = []

        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue

            key = str(candidate.get("canonical_key") or "").strip()
            key = aliases.get(key, key)
            if not key or key not in allowed_keys:
                continue

            value = candidate.get("normalized_value")
            if value is None or value == "":
                continue

            try:
                confidence = float(candidate.get("confidence", 1.0))
            except (TypeError, ValueError):
                confidence = 0.0
            confidence = min(1.0, max(0.0, confidence))

            raw_value = candidate.get("raw_value", value)
            trusted_candidate = {
                "raw_key": str(candidate.get("raw_key") or key),
                "canonical_key": key,
                "raw_value": raw_value,
                "normalized_value": value,
                "confidence": confidence,
            }
            if candidate.get("resolution_method"):
                trusted_candidate["resolution_method"] = candidate["resolution_method"]
            resolved_candidate, unresolved_reason = self._resolve_candidate_value(
                trusted_candidate,
                required_by_key,
                allowed_keys,
                current_state,
                conversation_history,
            )
            if resolved_candidate is None:
                if unresolved_reason:
                    unresolved.append(unresolved_reason)
                continue

            canonical_k = resolved_candidate["canonical_key"]
            field_def = required_by_key.get(canonical_k)
            is_list_field = (field_def and field_def.get("type") == "list") or canonical_k == "payload"

            if is_list_field:
                val = resolved_candidate["normalized_value"]
                val_list = val if isinstance(val, list) else ([val] if val is not None else [])
                raw_v = resolved_candidate["raw_value"]
                raw_list = raw_v if isinstance(raw_v, list) else ([raw_v] if raw_v is not None else [])

                if canonical_k in normalized_by_key:
                    existing = normalized_by_key[canonical_k]
                    existing_vals = existing["normalized_value"] if isinstance(existing["normalized_value"], list) else [existing["normalized_value"]]
                    existing_raws = existing["raw_value"] if isinstance(existing["raw_value"], list) else [existing["raw_value"]]
                    for item in val_list:
                        if item not in existing_vals:
                            existing_vals.append(item)
                    for item in raw_list:
                        if item not in existing_raws:
                            existing_raws.append(item)
                    existing["normalized_value"] = existing_vals
                    existing["raw_value"] = existing_raws
                else:
                    resolved_candidate["normalized_value"] = val_list
                    resolved_candidate["raw_value"] = raw_list
                    normalized_by_key[canonical_k] = resolved_candidate
            else:
                normalized_by_key[canonical_k] = resolved_candidate

        return list(normalized_by_key.values()), unresolved

    def _resolve_candidate_value(
        self,
        candidate: dict,
        required_by_key: dict[str, dict],
        allowed_keys: set[str],
        current_state: dict,
        conversation_history: list[dict],
    ) -> tuple[dict | None, str | None]:
        """受约束字段解析：标准值 exact → alias exact → LLM 语义兜底 → 后端校验。"""
        key = str(candidate.get("canonical_key") or "")
        field_def = required_by_key.get(key)
        if not field_def or not field_def.get("allowed_values"):
            candidate.setdefault("resolution_method", "type_normalization")
            return candidate, None
        if field_def.get("type") == "list":
            candidate.setdefault("resolution_method", "type_normalization")
            return candidate, None

        for value in self._candidate_match_inputs(candidate):
            canonical = self._match_allowed_value(value, field_def.get("allowed_values") or [])
            if canonical is not None:
                resolved = dict(candidate)
                resolved["normalized_value"] = canonical
                resolved["resolution_method"] = "canonical_exact"
                return (
                    (resolved, None)
                    if self._validate_resolved_candidate(key, canonical, required_by_key, allowed_keys)
                    else (None, self._format_unresolved(candidate, "不属于当前合法候选"))
                )

        for value in self._candidate_match_inputs(candidate):
            canonical = self._match_alias_value(value, field_def)
            if canonical is not None:
                resolved = dict(candidate)
                resolved["normalized_value"] = canonical
                resolved["resolution_method"] = "alias_exact"
                return (
                    (resolved, None)
                    if self._validate_resolved_candidate(key, canonical, required_by_key, allowed_keys)
                    else (None, self._format_unresolved(candidate, "alias 指向的标准值不属于当前合法候选"))
                )

        semantic = self._resolve_candidate_semantically(
            candidate.get("raw_value", candidate.get("normalized_value")),
            key,
            list(required_by_key.values()),
            current_state,
            conversation_history,
        )
        if semantic:
            resolved_key = str(semantic.get("canonical_key") or "")
            canonical = semantic.get("canonical_value")
            if self._validate_resolved_candidate(
                resolved_key,
                canonical,
                required_by_key,
                allowed_keys,
            ):
                resolved = dict(candidate)
                resolved["canonical_key"] = resolved_key
                resolved["normalized_value"] = canonical
                resolved["confidence"] = self._coerce_confidence(
                    semantic.get("confidence"),
                    candidate.get("confidence", 1.0),
                )
                resolved["resolution_method"] = "llm_semantic"
                return resolved, None

        return None, self._format_unresolved(candidate, "无法唯一匹配当前合法候选")

    @staticmethod
    def _candidate_match_inputs(candidate: dict) -> list[object]:
        values = []
        for key in ("normalized_value", "raw_value"):
            value = candidate.get(key)
            if value is not None and value != "":
                values.append(value)
        return values

    @staticmethod
    def _match_allowed_value(value: object, allowed_values: list) -> object | None:
        needle = FieldNormalizer.make_match_key(value)
        if not needle:
            return None
        matches = [
            allowed
            for allowed in allowed_values
            if FieldNormalizer.make_match_key(allowed) == needle
        ]
        return matches[0] if len(matches) == 1 else None

    @staticmethod
    def _match_alias_value(value: object, field_def: dict) -> object | None:
        needle = FieldNormalizer.make_match_key(value)
        if not needle:
            return None
        matches = []
        for alias, canonical in (field_def.get("alias_mappings") or {}).items():
            if FieldNormalizer.make_match_key(alias) == needle:
                matches.append(canonical)
        return matches[0] if len(set(map(str, matches))) == 1 else None

    def _resolve_candidate_semantically(
        self,
        raw_value: object,
        proposed_key: str,
        required: list[dict],
        current_state: dict,
        conversation_history: list[dict],
    ) -> dict | None:
        candidate_fields = []
        for field in required:
            allowed = field.get("allowed_values") or []
            evidence = field.get("candidate_evidence") or []
            if not allowed:
                continue
            candidate_fields.append(
                {
                    "key": field.get("key"),
                    "label": field.get("label"),
                    "allowed_values": allowed,
                    "alias_mappings": field.get("alias_mappings") or {},
                    "ambiguous_aliases": field.get("ambiguous_aliases") or {},
                    "candidate_evidence": evidence,
                }
            )
        if not candidate_fields:
            return None

        payload = {
            "user_expression": raw_value,
            "proposed_field": proposed_key,
            "expected_fields": [field.get("key") for field in required if field.get("key")],
            "current_state": current_state,
            "candidate_fields": candidate_fields,
            "recent_history": [
                {
                    "role": item.get("role"),
                    "content": item.get("content"),
                }
                for item in (conversation_history or [])[-MAX_EXTRACTION_USER_HISTORY:]
                if item.get("role") in ("user", "assistant") and item.get("content")
            ],
        }
        messages = [
            {
                "role": "system",
                "content": (
                    "你是受约束的候选语义解析器，只能输出 JSON object。"
                    "请结合 aliases、ambiguous_aliases、candidate_evidence、当前状态和历史，"
                    "从 allowed_values 中选择唯一标准值；不能生成 allowed_values 之外的值。"
                    "输出格式："
                    "{\"matched\": true/false, \"canonical_key\": string|null, "
                    "\"canonical_value\": string|null, \"confidence\": number, \"reason\": string}"
                ),
            },
            {
                "role": "user",
                "content": json.dumps(payload, ensure_ascii=False),
            },
        ]
        result = self.llm.extract_json(messages, max_tokens=500)
        if not isinstance(result, dict) or not result.get("matched"):
            return None
        return result

    @staticmethod
    def _validate_resolved_candidate(
        key: str,
        value: object,
        required_by_key: dict[str, dict],
        allowed_keys: set[str],
    ) -> bool:
        if key not in allowed_keys:
            return False
        field_def = required_by_key.get(key)
        if not field_def:
            return False
        allowed_values = field_def.get("allowed_values") or []
        if allowed_values:
            return any(value == allowed for allowed in allowed_values)
        return value is not None and value != ""

    @staticmethod
    def _coerce_confidence(value: object, fallback: object = 1.0) -> float:
        try:
            confidence = float(value)
        except (TypeError, ValueError):
            confidence = float(fallback)
        return min(1.0, max(0.0, confidence))

    @staticmethod
    def _format_unresolved(candidate: dict, reason: str) -> str:
        key = candidate.get("canonical_key") or "未知字段"
        raw = candidate.get("raw_value", candidate.get("normalized_value", ""))
        return f"{key} 表达“{raw}”{reason}。"

    def _select_extraction_history(
        self,
        user_message: str,
        required: list[dict] | None,
        conversation_history: list[dict] | None,
    ) -> list[dict]:
        """仅在当前指令依赖上下文时提供有限的最近历史消息。"""
        if not self._needs_history_context(user_message, required):
            return []

        recent = []
        for message in (conversation_history or [])[-MAX_EXTRACTION_USER_HISTORY:]:
            role = message.get("role")
            content = str(message.get("content") or "").strip()
            if role in ("user", "assistant") and content:
                recent.append({"role": role, "content": content})
        return recent

    @classmethod
    def _needs_history_context(
        cls,
        user_message: str,
        required: list[dict] | None,
    ) -> bool:
        """按通用指代特征和 schema 字段线索判断当前消息是否依赖历史。"""
        text = str(user_message or "").strip()
        compact = re.sub(r"[\s，,。.!！?？、；;：:]", "", text)
        if not compact:
            return False

        contextual_patterns = (
            r"第[一二三四五六七八九十百\d]+(?:个|项|条|台)?",
            r"(?:选|选择)[一二三四五六七八九十百\d]+",
            r"^(?:[一二三四五六七八九十百\d]+)(?:个|项|条|台)?$",
            r"(?:这个|那个|刚才的|之前的|原来的|上一个|下一个|前者|后者|同上|照旧)",
        )
        if any(
            re.search(pattern, compact, flags=re.IGNORECASE)
            for pattern in contextual_patterns
        ):
            return True

        field_cues = cls._build_field_cues(required)
        if any(cue in compact for cue in field_cues):
            return False

        return bool(required) and len(compact) <= 20

    @staticmethod
    def _build_field_cues(required: list[dict] | None) -> set[str]:
        """从 schema 元数据生成字段线索，不维护业务字段特判表。"""
        cues = set()
        for field in required or []:
            key = re.sub(r"\s+", "", str(field.get("key") or ""))
            label = re.sub(r"\s+", "", str(field.get("label") or ""))
            label = re.sub(r"[（(].*?[）)]", "", label)
            variants = {key, label}
            for prefix in ("任务", "作业", "具体", "当前"):
                if label.startswith(prefix):
                    variants.add(label[len(prefix):])
            for suffix in ("编号", "名称", "类型", "经纬度"):
                if label.endswith(suffix):
                    variants.add(label[:-len(suffix)])
            cues.update(value for value in variants if value)
        return cues

    def resolve_rov_description(
        self,
        description: str,
        all_rovs: list[dict],
        task_type_key: str | None,
    ) -> list[dict]:
        rov_list_text = json.dumps(
            [
                {
                    "model": r["model"],
                    "full_name": r["full_name"],
                    "category": r["category"],
                    "max_depth_m": r["max_depth_m"],
                    "brief": r["brief"],
                    "aliases": r.get("aliases", []),
                }
                for r in all_rovs
            ],
            ensure_ascii=False,
        )

        constraint_hint = ""
        if task_type_key:
            capability_union: set[str] = set()
            for rov in all_rovs:
                capability_union.update(rov.get("capabilities") or [])
            if capability_union:
                constraint_hint = f"注意：该任务候选设备必须从给定列表中按能力匹配，候选能力包括：{'、'.join(sorted(capability_union))}。"

        system = f"""\
你是ROV设备匹配专家。根据用户描述，从给定设备列表中找出最匹配的ROV（最多3个），
优先考虑名称/型号匹配（包括拼写纠错），其次考虑功能描述匹配。{constraint_hint}
所有设备能力、最大水深、载荷、尺寸、类别和别名只能依据下方设备列表，不得使用通用知识或训练记忆补全。
如果设备列表未提供某项能力，不要据此编造匹配理由。

设备列表：
{rov_list_text}

只返回 JSON 数组，包含匹配设备的 model 字段，按匹配度降序排列：
["model1", "model2", ...]
如无匹配返回：[]
"""
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": f"用户描述：{description}"},
        ]
        raw = self.llm.generate(messages, temperature=0.1, max_tokens=100)

        import re
        match = re.search(r"\[.*?\]", raw, re.DOTALL)
        if not match:
            return []
        try:
            model_names = json.loads(match.group())
            return [r for name in model_names for r in all_rovs if r["model"] == name]
        except Exception:
            return []
