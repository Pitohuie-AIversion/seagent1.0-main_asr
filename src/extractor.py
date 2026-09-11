"""
extractor.py — 参数提取器主调度门面 (解耦重构版)

职责：
1. 每轮对话后，用 LLM 从最新用户消息中提取或更新任务参数结构；
2. 调度 TemporalParser (src/temporal_parser.py) 进行相对时间、区间物化与时长算术推导；
3. 调度 CandidateResolver (src/candidate_resolver.py) 进行实体候选消歧、口语清洗与语义匹配；
4. 维持全套向后兼容公共 API 契约与方法代理。
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

from .candidate_resolver import (
    CandidateResolver,
    CANDIDATE_RESOLUTION_JSON_SCHEMA,
    MAX_EXTRACTION_USER_HISTORY,
)
from .duration_parser import (
    parse_chinese_number,
    parse_duration_spec,
)
from .llm_client import LLMClient
from .model_profile import ModelRole, _is_unsupported_role_keyword_error
from .normalizer import FieldNormalizer
from .relative_time_parser import parse_relative_datetime, parse_time_range
from .temporal_parser import TemporalParser

FULL_TURN_EXTRACTION_MAX_TOKENS = 1600

EXTRACTION_TASK = """\
你是一个严格的任务参数候选抽取器。

【极重要：输出边界】
你当前不是对话助手，而是结构化候选抽取器。
- 只允许输出一个 JSON object，不得输出任何自然语言解释。
- 必须严格遵守以下输出 JSON 结构。
- 可能提供的最近历史消息用于理解上下文；编号选择只能引用紧邻上一条 assistant 消息中明确展示的有序候选，不能利用 required/allowed_values 的后台顺序；只有最新 user 消息能授权本轮字段更新。
- 用户本轮明确接受上一轮助手给出的单一推荐时，可以从紧邻的上一条 assistant 消息复制被接受的推荐值；助手之前自行提到的值不能在没有本轮用户授权时写入。
- 上游计划已判定本轮包含任务状态变更；必须输出候选、列表变更、时长关系之一，无法落实时必须说明 unresolved，禁止四者同时为空。

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
  "list_mutations": [],
  "time_relation": null,
  "unresolved": []
}}

【提取规则】
1. 对于任务类型：
{task_type_rules}
2. 如果无法识别任何任务字段，不得猜测候选；将无法落实的变更写入 unresolved。
3. 只支持上述任务；用户明确描述了不支持的任务时，不提取 task_type，并把任务描述写入 unresolved。
4. 如果最新用户消息中对同一字段多次修正，以最后出现的候选为准。
5. 最新用户消息选择编号时，只能根据紧邻上一条 assistant 消息中明确编号展示的选项映射；即使 allowed_values 有固定顺序，只要该顺序未向用户展示就必须写入 unresolved。接受单一推荐时只能使用紧邻助手明确推荐的值；不得猜测。
6. 仅当用户明确提及无歧义的紧急词汇（如"紧急"、"加急"、"应急救援"、"应急抢修"等）时，才提取 canonical_key: "emergency_mode" 且 normalized_value: true。严禁因语气词、标点符号（如感叹号）或"赶紧/优先"等泛化词语擅自判断为紧急模式。
7. 若用户明确表示"取消紧急"、"非紧急"、"正常模式"、"按普通模式"、"不紧急"等，提取 canonical_key: "emergency_mode" 且 normalized_value: false。
8. 本阶段只允许输出 task_type、task_type_key、emergency_mode。
"""

EXTRACTION_SYSTEM = """\
你是一个严格的参数候选抽取器，专门从用户的自然语言中提取水下ROV作业任务参数。

【极重要：输出边界】
你当前不是对话助手，而是结构化候选抽取器。
- 只允许输出一个 JSON object，不得输出任何自然语言解释。
- 即使当前任务已确认、已发布、已锁定，只要用户本轮明确补充、修改或确认字段，也必须抽取为候选列表。
- 如果用户本轮没有任何字段更新，返回 slot_candidates 为空列表的 JSON。
- 可能提供的最近历史消息用于理解上下文；编号选择只能引用紧邻上一条 assistant 消息中明确展示的有序候选，不能利用 required/allowed_values 的后台顺序；只有最新 user 消息能授权本轮字段更新。
- 用户本轮明确接受上一轮助手给出的单一推荐时，可以从紧邻的上一条 assistant 消息复制被接受的推荐值；助手之前自行提到的值不能在没有本轮用户授权时写入。
- 上游计划已判定本轮包含任务状态变更；必须输出候选、列表变更、时长关系之一，无法落实时必须说明 unresolved，禁止四者同时为空。

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
  "list_mutations": [],
  "time_relation": null,
  "unresolved": []
}}

