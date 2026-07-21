"""
extractor.py — 参数提取器
每轮对话后，用 LLM 从最新用户消息中提取或更新任务参数。
使用低温度、结构化 prompt，返回严格的结构化候选列表。
"""

import json
import re
from datetime import date
from typing import Any

from .llm_client import LLMClient

EXTRACTION_TASK = """\
你是一个严格的任务识别参数提取器，专门从用户的自然语言中提取任务类型。

【极重要：输出边界】
你当前不是对话助手，而是结构化候选抽取器。
- 只允许输出一个 JSON object，不得输出任何自然语言解释。
- 必须严格遵守以下输出 JSON 结构。
- 可能提供的最近历史消息只用于识别当前回复所指的字段或编号选项；只有最新 user 消息能触发本轮字段更新。

【输出格式】
{{
  "intent": "task_update",
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
2. 只支持上述提到的任务，如果不匹配，则返回空的 slot_candidates 列表，并把无法识别/无法匹配的任务输入写入 unresolved。
3. 如果最新用户消息中对同一字段出现多个候选或多次反悔/修正，以文本中最后出现的候选为准。
4. 最新用户消息使用“第一个”“第二个”“选2”等编号选择时，只能根据最近历史中明确列出的选项映射。
5. 如用户明确说任务紧急（"紧急"、"急"、"加急"等），提取 canonical_key: "emergency_mode" 且 normalized_value: true。
6. 只提取任务类型相关信息，补全 "task_type", "task_type_key", "emergency_mode" 字段。
"""

EXTRACTION_SYSTEM = """\
你是一个严格的参数提取器，专门从用户的自然语言中提取水下ROV作业任务参数。

【极重要：输出边界】
你当前不是对话助手，而是结构化候选抽取器。
- 只允许输出一个 JSON object，不得输出任何自然语言解释。
- 即使当前任务已确认、已发布、已锁定，只要用户本轮明确补充、修改或确认字段，也必须抽取为候选列表。
- 如果用户本轮没有任何字段更新，返回 slot_candidates 为空列表的 JSON。
- 可能提供的最近历史消息只用于识别当前回复所指的字段或编号选项；只有最新 user 消息能触发本轮字段更新。

【输出格式】
{{
  "intent": "task_update",
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
16. 无法识别的信息（包括用户提问的其他话题、闲聊、或不相关的数据）必须提取到 "unresolved" 数组中（每个未识别的段落或词作为字符串），不允许丢弃！

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

        print('extract prompt | '*10)
        result = self.llm.extract_json(messages, max_tokens=800)
        print('result in extract update | '*10)
        print(result)
        
        default_res = {"intent": "task_update", "slot_candidates": [], "unresolved": []}
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
                "intent": result.get("intent", "task_update"),
                "slot_candidates": candidates,
                "unresolved": result.get("unresolved", [])
            }
        return result

    def _select_extraction_history(
        self,
        user_message: str,
        required: list[dict] | None,
        conversation_history: list[dict] | None,
    ) -> list[dict]:
        """仅在当前指令依赖上下文时提供最近六条历史消息。"""
        if not self._needs_history_context(user_message, required):
            return []

        recent = []
        for message in (conversation_history or [])[-6:]:
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
