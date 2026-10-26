"""Evidence for the current task editor, separate from general engineering advice.

The flat OutputBuilder schema and TaskIntentBuilder accept a single task;
neither the dialogue writer nor the publication protocol accepts child tasks or
installs task templates from chat. Keep these interface facts here, and derive
field cardinality/options from the loaded schema (including the active mode).
"""

from __future__ import annotations

import re


def build_task_structure_evidence(
    task_schemas: dict, mode: str = "normal", task_type_key: str | None = None,
) -> dict:
    templates = task_schemas.get("task_templates", {})
    cable_fields = []
    for key, template in templates.items():
        for field in template.get("output_schema", {}).get(mode, []):
            if field.get("key") == "cable_type":
                cable_fields.append({
                    "task_type_key": key,
                    "task_name": template.get("display_name", key),
                    "multiple": field.get("type") == "list",
                    "options": field.get("allowed_values", []),
                })
    return {
        "scope": "当前应用的任务编辑与提交接口，不代表一般工程方法或未来开发能力",
        "child_tasks_supported": False,
        "install_task_types_via_chat": False,
        "inspection_planning": (
            "当前对话应用收集参数、校验并生成TaskIntent；管缆巡检仅记录起终点，"
            "不生成细化航点、悬停间隔或自动匹配巡检策略。机载导航由下游控制系统和设备实现，"
            "不代表本应用已生成路径或支持通过自然语言自定义策略。"
        ),
        "configured_task_types": [
            value for template in templates.values()
            for value in template.get("task_type_values", [])
        ],
        "mode": mode,
        "active_task_type_key": task_type_key,
        "cable_type_fields": cable_fields,
        "development_boundary": "开发者可修改配置和代码；新增模板还需适配校验、任务保存与执行协议，不能据此声称当前对话已支持",
    }


_SYSTEM_SCOPE = re.compile(r"(?:本|当前|现有|这个|该)?系统|本项目|当前应用|网页|界面|平台|对话|聊天|现有任务")
_TASK_SCOPE = re.compile(r"(?:同一(?:个)?|一个|单个|单一|当前|现有|本次).{0,8}任务")
_CURRENT_TASK_SCOPE = re.compile(r"(?:当前|现有|本次).{0,8}任务")
_SUBTASK = re.compile(r"子任务|子计划|任务嵌套|嵌套任务|父子任务")
_EXTENSION = re.compile(r"(?:扩展|自定义|新增|增加|添加|创建).{0,12}任务类型|任务类型.{0,12}(?:扩展|自定义|新增|增加|添加)")
_MULTIPLE_CABLES = re.compile(
    r"(?:管缆|电缆|线缆)类型.{0,15}(?:多选|多个|多种|同时选择)"
    r"|(?:同时|一并|一起).{0,24}(?:电力|光纤).{0,20}(?:电力|光纤)"
    r"|(?:多种|多个|两种|两类).{0,8}(?:管缆|电缆|线缆)"
)
_POSITIVE = re.compile(r"支持|可以|能够|允许|可(?:直接|通过|将|把|以|在|添加|创建|选择)|能(?:直接|通过|将|把|够|在|添加|创建|同时)|已(?:添加|创建)")
_NEGATIVE = re.compile(r"不(?:支持|可以|能|允许|可|代表|意味着|是)|无法|未(?:支持|实现|提供)|尚未|并非|没有|不能认为|不得")
_REFUTATION = re.compile(r"(?:说法|承诺|描述).{0,8}(?:不准确|不成立|错误|不属实)|(?:并非|不是)当前.{0,6}功能")
_DEVELOPMENT = re.compile(r"(?:开发者|开发人员|开发维护|修改代码|修改配置|改造|二次开发|未来版本|未来开发|一般工程|通用工程|工程规划中|算法中)")
_CABLE_PAIR = re.compile(r"(?:电力电缆|光纤通信缆|海底油气管道).{0,6}(?:和|与|及|、).{0,6}(?:电力电缆|光纤通信缆|海底油气管道)")
_STRATEGY = re.compile(
    r"(?:自动|生成|匹配|规划).{0,30}(?:巡检策略|路径策略|策略细节|航线|航迹|路径)"
    r"|(?:巡检|路径|导航)策略.{0,15}(?:自动|生成|匹配)"
)


def _cable_fields(evidence: dict) -> list[dict]:
    fields = evidence["cable_type_fields"]
    active = evidence.get("active_task_type_key")
    return [f for f in fields if f["task_type_key"] == active] if active else fields


def _mentions_multiple_cables(text: str) -> bool:
    return bool(_MULTIPLE_CABLES.search(text) or (_TASK_SCOPE.search(text) and _CABLE_PAIR.search(text)))


def _limits(evidence: dict, *, subtask: bool, extension: bool, cable: bool, strategy: bool = False) -> str:
    parts = []
    if subtask:
        parts.append("当前系统不支持在任务中添加子任务。")
    if extension:
        parts.append("当前对话不能扩展任务类型；新增类型需要开发者适配配置、校验、任务保存与执行协议。")
    if cable:
        single_fields = [f for f in _cable_fields(evidence) if not f["multiple"]]
        if single_fields:
            names = "、".join(dict.fromkeys(f["task_name"] for f in single_fields))
            parts.append(f"当前{names}的管缆类型为单选，一个任务只能选择一种管缆类型。")
        else:
            parts.append("当前任务模式没有提供管缆类型选择字段，不能据此承诺单任务同时指定多种管缆。")
    if strategy:
        parts.append(
            "当前对话应用记录巡检起终点并校验任务参数，不会自动生成详细路径、悬停步骤或巡检策略；"
            "机器人机载导航能力不能等同于本应用支持通过自然语言配置这些策略。"
        )
    return "".join(parts)