【提取规则】
1. 只抽取用户当前已明确表达的参数；如果用户未提及某字段，绝不要在 slot_candidates 中输出该字段。
2. 对于任务类型，必须同时提取 task_type 与 task_type_key 两个字段：
{task_type_rules}
3. 如果当前状态中已有某字段值，但用户在本轮给出了新的值（包括修改、订正、补充），必须提取新值。
4. 如果最新用户消息中对同一字段多次修正，以最后出现的候选为准。
5. 针对 required 声明的字段，只允许提取 required 中存在的 canonical_key 以及任务类型选择器（task_type, task_type_key）。
6. 如果用户的输入不是修改已有字段，而是提问、闲聊或确认，slot_candidates 返回空列表 []。
7. 【列表字段特别规则】对于 payload 字段：
   - 用户明确表达"增加/还要/再带/装载/搭载/配备/加装/添加 [工具]"时，输出 list_mutations: [{{"field": "payload", "action": "append", "items": ["工具名称"]}}]
   - 用户明确表达"删除/不要/卸下/去掉/移除/取消 [工具]"时，输出 list_mutations: [{{"field": "payload", "action": "remove", "items": ["工具名称"]}}]
   - 用户明确表达"清空/全不要/什么都不带/不带任何工具/全部卸下/清空工具"时，输出 list_mutations: [{{"field": "payload", "action": "clear", "items": []}}]
   - 用户明确列出完整工具清单且意图是全量替换时（如"只要A和B"、"改成带A和B"、"载荷设置为A、B"），输出 list_mutations: [{{"field": "payload", "action": "replace", "items": ["A", "B"]}}]
   - 产生 list_mutations 时，slot_candidates 中不要再输出 payload 候选。
8. 【时间区间与时长规则】对于 start_time / end_time / 持续时长：
   - 用户表达"两小时后开始"、"明天上午九点"等相对时间时，尝试根据今天日期 {today} 换算为绝对 ISO 时间 "YYYY-MM-DDTHH:MM:SS"。
   - 用户明确表达持续时长或时长增量变动时（如"干2小时"、"作业持续3天"、"时长再延长1小时"、"提前半小时结束"、"结束时间保持不变"），除尽量换算 end_time 外，必须在 time_relation 中输出：
     {{"has_duration": true, "raw_text": "用户时长原词", "duration_seconds": 换算秒数, "target": "duration/start_time/end_time", "action": "SET/ADD/SUB", "confidence": 0.95}}
9. 【设备选择器特别规则】ROV设备可根据以下关联信息进行辅助推导：
{ROV2type}
10. 【选项编号与单一推荐】最新用户消息选择编号时，只能根据紧邻上一条 assistant 消息中明确编号展示的选项映射；即使 allowed_values 有固定顺序，只要该顺序未向用户展示就必须写入 unresolved。接受单一推荐时只能使用紧邻助手明确推荐的值；不得猜测。
11. 【紧急模式识别】仅当用户明确提及"紧急"、"加急"、"应急"等词汇时提取 emergency_mode: true；明确取消时提取 emergency_mode: false。

当前任务状态：
{current_state}

