"""
extractor.py — 参数提取器
每轮对话后，用 LLM 从最新用户消息中提取或更新任务参数。
使用低温度、结构化 prompt，返回严格的结构化候选列表。
"""

import json
from datetime import date
from typing import Any

from .llm_client import LLMClient

MUTATING_INTENTS = {"TASK_CREATE", "TASK_UPDATE"}
NON_MUTATING_INTENTS = {"GENERAL_CHAT", "UNKNOWN"}


def normalize_intent(value: Any) -> str:
    if not isinstance(value, str):
        return "UNKNOWN"
    normalized = value.strip().upper()
    if normalized in (MUTATING_INTENTS | NON_MUTATING_INTENTS):
        return normalized
    return "UNKNOWN"


MAX_EXTRACTION_USER_HISTORY = 6

EXTRACTION_TASK = """\
你是一个严格的任务识别与意图分类提取器。

【意图路由分类 (intent)】
- "TASK_CREATE" / "TASK_UPDATE": 用户欲创建或修改水下作业任务（管缆巡检、采油树控制面板插入、采油树控制面板拔出等）。
- "GENERAL_CHAT": 用户进行日常问候、询问系统功能/自我介绍（如"你好"、"你能做什么"、"介绍一下"、"海洋知识"等非任务参数提取对话）。
- "UNKNOWN": 用户表达完全不明确、无法识别意图。

【极重要：输出边界】
你当前不是对话助手，而是结构化候选抽取器。
- 只允许输出一个 JSON object，不得输出任何自然语言解释。
- 必须严格遵守以下输出 JSON 结构。

【输出格式】
{{
  "intent": "TASK_CREATE",
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
2. 若属于 GENERAL_CHAT 或 UNKNOWN，slot_candidates 为空列表 []，unresolved 为空列表 []。
3. 只支持上述提到的任务，如果不匹配，则返回空的 slot_candidates 列表，并把无法识别/无法匹配的任务输入写入 unresolved。
4. 如果最新用户消息中对同一字段出现多个候选或多次反悔/修正，以文本中最后出现的候选为准。
5. 如用户明确说任务紧急（"紧急"、"急"、"加急"等），提取 canonical_key: "emergency_mode" 且 normalized_value: true。
"""

EXTRACTION_SYSTEM = """\
你是一个严格的参数提取与意图分类器，专门从用户的自然语言中提取水下ROV作业任务参数。

【意图路由分类 (intent)】
- "TASK_UPDATE": 用户更新、修改或确认水下作业任务参数。
- "GENERAL_CHAT": 用户进行日常问候、询问系统功能（如"你好"、"你能做什么"、"介绍系统"等闲聊）。
- "UNKNOWN": 用户表达完全不明确。

【极重要：输出边界】
你当前不是对话助手，而是结构化候选抽取器。
- 只允许输出一个 JSON object，不得输出任何自然语言解释。
- 即使当前任务已确认、已发布、已锁定，只要用户本轮明确补充、修改或确认字段，也必须抽取为候选列表。
- 如果用户本轮没有任何字段更新，返回 slot_candidates 为空列表的 JSON。

【输出格式】
{{
  "intent": "TASK_UPDATE",
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
3. 最新用户消息优先：当前任务状态和历史对话只作为参考，不得覆盖最新用户消息中的明确修正。
4. 如果最新用户消息中对同一字段出现多个候选或多次反悔/修正，以文本中最后出现的候选为准。
5. 对于时间信息：将口语时间转换为 YYYY-MM-DDTHH:MM:SS 格式，无时间部分时补 T00:00:00；"现在/当前/立即"等表达必须基于【当前时间】换算。
6. 对于坐标：normalized_value 提取为 {{"lat": float, "lon": float}} 格式，统一十进制度。
7. 对于水深：统一转换为米（m）为单位的数值，例如"1千米"→1000，"500m"→500。
8. 对于任务类型：
{task_type_rules}
9. 对于ROV型号：如用户描述模糊（如"深水工作ROV"、"轻型观察"），提取 canonical_key: "rov_description" 字段，不要强行映射型号名。
10. 若确定ROV型号，可自动识别出ROV类型：{ROV2type}
11. 机器人能力、最大水深、载荷、功率、尺寸、状态、任务阈值和作业限制必须以所需字段、允许值、ROV2type和后续知识库/约束校验为准。
12. 如用户明确说任务紧急（"紧急"、"急"、"加急"等），提取 canonical_key: "emergency_mode" 且 normalized_value: true。
13. 无法识别的信息（包括用户提问的其他话题、闲聊、或不相关的数据）如果是属于任务维度的未识别字段，提取到 "unresolved" 数组中；如果是常规问候（如"你好"），路由为 "GENERAL_CHAT"，不要写入 unresolved。

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
        conversation_history: list[dict],
        current_state: dict,
        task_type_key: str | None,
        task_type_map: dict[str, str] | None = None,
        required: list[dict] | None = None,
        ROV2type: list[dict] | None = None,
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

        recent = self._build_extraction_history(conversation_history)
        messages = [
            {"role": "system", "content": system_prompt},
            *recent,
            {"role": "user", "content": user_message},
        ]

        print('extract prompt | '*10)
        result = self.llm.extract_json(messages, max_tokens=800)
        print('result in extract update | '*10)
        print(result)
        
        default_res = {"intent": "UNKNOWN", "slot_candidates": [], "unresolved": []}
        if not result or not isinstance(result, dict):
            return default_res
        
        # Ensure correct structure is returned
        if "slot_candidates" not in result:
            # Fallback if LLM output does not match structured format but is a flat JSON dict
            candidates = []
            for k, v in result.items():
                if k in ("intent", "unresolved"):
                    continue
                candidates.append({
                    "raw_key": k,
                    "canonical_key": k,
                    "raw_value": str(v),
                    "normalized_value": v,
                    "confidence": 1.0
                })
            result = {
                "intent": normalize_intent(result.get("intent")),
                "slot_candidates": candidates,
                "unresolved": result.get("unresolved", []) if isinstance(result.get("unresolved"), list) else []
            }
        else:
            result["intent"] = normalize_intent(result.get("intent"))
            if not isinstance(result.get("unresolved"), list):
                result["unresolved"] = []
        return result

    @staticmethod
    def _build_extraction_history(conversation_history: list[dict]) -> list[dict]:
        recent = []
        for msg in conversation_history[-6:]:
            role = msg.get("role")
            content = msg.get("content", "")
            if role in ("user", "assistant") and content:
                recent.append({"role": role, "content": content})
        return recent

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
