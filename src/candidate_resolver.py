"""
src/candidate_resolver.py — 实体候选消歧、口语化清洗与语义匹配器

职责：
1. 口语化前缀与后缀清洗 (strip_colloquial_prefixes, strip_colloquial_suffixes)；
2. 助手回复有序编号解析与用户序号映射 (extract_numbered_options, match_numbered_option)；
3. 枚举允许值与别名表精确/模糊匹配 (match_allowed_value, match_alias_value)；
4. 数值型字段容错清洗（中文数字解析、单位换算、口语时长换算）；
5. LLM 受限语义兜底与候选消歧 (resolve_candidate_semantically, resolve_allowed_candidate)；
6. ROV 设备多候选描述匹配 (resolve_rov_description)。
"""

from __future__ import annotations

import json
import re
from typing import Any

from .duration_parser import parse_chinese_number
from .model_profile import ModelRole, _is_unsupported_role_keyword_error
from .normalizer import FieldNormalizer


def parse_relative_datetime(*args: Any, **kwargs: Any) -> Any:
    """动态感知外部针对 src.extractor.parse_relative_datetime 的 monkey-patch"""
    import sys
    ext_mod = sys.modules.get("src.extractor")
    if ext_mod is not None:
        prd = getattr(ext_mod, "parse_relative_datetime", None)
        if prd is not None and callable(prd):
            # 避免自递归：若 ext_mod.parse_relative_datetime 指向当前函数，跳过
            if prd is not parse_relative_datetime:
                return prd(*args, **kwargs)
    from .relative_time_parser import parse_relative_datetime as _prd
    return _prd(*args, **kwargs)


MAX_EXTRACTION_USER_HISTORY = 6

CANDIDATE_RESOLUTION_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "matched": {"type": "boolean"},
        "canonical_key": {"type": ["string", "null"]},
        "canonical_value": {"type": ["string", "null"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "reason": {"type": "string"},
    },
    "required": [
        "matched",
        "canonical_key",
        "canonical_value",
        "confidence",
        "reason",
    ],
    "additionalProperties": False,
}