字段规范定义（包含类型、合法值列表、别名映射与证据线索）：
{required}
"""


def _build_task_type_rules(task_type_map: dict[str, str]) -> str:
    """根据任务类型映射字典生成 Prompt 规则文本"""
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


def _load_payload_catalog() -> dict:
    """容错加载载荷目录（兼容历史调用）"""
    return {}


class ParameterExtractor:
    """水下任务参数抽取器主门面调度器"""

    def __init__(self, llm: LLMClient):
        self.llm = llm
        self.temporal_parser = TemporalParser(llm)
        self.candidate_resolver = CandidateResolver(llm)

    # ──────────────────────────────────────────────────────────────────────────
    # 主抽取调度方法
    # ──────────────────────────────────────────────────────────────────────────

    def extract_updates(
        self,
        user_message: str,
        current_state: dict,
        task_type_key: str | None,
        task_type_map: dict[str, str] | None = None,
        required: list[dict] | None = None,
        ROV2type: list[dict] | None = None,
        conversation_history: list[dict] | None = None,
        allow_empty_for_side_effect: bool = False,
        allow_task_type_transition: bool = False,
    ) -> dict:
        from .simulated_time import get_current_datetime
        now = get_current_datetime()
        today_str = now.isoformat()

        known = {k: v for k, v in current_state.items() if v is not None}
        task_type_rules = _build_task_type_rules(task_type_map or {})
        extraction_required = required or []
        if allow_task_type_transition and task_type_key is not None:
            extraction_required = self._with_task_type_transition_values(
                extraction_required,
                task_type_map or {},
            )

        if task_type_key is None:
            system_prompt = EXTRACTION_TASK.format(
                task_type_rules=task_type_rules,
            )
        else:
            required_json = (
                json.dumps(extraction_required, ensure_ascii=False, indent=2)
                if extraction_required
                else "[]"
            )
            system_prompt = EXTRACTION_SYSTEM.format(
                today=today_str,
                current_state=json.dumps(known, ensure_ascii=False, indent=2),
                task_type_rules=task_type_rules,
                required=required_json,
                ROV2type=ROV2type,
            )

        if allow_empty_for_side_effect:
            system_prompt += (
                "\n\n【同轮次级动作探测】上游计划还声明了一个非字段副作用。"
                "仍须优先抽取最新用户消息中的全部任务字段；若确实没有任何任务字段，"
                "允许返回空 slot_candidates、空 list_mutations、null time_relation 和空 unresolved，"
                "由执行器随后处理该次级动作。不得为了满足非空要求伪造字段或 unresolved。"
            )

        extraction_context = self._select_extraction_history(
            user_message,
            extraction_required,
            conversation_history,
        )
        messages = [
            {"role": "system", "content": system_prompt},
            *extraction_context,
            {"role": "user", "content": user_message},
        ]

        try:
            result = self.llm.extract_json(
                messages,
                max_tokens=FULL_TURN_EXTRACTION_MAX_TOKENS,
                role=ModelRole.EXTRACTOR,
            )
        except TypeError as exc:
            if not _is_unsupported_role_keyword_error(exc):
                raise
            result = self.llm.extract_json(
                messages,
                max_tokens=FULL_TURN_EXTRACTION_MAX_TOKENS,
            )

        if not isinstance(result, dict):
            result = {}

        allowed_keys = self._allowed_candidate_keys(
            task_type_key,
            extraction_required,
        )
        raw_candidates = result.get("slot_candidates")
        if not isinstance(raw_candidates, list):
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

        # 调度 TemporalParser 处理时间关系与物化
        time_relation = result.get("time_relation")
        if time_relation is None and "end_time" in allowed_keys:
            time_relation = self.temporal_parser.extract_temporal_relation(
                user_message,
                current_state,
            )
        raw_candidates, time_unresolved = self.temporal_parser.materialize_time_relation(
            raw_candidates,
            time_relation,
            current_state,
            allowed_keys,
            user_message=user_message,
        )

        # 调度 CandidateResolver 进行候选清洗与规范化消歧
        normalized_candidates, resolver_unresolved = self._normalize_candidates(
            raw_candidates,
            allowed_keys,
            extraction_required,
            current_state,
            conversation_history or [],
            user_message=user_message,
        )

        raw_mutations = result.get("list_mutations", [])
        list_mutations = raw_mutations if isinstance(raw_mutations, list) else []
        mutation_unresolved = []

        if list_mutations:
            normalized_candidates = [
                cand for cand in normalized_candidates
                if cand.get("canonical_key") != "payload"
            ]

        selector_values: dict[str, object] = {}
        for candidate in normalized_candidates:
            key = candidate.get("canonical_key")
            if key not in {"task_type", "task_type_key"}:
                continue
            value = candidate.get("normalized_value")
            if key in selector_values and selector_values[key] != value:
                unresolved.append(
                    "同轮具体任务类型互相冲突，请只指定一种任务操作。"
                )
            else:
                selector_values[str(key)] = value

        all_unresolved = [
            *unresolved,
            *time_unresolved,
            *resolver_unresolved,
            *mutation_unresolved,
        ]

        return {
            "slot_candidates": normalized_candidates,
            "unresolved": [
                str(item).strip()
                for item in all_unresolved
                if str(item).strip()
            ],
            "list_mutations": list_mutations,
        }

    # ──────────────────────────────────────────────────────────────────────────
    # 候选过滤与消歧装配
    # ──────────────────────────────────────────────────────────────────────────

    def _with_task_type_transition_values(
        self,
        required: list[dict],
        task_type_map: dict[str, str],
    ) -> list[dict]:
        all_task_values = sorted(set(task_type_map.keys()))
        if not all_task_values:
            return required

        expanded: list[dict] = []
        task_field = None
        for item in required:
            copied = dict(item)
            if copied.get("key") == "task_type":
                task_field = copied
            expanded.append(copied)

        if task_field is None:
            expanded.append({
                "key": "task_type",
                "label": "任务类型",
                "type": "string",
                "required": True,
                "allowed_values": all_task_values,
            })
        else:
            task_field["allowed_values"] = all_task_values
        return expanded

    @staticmethod
    def _allowed_candidate_keys(
        task_type_key: str | None,
        required: list[dict],
    ) -> set[str]:
        if task_type_key is None:
            return {"task_type", "task_type_key", "emergency_mode"}

        keys = {
            str(field.get("key"))
            for field in required or []
            if field.get("key")
        }
        keys.update(
            {
                "task_type",
                "task_type_key",
                "emergency_mode",
                "equipment_class",
                "equipment_model",
                "rov_description",
                "equipment_name",
                "raw_oilfield_name",
                "oilfield_name",
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
        user_message: str = "",
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
        task_selector_candidates: list[dict] = []
        task_selector_values: dict[str, object] = {}
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
            resolution_method = candidate.get("resolution_method")
            if isinstance(resolution_method, str) and resolution_method.strip():
                trusted_candidate["resolution_method"] = resolution_method.strip()

            # 调度 CandidateResolver 解析单一候选值
            resolved_candidate, unresolved_reason = self.candidate_resolver.resolve_candidate_value(
                trusted_candidate,
                required_by_key,
                allowed_keys,
                current_state,
                conversation_history,
                user_message=user_message,
            )
            if resolved_candidate is None:
                if unresolved_reason:
                    unresolved.append(unresolved_reason)
                continue

            canonical_k = resolved_candidate["canonical_key"]
            if canonical_k in {"task_type", "task_type_key"}:
                selector_value = resolved_candidate.get("normalized_value")
                if canonical_k not in task_selector_values:
                    task_selector_values[canonical_k] = selector_value
                    task_selector_candidates.append(resolved_candidate)
                elif task_selector_values[canonical_k] != selector_value:
                    task_selector_candidates.append(resolved_candidate)
                continue

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

        return [
            *normalized_by_key.values(),
            *task_selector_candidates,
        ], unresolved

    def _select_extraction_history(
        self,
        user_message: str,
        required: list[dict] | None,
        conversation_history: list[dict] | None,
    ) -> list[dict]:
        """始终提供有界最近历史，让模型自行判断是否存在指代或省略。"""
        del user_message, required
        recent = []
        for message in (conversation_history or [])[-MAX_EXTRACTION_USER_HISTORY:]:
            role = message.get("role")
            content = str(message.get("content") or "").strip()
            if role in ("user", "assistant") and content:
                recent.append({"role": role, "content": content})
        return recent

    # ──────────────────────────────────────────────────────────────────────────
    # 向后兼容代理方法 (Delegated Proxies)
    # ──────────────────────────────────────────────────────────────────────────

    def _extract_temporal_relation(self, user_message: str, current_state: dict) -> dict | None:
        return self.temporal_parser.extract_temporal_relation(user_message, current_state)

    @staticmethod
    def _materialize_time_relation(
        candidates: list,
        relation: object,
        current_state: dict,
        allowed_keys: set[str],
        user_message: str = "",
    ) -> tuple[list, list[str]]:
        return TemporalParser.materialize_time_relation(candidates, relation, current_state, allowed_keys, user_message=user_message)

    @staticmethod
    def _parse_state_datetime(value: object) -> datetime | None:
        return TemporalParser.parse_state_datetime(value)

    @staticmethod
    def _looks_like_iso_datetime(value: object) -> bool:
        return TemporalParser.looks_like_iso_datetime(value)

    @staticmethod
    def _has_date_semantics(text: str) -> bool:
        return TemporalParser.has_date_semantics(text)

    @staticmethod
    def _mentions_end_time_keep(text: str) -> bool:
        return TemporalParser.mentions_end_time_keep(text)

    @staticmethod
    def _extract_duration_delta_text(text: str) -> str | None:
        return TemporalParser.extract_duration_delta_text(text)

    @staticmethod
    def _select_time_range_start_text(start_cand: dict | None, current_state: dict, user_message: str, base_dt: datetime) -> object:
        return TemporalParser.select_time_range_start_text(start_cand, current_state, user_message, base_dt)

    @staticmethod
    def _select_time_range_end_text(end_cand: dict | None, user_message: str, has_relation: bool) -> object:
        return TemporalParser.select_time_range_end_text(end_cand, user_message, has_relation)

    @staticmethod
    def _strip_colloquial_prefixes(raw_text: str) -> str:
        return CandidateResolver.strip_colloquial_prefixes(raw_text)

    @staticmethod
    def _strip_colloquial_suffixes(raw_text: str) -> str:
        return CandidateResolver.strip_colloquial_suffixes(raw_text)

    @classmethod
    def _candidate_match_inputs(cls, candidate: dict) -> list[object]:
        return CandidateResolver.candidate_match_inputs(candidate)

    @classmethod
    def _match_allowed_value(cls, value: object, allowed_values: list) -> object | None:
        return CandidateResolver.match_allowed_value(value, allowed_values)

    @classmethod
    def _match_alias_value(cls, value: object, field_def: dict) -> object | None:
        return CandidateResolver.match_alias_value(value, field_def)

    @staticmethod
    def _extract_numbered_options_from_assistant_message(conversation_history: list[dict]) -> list[str]:
        return CandidateResolver.extract_numbered_options_from_assistant_message(conversation_history)

    @staticmethod
    def _match_numbered_option_by_user_input(raw_value: object, shown_options: list[str]) -> str | None:
        return CandidateResolver.match_numbered_option_by_user_input(raw_value, shown_options)

    def _resolve_candidate_value(
        self,
        candidate: dict,
        required_by_key: dict[str, dict],
        allowed_keys: set[str],
        current_state: dict,
        conversation_history: list[dict],
        user_message: str = "",
    ) -> tuple[dict | None, str | None]:
        return self.candidate_resolver.resolve_candidate_value(
            candidate, required_by_key, allowed_keys, current_state, conversation_history, user_message=user_message
        )

    def _resolve_candidate_semantically(
        self,
        raw_value: object,
        proposed_key: str,
        required: list[dict],
        current_state: dict,
        conversation_history: list[dict],
    ) -> dict | None:
        return self.candidate_resolver.resolve_candidate_semantically(
            raw_value, proposed_key, required, current_state, conversation_history
        )

    def resolve_allowed_candidate(
        self,
        raw_value: object,
        field_key: str,
        field_def: dict,
        current_state: dict | None = None,
        conversation_history: list[dict] | None = None,
    ) -> object | None:
        return self.candidate_resolver.resolve_allowed_candidate(
            raw_value, field_key, field_def, current_state, conversation_history
        )

    def resolve_rov_description(
        self,
        description: str,
        all_rovs: list[dict],
        task_type_key: str | None,
    ) -> list[dict]:
        return self.candidate_resolver.resolve_rov_description(description, all_rovs, task_type_key)

    @staticmethod
    def _validate_resolved_candidate(key: str, value: object, required_by_key: dict[str, dict], allowed_keys: set[str]) -> bool:
        return CandidateResolver.validate_resolved_candidate(key, value, required_by_key, allowed_keys)

    @staticmethod
    def _coerce_confidence(value: object, fallback: object = 1.0) -> float:
        return CandidateResolver.coerce_confidence(value, fallback)

    @staticmethod
    def _format_unresolved(candidate: dict, reason: str) -> str:
        return CandidateResolver.format_unresolved(candidate, reason)
