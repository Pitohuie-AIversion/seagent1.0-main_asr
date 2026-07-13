"""
extractor.py — 参数提取器
每轮对话后，用 LLM 从最新用户消息中提取或更新任务参数。
使用低温度、结构化 prompt，只返回 JSON diff（有变化的字段）。
"""

import json
from datetime import date
from typing import Any

from .llm_client import LLMClient

MAX_EXTRACTION_USER_HISTORY = 6

EXTRACTION_TASK = """\
你是一个严格的任务识别参数提取器，专门从用户的自然语言中提取任务类型。

【提取规则】
1. 对于任务类型：
{task_type_rules}
2. 只支持上述提到的任务，如果用户提出其他要求则返回{{}}
3. 最新用户消息优先：当前任务状态和历史对话只作为参考，不得覆盖最新用户消息中的明确修正。
4. 如果最新用户消息中对同一字段出现多个候选或多次反悔/修正，以文本中最后出现的候选为准。
5. 如用户说"上一次是XX，现在要下一步"，结合上文推断当前任务类型和动作。
6. 如用户明确说任务紧急（"紧急"、"急"、"加急"等），提取 emergency_mode: true。
7. 只提取任务类型相关信息，补全"task_type"、"task_type_key"、"emergency_mode"字段，不要新增任何其他字段。

【输出格式示例】
{{
  "task_type": "管缆巡检",
  "task_type_key": "pipeline_inspection",
  "emergency_mode": false
}}

如无法提取任务类型信息，返回：{{}}
"""

EXTRACTION_SYSTEM = """\
你是一个严格的参数提取器，专门从用户的自然语言中提取水下ROV作业任务参数。

【极重要：输出边界】
你当前不是对话助手，而是字段抽取器。
- 只允许输出一个 JSON object，不得输出任何自然语言解释。
- 不得模仿历史助手回复，不得输出"收到"、"已更新"、"任务已发布"、"参数已生效"等对话式文本。
- 即使当前任务已确认、已发布、已锁定，只要用户本轮明确补充、修改或确认字段，也必须抽取为 JSON diff。
- 如果用户本轮没有任何字段更新，只输出 {{}}。

【错误输出示例】
收到，已更新任务水深为300米。

【正确输出示例】
{{"water_depth": 300}}

【提取规则】
1. 只提取用户明确提供或可以高置信度推断的信息，不猜测。
2. 只返回本轮对话中有新内容/更新的字段（JSON diff），不重复已知字段。
3. 最新用户消息优先：当前任务状态和历史对话只作为参考，不得覆盖最新用户消息中的明确修正。
4. 如果最新用户消息中对同一字段出现多个候选或多次反悔/修正，以文本中最后出现的候选为准。
   例如用户说"晚上八点开始，不不现在开始，算了还是明天上午开始吧"，start_time 应以最后的"明天上午开始"为准。
5. 对于时间信息：将口语时间转换为 YYYY-MM-DDTHH:MM:SS 格式，无时间部分时补 T00:00:00；"现在/当前/立即"等表达必须基于【当前时间】换算。
6. 对于坐标：提取为 {{"lat": float, "lon": float}} 格式，统一十进制度。以下形式都要识别为坐标：
   - (19.8,113.5)、19.8,113.5、19.8，113.5、19.8 113.5
   - 北纬19.8，东经113.5、纬度19.8，经度113.5、lat 19.8 lon 113.5
   - 十九点八，一百一十三点五、北纬十九点八，东经一百一十三点五
   - 未明确标注经纬度时，默认前一个数是 lat，后一个数是 lon。
7. 对于水深：统一转换为米（m）为单位的数值，例如"1千米"→1000，"500m"→500。
   用户说"水深300米"、"深度300"、"作业水深是300"、"300米水深"时，输出 {{"water_depth": 300}}。
8. 对于任务类型：
{task_type_rules}
9. 对于ROV型号：如用户描述模糊（如"深水工作ROV"、"轻型观察"），提取 rov_description 字段，不要强行映射型号名。
10. 若确定ROV型号，可自动识别出ROV类型：{ROV2type}
11. 机器人能力、最大水深、载荷、功率、尺寸、状态、任务阈值和作业限制必须以所需字段、允许值、ROV2type和后续知识库/约束校验为准；不得凭通用知识补全或改写配置中没有的信息。
12. 如用户明确说任务紧急（"紧急"、"急"、"加急"等），提取 emergency_mode: true。
13. 如用户说"上一次是XX，现在要下一步"，结合上文推断当前任务类型和动作。
14. 只根据所需字段中定义的key提取，不新增字段，不自创字段。
15. 如果有系统提出的修改建议被用户接受的，同样也需要能够提取出来。
16. 只输出 JSON，不要任何解释文字。
17. 【数字选项映射】如果上一条助手消息中以"1."/"2."/"3."等编号形式列出了选项，而用户本轮仅回复了一个数字（如"1"、"2"、"3"），则应将该数字映射为对应编号的选项值进行提取，不得忽略此类回复。

【当前时间】{today}

【所需字段及其描述，key为字段名，label为字段描述】
{required}

【当前任务状态（已知字段，避免重复提取）】
{current_state}

【输出格式示例】
{{
  "task_type": "管缆巡检",
  "task_type_key": "pipeline_inspection",
  "start_time": "2026-04-15T00:00:00",
  "water_depth": 850,
  "emergency_mode": false
}}

如无任何新信息可提取，返回：{{}}
"""


def _build_task_type_rules(task_type_map: dict[str, str]) -> str:
    """
    根据 task_type_map（{display_value: template_key}）动态生成提取规则文本。
    按 template_key 分组，让 LLM 知道同一模板的多个值共用同一个 task_type_key。
    """
    # 按 template_key 分组
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
        """
        从最新用户消息中提取参数更新，返回 dict（只含有变化的字段）。
        task_type_map: {display_value: template_key}，由 kb.get_task_type_map() 提供。
        若无可提取内容返回 {}。
        """
        # from datetime import datetime
        # from zoneinfo import ZoneInfo
        # now = datetime.now(ZoneInfo("Asia/Shanghai"))
        # today_str = now.isoformat()
        from .simulated_time import get_current_datetime
        now = get_current_datetime()  # 使用模拟时间
        today_str = now.isoformat()

        # 精简 current_state，只传已有值的字段
        known = {k: v for k, v in current_state.items() if v is not None}

        task_type_rules = _build_task_type_rules(task_type_map or {})

        if task_type_key is None:
            system_prompt = EXTRACTION_TASK.format(
                task_type_rules=task_type_rules,
            )
        else:
            # 将 required 转为 JSON 字符串，便于 LLM 理解
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
        print(messages)
        result = self.llm.extract_json(messages, max_tokens=500)
        print('result in extract update | '*10)
        print(result)
        return result or {}

    @staticmethod
    def _build_extraction_history(conversation_history: list[dict]) -> list[dict]:
        """只保留最近几轮的对话历史，同时包含用户和助手消息，以便保留选项上下文。"""
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
        """
        当用户用模糊描述指代ROV时，用 LLM 从全量ROV列表中匹配最合适的候选。
        返回按匹配度排序的候选列表（最多3个）。
        """
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
