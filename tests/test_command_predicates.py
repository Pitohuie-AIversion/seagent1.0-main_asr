"""
tests/test_command_predicates.py - 用户对话指令意图判定谓词单元测试
"""

import pytest
from src.handlers.command_predicates import (
    is_business_identity_query,
    is_confirmation_only,
    is_final_publish_confirmation,
    is_ignore_warning,
    is_payload_modification_request,
    is_user_cancelled,
    is_user_confirmed,
    is_user_requested_modification,
)
from src.dialogue_manager import DialogueManager


class TestCommandPredicates:
    """验证从 DialogueManager 独立抽离的指令判定谓词正确性与完备性"""

    def test_final_publish_confirmation_positives(self):
        positives = [
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
        ]
        for cmd in positives:
            assert is_final_publish_confirmation(cmd) is True, f"Failed positive: {cmd}"
            assert DialogueManager._is_final_publish_confirmation(cmd) is True

    def test_final_publish_confirmation_negatives(self):
        negatives = [
            "不发布",
            "不要发布",
            "别发布",
            "取消发布",
            "暂不发布",
            "拒绝发布",
            "暂缓发布",
            "先不发布",
            "不提交",
            "不要提交",
            "先不提交",
            "暂不提交",
            "我想修改水深为100米",
            "水深100米",
            "你好",
            "巡检管道",
        ]
        for cmd in negatives:
            assert is_final_publish_confirmation(cmd) is False, f"Failed negative: {cmd}"
            assert DialogueManager._is_final_publish_confirmation(cmd) is False

    def test_confirmation_only(self):
        positives = [
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
        ]
        for cmd in positives:
            assert is_confirmation_only(cmd) is True, f"Failed positive: {cmd}"
            assert DialogueManager._is_confirmation_only(cmd) is True

        negatives = [
            "不确认",
            "不要确认",
            "先不确认",
            "不确定",
            "不是",
            "不行",
            "不对",
            "确认修改水深100米",
            "取消任务",
        ]
        for cmd in negatives:
            assert is_confirmation_only(cmd) is False, f"Failed negative: {cmd}"
            assert DialogueManager._is_confirmation_only(cmd) is False

    def test_ignore_warning(self):
        positives = [
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
            "忽略排期软警告，进入发布。",
            "接受软警告并发布",
            "忽略未来任务环境与遥测延后校验提示",
        ]
        for cmd in positives:
            assert is_ignore_warning(cmd) is True, f"Failed positive: {cmd}"
            assert DialogueManager._is_ignore_warning(cmd) is True

        negatives = [
            "不忽略",
            "不要忽略",
            "不能忽略",
            "别忽略",
            "不无视",
            "不要无视",
            "不是忽略",
            "不接受",
            "不要接受",
            "不能接受",
            "忽略软警告修改水深300米",
            "把水深修改为300米，忽略之前设置",
        ]
        for cmd in negatives:
            assert is_ignore_warning(cmd) is False, f"Failed negative: {cmd}"
            assert DialogueManager._is_ignore_warning(cmd) is False

    def test_user_cancelled(self):
        positives = [
            "取消任务",
            "放弃任务",
            "终止任务",
            "取消",
            "放弃",
            "不要了",
            "终止",
            "退出",
            "算了一会儿再做",
            "先不搞了",
            "不要创建了",
            "撤销",
            "作废",
            "重新来",
            "清空",
            "不做了",
            "算了不用了",
            "先退出来",
            "终止创建",
            "不搞了",
            "不用了",
        ]
        for cmd in positives:
            assert is_user_cancelled(cmd) is True, f"Failed positive: {cmd}"
            assert DialogueManager._user_cancelled(cmd) is True

        negatives = [
            "不是要取消",
            "不是取消",
            "不要取消",
            "别取消",
            "不取消",
            "免取消",
            "修改载荷",
            "设置水深",
        ]
        for cmd in negatives:
            assert is_user_cancelled(cmd) is False, f"Failed negative: {cmd}"
            assert DialogueManager._user_cancelled(cmd) is False

    def test_user_requested_modification(self):
        positives = [
            "修改水深为150米",
            "改成工作级ROV",
            "改为海缆巡检",
            "调整载荷为机械臂",
            "重新设置为流花油田",
        ]
        for cmd in positives:
            assert is_user_requested_modification(cmd) is True, f"Failed positive: {cmd}"
            assert DialogueManager._user_requested_modification(cmd) is True

        negatives = [
            "确认无误",
            "发布",
            "你好",
        ]
        for cmd in negatives:
            assert is_user_requested_modification(cmd) is False, f"Failed negative: {cmd}"
            assert DialogueManager._user_requested_modification(cmd) is False

    def test_business_identity_query(self):
        positives = [
            "你是什么",
            "你是谁",
            "你的身份",
            "你能做什么",
            "what are you",
            "who are you",
        ]
        for cmd in positives:
            assert is_business_identity_query(cmd) is True, f"Failed positive: {cmd}"
            assert DialogueManager._is_business_identity_query(cmd) is True

        negatives = [
            "巡检管道",
            "水深300米",
            "确认发布",
        ]
        for cmd in negatives:
            assert is_business_identity_query(cmd) is False, f"Failed negative: {cmd}"
            assert DialogueManager._is_business_identity_query(cmd) is False

    def test_user_confirmed_basic(self):
        assert is_user_confirmed("确认") is True
        assert is_user_confirmed("没问题") is True
        assert is_user_confirmed("ok") is True
        assert is_user_confirmed("水深300米") is False
        assert DialogueManager._user_confirmed("好的") is True