def answer_task_structure_query(message: str, evidence: dict) -> str | None:
    """Answer narrowly scoped interface questions before generic identity fallback."""
    if (
        _DEVELOPMENT.search(message) and _EXTENSION.search(message)
        and re.search(r"配置|接口", message) and re.search(r"哪些|如何|怎么|需要", message)
    ):
        return (
            "从开发维护角度，新增任务类型需要检查并适配以下部分：\n"
            "1. 在 config/task_schemas.yaml 定义任务模板、类型取值，以及普通/应急模式的字段、类型和合法选项。\n"
            "2. 检查任务识别、参数抽取与归一化、设备能力匹配和约束校验能否处理新类型及其字段。\n"
            "3. 适配任务保存与下发接口：TaskIntentBuilder 的类型映射和详情构建需要支持新类型；"
            "执行协议也必须支持，不能仅新增配置就认定可以下发。\n"
            "4. 补充收集、修改、只读查询、校验、保存和协议转换的回归验证，再部署更新。\n"
            "这些是开发变更，当前对话不能即时安装新任务类型。本轮未修改任务。"
        )
    if _DEVELOPMENT.search(message) or re.search(r"区别|原理|优缺点|比较|解释", message):
        return None
    if not re.search(r"吗|么|是否|能否|可否|能不能|可不可以|如何|怎么|支持|允许", message):
        return None
    system_scope = bool(_SYSTEM_SCOPE.search(message) or _TASK_SCOPE.search(message))
    subtask = bool(_SUBTASK.search(message) and system_scope)
    extension = bool(_EXTENSION.search(message) and system_scope)
    cable = _mentions_multiple_cables(message) and system_scope
    strategy = bool(_STRATEGY.search(message) and system_scope)
    if not (subtask or extension or cable or strategy):
        return None
    # A list-valued future schema must not inherit today's single-value limit.
    cable = cable and not any(f["multiple"] for f in _cable_fields(evidence))
    answer = _limits(evidence, subtask=subtask, extension=extension, cable=cable, strategy=strategy)
    return answer + "本轮仅回答功能问题，未修改任务。" if answer else None


def ground_task_structure_reply(reply: str, evidence: dict) -> str:
    """Replace unsupported interface promises, retaining the engineering answer.

    Negation is evaluated per clause: a correct denial about one feature must
    not excuse an affirmative claim about another in the same sentence.
    """
    kept = []
    rejected = {"subtask": False, "extension": False, "cable": False, "strategy": False}
    for sentence in re.split(r"(?<=[。！？\n])", reply):
        plain = sentence.replace("**", "").replace("`", "")
        system_scope = False
        development_condition = False
        unsupported = False
        accurate_limitations = []
        for clause in re.split(r"[，,；;]|但(?:是)?|不过|然而", plain):
            system_scope |= bool(_SYSTEM_SCOPE.search(clause) or _CURRENT_TASK_SCOPE.search(clause))
            if _DEVELOPMENT.search(clause):
                development_condition = bool(re.search(r"后\s*$", clause))
                continue
            if re.search(r"当前|目前|现在|已支持", clause):
                development_condition = False
            if _NEGATIVE.search(clause) or _REFUTATION.search(clause):
                if _SUBTASK.search(clause) or _EXTENSION.search(clause) or _mentions_multiple_cables(clause):
                    accurate_limitations.append(clause.strip().rstrip("。！？"))
                continue
            if development_condition:
                continue
            if not (_POSITIVE.search(clause) or _STRATEGY.search(clause)):
                continue
            # Unqualified advice to the user to add a child is also an interface
            # promise; abstract algorithmic decomposition has no such actor.
            scoped = system_scope or bool(re.search(r"(?:您|你)可以", clause))
            subtask = bool(scoped and _SUBTASK.search(clause))
            extension = bool(scoped and _EXTENSION.search(clause))
            cable = bool(
                (scoped or _TASK_SCOPE.search(clause)) and _mentions_multiple_cables(clause)
                and not any(f["multiple"] for f in _cable_fields(evidence))
            )
            strategy = bool(scoped and _STRATEGY.search(clause))
            for key, value in (("subtask", subtask), ("extension", extension), ("cable", cable), ("strategy", strategy)):
                rejected[key] |= value
                unsupported |= value
        if not unsupported:
            kept.append(sentence)
        elif accurate_limitations:
            # Keep a valid denial even when the same sentence also promises a
            # different unsupported feature. Do not retain orphaned "if ..."
            # clauses from the removed recommendation.
            kept.append("。".join(accurate_limitations) + "。")
    answer = "".join(kept).strip()
    if any(rejected.values()):
        boundary = _limits(evidence, **rejected)
        answer = f"{answer}\n\n{boundary}" if answer else boundary
    return answer