class CandidateResolver:
    """实体候选消歧、口语化清洗与语义匹配器"""

    def __init__(self, llm: Any = None):
        self.llm = llm

    # ──────────────────────────────────────────────────────────────────────────
    # 静态清洗与文本正则匹配工具
    # ──────────────────────────────────────────────────────────────────────────

    @staticmethod
    def strip_colloquial_prefixes(raw_text: str) -> str:
        """剥离口语化修改与选择前缀"""
        text = str(raw_text or "").strip()
        prefixes = [
            "把支持船改成", "把船只改成", "把船改成", "把设备改成", "把管缆改成", "把油田改成",
            "把工具改成", "把载荷改成", "把水深改成", "把任务改成", "把水深调整为",
            "我要使用", "我要选择", "我想使用", "我想选择", "请选择", "请使用",
            "选择", "要用", "使用", "切换为", "采用", "配置", "指定", "选", "用", "换成",
            "更换为", "把", "调整为", "设为", "设置成", "修改为", "改成", "改用", "切换至",
            "选用", "更改为", "重新选择", "替换为", "重置为"
        ]
        for p in prefixes:
            if text.startswith(p) and len(text) > len(p):
                return text[len(p):].strip()
        return text

    @staticmethod
    def strip_colloquial_suffixes(raw_text: str) -> str:
        """剥离常见实体类型口语后缀"""
        text = str(raw_text or "").strip()
        suffixes = [
            "号船", "船只", "号", "管道", "电缆", "通信缆", "油田", "区域", "作业点",
            "位置", "探头", "传感器", "工具", "设备"
        ]
        for s in suffixes:
            if text.endswith(s) and len(text) > len(s):
                return text[:-len(s)].strip()
        return text

    @classmethod
    def candidate_match_inputs(cls, candidate: dict) -> list[object]:
        """为候选值生成多变体输入（原值、剥离前缀、剥离后缀）"""
        values = []
        for key in ("normalized_value", "raw_value"):
            value = candidate.get(key)
            if value is not None and value != "":
                val_str = str(value)
                if val_str not in values:
                    values.append(val_str)
                p_stripped = cls.strip_colloquial_prefixes(val_str)
                if p_stripped not in values:
                    values.append(p_stripped)
                s_stripped = cls.strip_colloquial_suffixes(p_stripped)
                if s_stripped not in values:
                    values.append(s_stripped)
        return values

    @classmethod
    def match_allowed_value(cls, value: object, allowed_values: list) -> object | None:
        """在白名单允许值列表中执行精确与子串容错匹配"""
        raw_str = str(value or "").strip()
        if not raw_str:
            return None
        candidates = [raw_str]
        p_stripped = cls.strip_colloquial_prefixes(raw_str)
        if p_stripped not in candidates:
            candidates.append(p_stripped)
        s_stripped = cls.strip_colloquial_suffixes(p_stripped)
        if s_stripped not in candidates:
            candidates.append(s_stripped)

        expanded_candidates = list(candidates)
        for cand in candidates:
            if not cand:
                continue
            converted_ji = re.sub(r'(\d+|[零〇一二两三四五六七八九十]+)\s*(?:号\s*)?级$', r'\1号机', cand)
            if converted_ji != cand and converted_ji not in expanded_candidates:
                expanded_candidates.append(converted_ji)
        candidates = expanded_candidates

        # 阶段 1：精确全匹配
        for cand in candidates:
            needle = FieldNormalizer.make_match_key(cand)
            if not needle:
                continue
            matches = [
                allowed for allowed in allowed_values
                if FieldNormalizer.make_match_key(allowed) == needle
            ]
            if len(matches) == 1:
                return matches[0]

        # 阶段 2：包含与子串容错匹配
        for cand in candidates:
            needle = FieldNormalizer.make_match_key(cand)
            if not needle:
                continue
            matches = [
                allowed for allowed in allowed_values
                if FieldNormalizer.make_match_key(allowed) and (
                    FieldNormalizer.make_match_key(allowed) in needle or
                    needle in FieldNormalizer.make_match_key(allowed)
                )
            ]
            if len(set(matches)) == 1:
                return matches[0]

        return None

    @classmethod
    def match_alias_value(cls, value: object, field_def: dict) -> object | None:
        """在字段定义的别名映射表 (alias_mappings) 中执行精确与子串容错匹配"""
        raw_str = str(value or "").strip()
        if not raw_str:
            return None
        alias_map = field_def.get("alias_mappings") or {}
        if not alias_map:
            return None

        candidates = [raw_str]
        p_stripped = cls.strip_colloquial_prefixes(raw_str)
        if p_stripped not in candidates:
            candidates.append(p_stripped)
        s_stripped = cls.strip_colloquial_suffixes(p_stripped)
        if s_stripped not in candidates:
            candidates.append(s_stripped)

        expanded_candidates = list(candidates)
        for cand in candidates:
            if not cand:
                continue
            # 常见 ASR 同音错字与量词变体转换（如 "2号级" -> "2号机", "2级" -> "2号机"）
            converted_ji = re.sub(r'(\d+|[零〇一二两三四五六七八九十]+)\s*(?:号\s*)?级$', r'\1号机', cand)
            if converted_ji != cand and converted_ji not in expanded_candidates:
                expanded_candidates.append(converted_ji)
            converted_hao = re.sub(r'(\d+|[零〇一二两三四五六七八九十]+)\s*级$', r'\1号', cand)
            if converted_hao != cand and converted_hao not in expanded_candidates:
                expanded_candidates.append(converted_hao)
        candidates = expanded_candidates

        # 阶段 1：精确全匹配
        for cand in candidates:
            needle = FieldNormalizer.make_match_key(cand)
            if not needle:
                continue
            matches = [
                canonical for alias, canonical in alias_map.items()
                if FieldNormalizer.make_match_key(alias) == needle
            ]
            if len(set(map(str, matches))) == 1:
                return matches[0]

        # 阶段 2：包含与子串容错匹配（针对口语修饰如 "选择天鹰座"、"天鹰座ROV"）
        for cand in candidates:
            needle = FieldNormalizer.make_match_key(cand)
            if not needle:
                continue
            matches = [
                canonical for alias, canonical in alias_map.items()
                if FieldNormalizer.make_match_key(alias) and (
                    FieldNormalizer.make_match_key(alias) in needle or
                    needle in FieldNormalizer.make_match_key(alias)
                )
            ]
            if len(set(map(str, matches))) == 1:
                return matches[0]

        return None

    @staticmethod
    def extract_numbered_options_from_assistant_message(
        conversation_history: list[dict],
    ) -> list[str]:
        """从紧邻上一条 assistant 消息中提取有序编号选项列表"""
        if not conversation_history:
            return []
        last_assistant_msg = None
        for msg in reversed(conversation_history):
            if isinstance(msg, dict) and msg.get("role") == "assistant":
                last_assistant_msg = str(msg.get("content") or "").strip()
                break
        if not last_assistant_msg:
            return []

        lines = last_assistant_msg.splitlines()
        options = []
        for line in lines:
            line_str = line.strip()
            cleaned = re.sub(r"^\(?\[?\d+\]?\)?|^\u2460-\u2473", "", line_str).strip()
            cleaned = cleaned.lstrip(".:：、． ").strip()
            if cleaned and cleaned != line_str:
                opt_text = re.split(r"[:：(（]", cleaned)[0].strip()
                if opt_text:
                    options.append(opt_text)
        return options

    @staticmethod
    def match_numbered_option_by_user_input(
        raw_value: object,
        shown_options: list[str],
    ) -> str | None:
        """根据用户输入的数字序号（如 '2', '第2个', '选1'），直接在编号选项中查找匹配"""
        if not shown_options or raw_value is None:
            return None

        val_str = str(raw_value).strip()
        m = re.search(r"(?:第|选|选择)?\s*([1-9][0-9]*)\s*(?:个|项|号)?", val_str)
        if m:
            try:
                idx = int(m.group(1)) - 1
                if 0 <= idx < len(shown_options):
                    return shown_options[idx]
            except ValueError:
                pass
        return None

    @staticmethod
    def clean_numeric_candidate(
        candidate: dict,
        key: str,
        field_def: dict,
    ) -> dict:
        """数值型字段容错清洗：中文数字转换、单位转换与口语时长换算"""
        f_type = field_def.get("type")
        if f_type in ("number", "integer", "float") or key in ("water_depth", "distance", "speed", "duration", "duration_seconds"):
            val = candidate.get("normalized_value")
            raw_v = candidate.get("raw_value")
            target_str = str(val if (isinstance(val, str) and val) else (raw_v or "")).strip()

            if target_str and not target_str.lstrip("-+").replace(".", "", 1).isdigit():
                raw_lower = target_str.lower()
                valid_units = ("英尺", "feet", "ft", "千米", "公里", "km", "米", "m", "节", "knot", "kn", "小时", "钟头", "hour", "hr", "分钟", "分", "min", "天", "日", "day")
                is_known_unit = any(u in raw_lower for u in valid_units) or any(cn in target_str for cn in ("一", "二", "两", "三", "四", "五", "六", "七", "八", "九", "十", "百", "千", "万", "半"))

                m_num = re.match(r"^([-+]?[0-9]+(?:\.[0-9]+)?)\s*([a-zA-Z\u4e00-\u9fa5]*)$", target_str.strip())
                clean_str = None
                unit_part = ""
                if m_num:
                    clean_str, unit_part = m_num.groups()
                else:
                    m_cn = re.match(
                        r"^([-+]?[零〇○Oo幺壹贰两俩叁仨肆伍陆柒捌玖勾一二三四五六七八九十拾佰仟万萬亿点半\d]+(?:点半|[零〇○Oo幺壹贰两俩叁仨肆伍陆柒捌玖勾一二三四五六七八九十百千万萬亿\d半]*半?)?)\s*([a-zA-Z\u4e00-\u9fa5]*)$",
                        target_str.strip(),
                    )
                    if m_cn:
                        cn_num_part, unit_part = m_cn.groups()
                        parsed_num = parse_chinese_number(cn_num_part)
                        if parsed_num is not None:
                            clean_str = str(parsed_num)

                if clean_str is not None:
                    unit_part_lower = unit_part.lower()
                    has_valid_unit = not unit_part or is_known_unit or any(u in unit_part_lower for u in valid_units)
                    if has_valid_unit:
                        try:
                            num_val = float(clean_str)
                            # 单位换算：英尺 (ft / feet / 英尺) -> 米 (m)
                            if any(unit in raw_lower for unit in ("英尺", "feet", "ft")) and key in ("water_depth", "distance"):
                                num_val = round(num_val * 0.3048, 2)
                            # 单位换算：千米/公里 (km) -> 米 (m)
                            elif any(unit in raw_lower for unit in ("千米", "公里", "km")) and key in ("water_depth", "distance"):
                                num_val = round(num_val * 1000.0, 2)
                            # 单位换算：节 (knots / kn / 节速) -> m/s
                            elif any(unit in raw_lower for unit in ("节", "knot", "kn")) and key in ("speed", "velocity"):
                                num_val = round(num_val * 0.5144, 2)
                            # 口语时长转换：例如 "2.5个小时" / "两个半小时" / "3天" -> 秒
                            elif key in ("duration", "duration_seconds"):
                                if "个半小时" in target_str or "点半小时" in target_str:
                                    num_val = (num_val + 0.5) * 3600.0
                                elif "半小时" in target_str:
                                    num_val = 1800.0
                                elif any(h in raw_lower for h in ("小时", "钟头", "hour", "hr", "h")):
                                    num_val = num_val * 3600.0
                                elif any(m in raw_lower for m in ("分钟", "分", "min", "m")):
                                    num_val = num_val * 60.0
                                elif any(d in raw_lower for d in ("天", "日", "day", "d")):
                                    num_val = num_val * 86400.0

                            clean_val = int(num_val) if (num_val.is_integer() and f_type != "float") else num_val
                            candidate = dict(candidate)
                            candidate["normalized_value"] = clean_val
                        except ValueError:
                            pass
        return candidate

    @staticmethod
    def validate_resolved_candidate(
        key: str,
        value: object,
        required_by_key: dict[str, dict],
        allowed_keys: set[str],
    ) -> bool:
        """核验最终消歧后的值是否属于白名单或符合合法非空规范"""
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
    def coerce_confidence(value: object, fallback: object = 1.0) -> float:
        try:
            confidence = float(value)
        except (TypeError, ValueError):
            confidence = float(fallback)
        return min(1.0, max(0.0, confidence))

    @staticmethod
    def format_unresolved(candidate: dict, reason: str) -> str:
        key = candidate.get("canonical_key") or "未知字段"
        raw = candidate.get("raw_value", candidate.get("normalized_value", ""))
        return f"{key} 表达“{raw}”{reason}。"

    # ──────────────────────────────────────────────────────────────────────────
    # 候选消歧核心业务方法
    # ──────────────────────────────────────────────────────────────────────────

    def resolve_candidate_value(
        self,
        candidate: dict,
        required_by_key: dict[str, dict],
        allowed_keys: set[str],
        current_state: dict,
        conversation_history: list[dict],
        user_message: str = "",
    ) -> tuple[dict | None, str | None]:
        """受约束字段解析：相对日期确定性校正 → 编号选项精确映射 → 标准值 exact → alias exact → LLM 语义兜底 → 后端校验"""
        key = str(candidate.get("canonical_key") or "")

        # 1. 相对时间口语确定性校正
        if key in ("start_time", "end_time") and candidate.get("resolution_method") not in ("duration_arithmetic", "cross_day_auto_corrected"):
            existing_norm = candidate.get("normalized_value")
            if isinstance(existing_norm, str) and re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", existing_norm.strip()):
                return candidate, None

            raw_text = str(candidate.get("raw_value") or candidate.get("normalized_value") or "").strip()
            from .simulated_time import get_current_datetime
            rel_iso = parse_relative_datetime(raw_text, get_current_datetime(), full_user_message=user_message)
            if not rel_iso and user_message:
                rel_iso = parse_relative_datetime(user_message, get_current_datetime(), full_user_message=user_message)
            if rel_iso:
                resolved = dict(candidate)
                resolved["normalized_value"] = rel_iso
                resolved["resolution_method"] = "relative_date_parsed"
                return resolved, None

        # 1.5 数值型字段容错清洗
        field_def = required_by_key.get(key) or {}
        candidate = self.clean_numeric_candidate(candidate, key, field_def)

        if not field_def or not field_def.get("allowed_values"):
            candidate.setdefault("resolution_method", "type_normalization")
            return candidate, None
        if field_def.get("type") == "list":
            candidate.setdefault("resolution_method", "type_normalization")
            return candidate, None

        # 2. 编号选项确定性映射 (例如用户回复 "2" 或 "第2个")
        shown_options = self.extract_numbered_options_from_assistant_message(conversation_history)
        if shown_options:
            raw_val = candidate.get("raw_value", candidate.get("normalized_value"))
            index_matched = self.match_numbered_option_by_user_input(raw_val, shown_options)
            if index_matched:
                canonical = self.match_allowed_value(index_matched, field_def.get("allowed_values") or [])
                if canonical is None:
                    canonical = self.match_alias_value(index_matched, field_def)
                if canonical is not None:
                    resolved = dict(candidate)
                    resolved["normalized_value"] = canonical
                    resolved["resolution_method"] = "option_index_exact"
                    return (
                        (resolved, None)
                        if self.validate_resolved_candidate(key, canonical, required_by_key, allowed_keys)
                        else (None, self.format_unresolved(candidate, "编号对应的标准值不属于当前合法候选"))
                    )

        # 3. 标准值精确匹配
        for value in self.candidate_match_inputs(candidate):
            canonical = self.match_allowed_value(value, field_def.get("allowed_values") or [])
            if canonical is not None:
                resolved = dict(candidate)
                resolved["normalized_value"] = canonical
                resolved["resolution_method"] = "canonical_exact"
                return (
                    (resolved, None)
                    if self.validate_resolved_candidate(key, canonical, required_by_key, allowed_keys)
                    else (None, self.format_unresolved(candidate, "不属于当前合法候选"))
                )

        # 4. 别名精确匹配
        for value in self.candidate_match_inputs(candidate):
            canonical = self.match_alias_value(value, field_def)
            if canonical is not None:
                resolved = dict(candidate)
                resolved["normalized_value"] = canonical
                resolved["resolution_method"] = "alias_exact"
                return (
                    (resolved, None)
                    if self.validate_resolved_candidate(key, canonical, required_by_key, allowed_keys)
                    else (None, self.format_unresolved(candidate, "alias 指向的标准值不属于当前合法候选"))
                )

        # 5. LLM 语义消歧兜底
        semantic = self.resolve_candidate_semantically(
            candidate.get("raw_value", candidate.get("normalized_value")),
            key,
            list(required_by_key.values()),
            current_state,
            conversation_history,
        )
        if semantic:
            resolved_key = str(semantic.get("canonical_key") or "")
            canonical = semantic.get("canonical_value")
            if self.validate_resolved_candidate(
                resolved_key,
                canonical,
                required_by_key,
                allowed_keys,
            ):
                resolved = dict(candidate)
                resolved["canonical_key"] = resolved_key
                resolved["normalized_value"] = canonical
                resolved["confidence"] = self.coerce_confidence(
                    semantic.get("confidence"),
                    candidate.get("confidence", 1.0),
                )
                resolved["resolution_method"] = "llm_semantic"
                return resolved, None

        return None, self.format_unresolved(candidate, "无法唯一匹配当前合法候选")

    def resolve_candidate_semantically(
        self,
        raw_value: object,
        proposed_key: str,
        required: list[dict],
        current_state: dict,
        conversation_history: list[dict],
    ) -> dict | None:
        """调用 LLM 理解简称、错别字、模糊描述与指代，进行受限语义消歧"""
        if not self.llm:
            return None
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
                    "理解简称、错别字、模糊描述、用途偏好和上下文指代，不要求用户逐字"
                    "复述标准名称。若证据足以支持唯一候选，应主动完成语义映射；若多个"
                    "候选仍同样合理，则 matched=false，不能按列表顺序猜测。用户请求推荐"
                    "时，可以依据用途和偏好做相对选择：只要某个候选的证据明确覆盖这些"
                    "偏好、而其他候选没有对应证据，就应视为唯一支持并返回 matched=true；"
                    "不要因为用户没有主动说出标准名称，或没有提供全部任务参数而拒绝推荐。"
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
        result = self.llm.extract_json(
            messages,
            max_tokens=500,
            json_schema=CANDIDATE_RESOLUTION_JSON_SCHEMA,
        )
        if not isinstance(result, dict) or not result.get("matched"):
            return None
        return result

    def resolve_allowed_candidate(
        self,
        raw_value: object,
        field_key: str,
        field_def: dict,
        current_state: dict | None = None,
        conversation_history: list[dict] | None = None,
    ) -> object | None:
        """用模型理解模糊表达，但只返回当前字段的权威候选值"""
        allowed_values = list((field_def or {}).get("allowed_values") or [])
        if not field_key or not allowed_values:
            return None

        semantic = self.resolve_candidate_semantically(
            raw_value,
            field_key,
            [field_def],
            current_state or {},
            conversation_history or [],
        )
        if not semantic or semantic.get("canonical_key") != field_key:
            return None

        canonical = semantic.get("canonical_value")
        return next(
            (allowed for allowed in allowed_values if canonical == allowed),
            None,
        )

    def resolve_rov_description(
        self,
        description: str,
        all_rovs: list[dict],
        task_type_key: str | None,
    ) -> list[dict]:
        """根据用户自然语言描述，从已有设备列表中匹配最符合条件的 ROV 设备（最多3个）"""
        if not self.llm:
            return []
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

        del task_type_key
        model_names = [str(r["model"]) for r in all_rovs if r.get("model")]
        schema = {
            "type": "array",
            "items": {"type": "string", "enum": model_names},
            "maxItems": 3,
            "uniqueItems": True,
        }
        system = f"""\
你是ROV设备匹配专家。根据用户描述，从给定设备列表中找出最匹配的ROV（最多3个），
优先考虑名称、型号、别名和功能描述匹配。任务适用性由后端约束系统另行校验。
所有设备信息只能依据下方设备列表，不得使用通用知识或训练记忆补全。

设备列表：
{rov_list_text}

只返回按匹配度降序排列的 model JSON 数组；无匹配返回空数组。
"""
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": f"用户描述：{description}"},
        ]
        try:
            result = self.llm.extract_json(
                messages,
                max_tokens=100,
                role=ModelRole.EXTRACTOR,
                json_schema=schema,
            )
        except TypeError as exc:
            if not _is_unsupported_role_keyword_error(exc):
                raise
            result = self.llm.extract_json(messages, max_tokens=100)
        if not isinstance(result, list):
            return []
        by_model = {str(r.get("model")): r for r in all_rovs if r.get("model")}
        return [by_model[name] for name in result if name in by_model]
