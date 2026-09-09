"""
tests/test_hsm_handlers.py - 分层状态机（HSM）生命周期处理器单元测试
"""

import pytest
from unittest.mock import MagicMock

from src.dialogue_manager import DialogueManager
from src.handlers import (
    BaseDialogueHandler,
    DialogueContext,
    HandlerResult,
    ConversationRouterHandler,
    SlotFillingHandler,
    ConstraintDecisionHandler,
    TaskCommitHandler,
)


class TestHSMHandlersContract:
    """验证 HSM 各生命周期处理器的契约与隔离性"""

    def test_handler_initialization(self):
        dm = DialogueManager()
        assert hasattr(dm, "router_handler")
        assert hasattr(dm, "constraint_handler")
        assert hasattr(dm, "commit_handler")
        assert hasattr(dm, "slot_handler")

        assert isinstance(dm.router_handler, ConversationRouterHandler)
        assert isinstance(dm.constraint_handler, ConstraintDecisionHandler)
        assert isinstance(dm.commit_handler, TaskCommitHandler)
        assert isinstance(dm.slot_handler, SlotFillingHandler)

    def test_conversation_router_time_query(self):
        dm = DialogueManager()
        ctx = DialogueContext(manager=dm, user_message="现在几点？")
        assert dm.router_handler.can_handle(ctx) is True
        res = dm.router_handler.handle(ctx)
        assert res.handled is True
        assert res.reply is not None
        assert "时间" in res.reply or "20" in res.reply

    def test_task_commit_handler_rejects_done_modification(self):
        dm = DialogueManager()
        dm.phase = "done"
        dm.task_state = {"intent_id": "TEST_INTENT_001"}
        ctx = DialogueContext(manager=dm, user_message="修改深度为100米")
        assert dm.commit_handler.can_handle(ctx) is True
        res = dm.commit_handler.handle(ctx)
        assert res.handled is True
        assert "归档" in res.reply or "无法就地修改" in res.reply

    def test_task_commit_handler_prevents_duplicate_publish(self):
        dm = DialogueManager()
        dm.phase = "done"
        dm.task_state = {"intent_id": "TEST_INTENT_001"}
        ctx = DialogueContext(manager=dm, user_message="确认发布")
        assert dm.commit_handler.can_handle(ctx) is True
        res = dm.commit_handler.handle(ctx)
        assert res.handled is True
        assert "无需重复发布" in res.reply

    def test_constraint_handler_blocks_hard_bypass(self):
        dm = DialogueManager()
        dm.phase = "blocked_hard"
        mock_violation = MagicMock()
        mock_violation.severity = "hard"
        mock_violation.constraint_id = "DEPTH_EXCEEDED"
        dm._blocking_violations = [mock_violation]
        
        # mock _refresh_validation 返回仍有硬违规
        mock_val_res = MagicMock()
        mock_val_res.violations = [mock_violation]
        mock_val_res.overall_status = "invalid"
        dm._refresh_validation = MagicMock(return_value=mock_val_res)

        ctx = DialogueContext(manager=dm, user_message="确认发布")
        assert dm.constraint_handler.can_handle(ctx) is True
        res = dm.constraint_handler.handle(ctx)
        assert res.handled is True
        assert "违规" in res.reply or "无法直接" in res.reply or "阻断" in res.reply or "修改" in res.reply

    def test_constraint_handler_handles_soft_warning_ignore(self):
        dm = DialogueManager()
        dm.phase = "blocked_soft"
        mock_violation = MagicMock()
        mock_violation.severity = "soft"
        mock_violation.constraint_id = "C013"
        mock_violation.related_fields = ["turbidity"]
        mock_violation.observed_value = 7
        dm._blocking_violations = [mock_violation]

        mock_val_res = MagicMock()
        mock_val_res.violations = [mock_violation]
        mock_val_res.task_version = 1
        mock_val_res.validation_version = 1
        mock_val_res.validation_fingerprint = "fp123"
        mock_val_res.state_snapshot = {}
        dm._refresh_validation = MagicMock(return_value=mock_val_res)
        dm.builder.get_schema = MagicMock(return_value=[])

        ctx = DialogueContext(manager=dm, user_message="忽略警告")
        assert dm.constraint_handler.can_handle(ctx) is True
        res = dm.constraint_handler.handle(ctx)
        assert res.handled is True
        assert "记录" in res.reply or "警告" in res.reply
