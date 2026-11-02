"""Keep payload explanations separate from permission to publish or execute."""

import re


_UNQUALIFIED_ADMISSION = re.compile(
    r"(?:即可|就可|便可|就能|便能|则(?:任务)?可)(?:以)?(?:直接)?"
    r"(?:发布|执行|满足(?:全部|任务)?准入(?:条件)?)"
)
_NEGATED_ADMISSION = re.compile(r"不代表|不意味着|不能|不得|不可|并不|未完成")
_COMPLETE_VALIDATION = re.compile(r"全部校验|完整校验|所有约束|发布确认|校验.{0,15}确认")
_ADMISSION_SHORTCUT = re.compile(r"准入校验逻辑.{0,50}(?:而非|而不是)")
_TASK_SOLICITATION = re.compile(r"(?:请|您可以|如需|如有).{0,30}(?:描述|提供).{0,20}(?:作业需求|任务目标)")


def ground_payload_requirement_reply(reply: str) -> str:
    """Drop unsupported admission shortcuts while retaining the model's answer.

    This read-only query contains configuration evidence, not the result of a
    complete task validation or a user's publication confirmation.
    """
    kept = []
    for sentence in re.split(r"(?<=[。！？\n])", reply):
        plain = sentence.replace("**", "").replace("`", "")
        unsupported = (
            (_UNQUALIFIED_ADMISSION.search(plain) or _ADMISSION_SHORTCUT.search(plain))
            and not _NEGATED_ADMISSION.search(plain)
            and not _COMPLETE_VALIDATION.search(plain)
            and not plain.rstrip().endswith(("?", "？"))
        )
        if unsupported or _TASK_SOLICITATION.search(plain):
            continue
        kept.append(sentence)
    answer = "".join(kept).strip()
    boundary = (
        "以上仅说明设备能力与载荷配置，不代表已通过全部准入检查。"
        "发布前仍须参数完整合法、硬约束校验通过、软警告处理及明确确认；实际下发还需执行协议支持。"
        "本轮未创建、修改或发布任务。"
    )
    return f"{answer}\n\n{boundary}" if answer else boundary
