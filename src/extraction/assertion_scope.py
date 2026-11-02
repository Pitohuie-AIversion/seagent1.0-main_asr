"""Explicit task-wide edit restrictions, distinct from field-local negation."""

import re


_PREFIX = r"(?:(?:请|本轮|这轮|这次|现在|目前|暂时|先|以上)\s*)*"
_TASK_OBJECT = r"(?:(?:任何|所有|全部|当前|现有|已有|这个|本次)\s*)*(?:任务(?:参数|配置|信息)?|参数|配置)"
_TASK_PAUSE = re.compile(
    _PREFIX
    + r"(?:不要|别|不)\s*(?:(?:再|先|替我|帮我|擅自)\s*)*"
    + r"(?:修改|改动|调整|变更|更改|更新|改)\s*(?:"
    + _TASK_OBJECT + r")?\s*(?:了|吧)?"
)
_DISCUSSION_ONLY = re.compile(
    _PREFIX + r"(?:仅|只)(?:是)?\s*(?:讨论|咨询|了解|询问|解释|分析)(?:一下)?(?:而已)?"
)


def task_edits_are_paused(text: str) -> bool:
    """Require a whole clause so '不要修改任务编号' remains field-local.

    A standalone pause applies across the current turn's clauses. Object-
    specific discussion such as '只是讨论删除工具' is left to field guards.
    This does not infer READ/WRITE intent or override execution controls.
    """
    return any(
        _TASK_PAUSE.fullmatch(clause.strip()) or _DISCUSSION_ONLY.fullmatch(clause.strip())
        for clause in re.split(r"[，,。；;！!？?\n]+", str(text or ""))
        if clause.strip()
    )
