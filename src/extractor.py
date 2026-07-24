"""
extractor.py — 参数提取器
每轮对话后，用 LLM 从最新用户消息中提取或更新任务参数。
使用低温度、结构化 prompt，返回严格的结构化候选列表。
"""

import json
import re
from datetime import date

from .llm_client import LLMClient
from .normalizer import FieldNormalizer


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
      "raw_value": "300米",
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
   例如用户说"晚上八点开始，不不现在开始，算了还是明天上午开始吧"，start_time 应以最后的"明天上午开始"为准。
5. 对于时间信息：将口语时间转换为 YYYY-MM-DDTHH:MM:SS 格式，无时间部分时补 T00:00:00；"现在/当前/立即"等表达必须基于【当前时间】换算。
6. 对于坐标：normalized_value 提取为 {{"lat": float, "lon": float}} 格式，统一十进制度。以下形式都要识别为坐标：
   - (19.8,113.5)、19.8,113.5、19.8，113.5、19.8 113.5
   - 北纬19.8，东经113.5、纬度19.8，经度113.5、lat 19.8 lon 113.5
   - 十九点八，一百一十三点五、北纬十九点八，东经一百一十三点五
   - 未明确标注经纬度时，默认前一个数是 lat，后一个数是 lon。
7. 对于水深：统一转换为米（m）为单位的数值，例如"1千米"→1000，"500m"→500。
   用户说"水深300米"、"深度300"、"作业水深是300"、"300米水深"时，输出 normalized_value: "300"。
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


def _build_task_type_rules(task_type_map: dict[str, str]) -> str:
    groups: dict[str, list[str]] = {}
    for display, tkey in task_type_map.items():
        groups.setdefault(tkey, []).append(display)

    lines = []
    for tkey, values in groups.items():
        values_str = " / ".join(f'"{v}"' for v in values)
        lines.append(
            f'   - 识别为 {tkey} 类任务时 → task_type_key: "{tkey}"，'
            f'并根据用户描述推断 task_type 为 {values_str} 其中之一'
        )
    lines.append(
        '   - 无法确定具体 task_type 值时只输出 task_type_key，'
        '不要猜测或伪造 task_type'
    )
    lines.append(
        '   - 用户描述的任务类型不在上述范围内时，不提取任何 task_type 字段'
    )
    return "\n".join(lines)


class ParameterExtractor:
    def __init__(self, llm: LLMClient):
        self.llm = llm

    def extract_updates(
        self,
        user_message: str,
        current_state: dict,
        task_type_key: str | None,
        task_type_map: dict[str, str] | None = None,
        required: list[dict] | None = None,
        ROV2type: list[dict] | None = None,
        conversation_history: list[dict] | None = None,
    ) -> dict:
        from .simulated_time import get_current_datetime
        now = get_current_datetime()
        today_str = now.isoformat()

        known = {k: v for k, v in current_state.items() if v is not None}
        task_type_rules = _build_task_type_rules(task_type_map or {})

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

        result = self.llm.extract_json(messages, max_tokens=800)

        default_res = {"slot_candidates": [], "unresolved": []}
        if not result or not isinstance(result, dict):
            return default_res

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

        normalized_candidates, resolver_unresolved = self._normalize_candidates(
            raw_candidates,
            allowed_keys,
            required or [],
            current_state,
            conversation_history or [],
        )

        return {
            "slot_candidates": normalized_candidates,
            "unresolved": [
                str(item).strip()
                for item in [*unresolved, *resolver_unresolved]
                if str(item).strip()
            ],
        }

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
            }
        )
        return keys

    def _normalize_candidates(
        self,
        candidates: list,
        allowed_keys: set[str],
        required: list[dict],
        current_state: dict,
        conversation_history: list[dict],
    ) -> tuple[list[dict], list[str]]:
        """校验候选结构；同一字段多次出现时保留最后一次修正。"""
        aliases = {"equipment_model": "equipment_type"}
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

            normalized_by_key[resolved_candidate["canonical_key"]] = resolved_candidate

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
        if task_type_key == "pipeline_inspection":
            constraint_hint = "注意：该任务必须使用观察级ROV（category=observation）。"
        elif task_type_key == "tree_valve_operation":
            constraint_hint = "注意：该任务必须使用工作级ROV（category=work）。"

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
