"""
src/handlers/command_predicates.py - 对话用户控制指令与意图谓词模块

职责：
从 DialogueManager 中解耦抽离所有确定性的控制指令与用户意图判定逻辑：
1. 任务最终发布确认指令（is_final_publish_confirmation）
2. 泛确认/认可指令（is_confirmation_only, is_user_confirmed）
3. 软警告忽略/接受风险指令（is_ignore_warning）
4. 任务取消/放弃指令（is_user_cancelled）
5. 参数显式修改意图（is_user_requested_modification）
6. 业务身份与能力提问（is_business_identity_query）
7. 搭载载荷修改意图（is_payload_modification_request）
"""

from __future__ import annotations

import re


def is_business_identity_query(message: str) -> bool:
    """判断是否为询问系统/助手自身身份或基础能力的查询。"""
    text = message.strip().lower()
    identity_patterns = (
        "你是什么", "你是谁", "你是啥", "你的身份", "你叫什么",
        "介绍一下你自己", "自我介绍", "你能做什么", "你有什么功能", "what are you", "who are you",
    )
    return any(pattern in text for pattern in identity_patterns)


def is_user_confirmed(message: str) -> bool:
    """判断消息中是否包含基本的确认/完成关键词。"""
    keywords = ["确认", "没问题", "发布", "提交", "ok", "好的", "可以", "确定"]
    return any(kw in message.lower() for kw in keywords)


def is_final_publish_confirmation(message: str) -> bool:
    """仅识别明确具有‘发布/提交当前任务’语义的独立指令。"""
    text = re.sub(r"[\s，,。.!！?？、；;：:]+", "", message).lower()
    negated = [
        "不发布", "不要发布", "别发布", "取消发布", "暂不发布",
        "拒绝发布", "暂缓发布", "先不发布", "不提交", "不要提交", "先不提交", "暂不提交",
    ]
    if any(neg in text for neg in negated):
        return False

    base_cmds = {
        "确认发布",
        "确认并发布",
        "确认发布任务",
        "确认发布当前任务",
        "确认发布该任务",
        "确认最终发布",
        "最终确认发布",
        "确认正式发布",
        "正式确认发布",
        "确认立即发布",
        "立即确认发布",
        "最终发布",
        "正式发布",
        "发布任务",
        "发布当前任务",
        "发布",
        "立即发布",
        "现在发布",
        "直接发布",
        "确认提交",
        "确认并提交",
        "确认提交任务",
        "确认最终提交",
        "最终确认提交",
        "最终提交",
        "提交任务",
        "提交",
        "立即提交",
        "确认下发",
        "确认下发任务",
        "最终下发",
        "下发任务",
        "下发",
        "确认开始",
        "确认开始任务",
    }
    if text in base_cmds:
        return True
    if len(text) <= 12 and re.fullmatch(
        r"(?:确认|最终|正式|直接|现在|立刻|立即)*(?:确认)*(?:发布|提交|下发)(?:任务|当前任务|该任务)?",
        text,
    ):
        return True
    return False


def is_confirmation_only(message: str) -> bool:
    """仅识别不携带参数更新的独立泛确认/认可指令。"""
    text = re.sub(r"[\s，,。.!！?？、；;：:]+", "", message).lower()
    negated = ["不确认", "不要确认", "先不确认", "不确定", "不是", "不行", "不对"]
    if any(neg in text for neg in negated):
        return False
    return text in {
        "确认",
        "确认无误",
        "最终确认",
        "确认开始",
        "开始",
        "开始任务",
        "确定",
        "没问题",
        "好的",
        "可以",
        "ok",
        "继续",
        "认可",
        "同意",
        "收到",
        "清楚",
        "明白",
    }


def is_ignore_warning(message: str) -> bool:
    """仅识别明确具有忽略/无视软警告语义的独立控制指令。"""
    text = re.sub(r"[\s，,。.!！?？、；;：:]+", "", message).lower()
    negated = ["不忽略", "不要忽略", "不能忽略", "别忽略", "不无视", "不要无视", "不是忽略", "不接受", "不要接受", "不能接受"]
    if any(neg in text for neg in negated):
        return False
    if any(kw in text for kw in ["修改", "改成", "更改", "调整", "设为", "设置为"]):
        return False

    base_cmds = {
        "忽略警告",
        "忽略警告继续",
        "忽略软警告",
        "忽略软警告继续",
        "忽略",
        "无视警告",
        "无视软警告",
        "忽略此警告",
        "忽略当前警告",
        "无视此警告",
        "无视当前警告",
        "接受风险",
        "忽略风险",
        "无视",
        "忽略继续",
        "无视继续",
        "接受软警告",
    }
    if text in base_cmds:
        return True

    pattern = r"^(?:忽略|无视|接受|跳过)(?:(?!修改|更改|调整|取消|放弃).)*?(?:软警告|警告|软约束|风险|提示)(?:并)?(?:继续|确认|确认任务|继续确认|继续确认任务|发布|进入发布|直接发布|去发布|进行发布|继续发布|确认发布|并继续|并确认|并发布|继续执行|提交|下一步)?$"
    if re.match(pattern, text):
        return True

    prefixes = ("忽略软警告", "忽略警告", "无视软警告", "无视警告", "接受风险", "忽略风险", "接受软警告")
    suffixes = (
        "继续", "确认", "确认任务", "继续确认", "继续确认任务",
        "继续发布", "确认发布", "并继续", "并确认", "并发布", "继续执行",
        "进入发布", "直接发布", "进行发布", "去发布", "提交",
    )
    for p in prefixes:
        if text.startswith(p):
            rest = text[len(p):]
            if rest in suffixes:
                return True
    return False


def is_user_cancelled(message: str) -> bool:
    """判断用户是否明确要求取消、放弃或终止当前任务流程。"""
    negated_cancel = ["不是要取消", "不是取消", "不要取消", "别取消", "不取消", "免取消"]
    if any(neg in message for neg in negated_cancel):
        return False

    if any(mod_kw in message for mod_kw in ["修改", "参数", "设置", "载荷", "水深", "支持船", "管缆", "油田", "设备", "工具"]) and "任务" not in message:
        return False
    keywords = [
        "取消任务", "放弃任务", "终止任务", "取消", "放弃", "不要了", "终止", "退出",
        "算了一会儿再做", "先不搞了", "不要创建了", "撤销", "作废", "重新来", "清空",
        "不做了", "算了不用了", "先退出来", "终止创建", "不搞了", "不用了",
    ]
    return any(kw in message for kw in keywords)


def is_user_requested_modification(message: str) -> bool:
    """判断用户是否明确要求覆盖已经录入的参数。"""
    keywords = (
        "修改",
        "改成",
        "改为",
        "改到",
        "更改",
        "更换",
        "换成",
        "换为",
        "调整",
        "重新设置",
        "设置为",
        "设为",
        "替换",
    )
    return any(keyword in message for keyword in keywords)


def is_payload_modification_request(user_message: str) -> bool:
    """判断用户是否明确请求重新选择/修改/配置载荷（且不属于取消修改指令）。"""
    from .slot_filling import SlotFillingHandler
    return SlotFillingHandler.is_payload_modification_request(user_message)
