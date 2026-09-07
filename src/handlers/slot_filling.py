"""
src/handlers/slot_filling.py - 槽位填报与消歧生命周期处理器

职责：
1. 搭载工具就地调整/补丁提取；
2. 任务参数抽取（ParameterExtractor）与规范化应用（FieldNormalizer/NormalizationContract）；
3. 槽位状态存储与事务更新（SlotStore, TaskSlotFilter）；
4. 交互式多候选消歧（ROV候选、油田候选、工具组合候选）；
5. 缺失参数指引与下步提问生成。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from .base import BaseDialogueHandler, DialogueContext, HandlerResult

logger = logging.getLogger(__name__)


class SlotFillingHandler(BaseDialogueHandler):
    """槽位填报与消歧生命周期处理器"""

    def can_handle(self, ctx: DialogueContext) -> bool:
        """
        判断是否由本处理器进行槽位填报或消歧：
        1. 搭载工具就地修改指令；
        2. 处于 collecting 或其他正常任务填报阶段。
        """
        user_message = ctx.user_message
        if self.manager._is_payload_modification_request(user_message):
            return True

        # 默认在任务收集流程中接管填报
        return True

    def handle(self, ctx: DialogueContext) -> HandlerResult:
        """执行槽位就地修改或委托至核心填报状态机"""
        user_message = ctx.user_message

        # 1. 优先检查搭载工具就地修改
        if self.manager._is_payload_modification_request(user_message):
            payload_mod_reply = self.manager._handle_payload_modification_request(user_message)
            if payload_mod_reply is not None:
                return HandlerResult.success(reply=payload_mod_reply)

        # 2. 其他槽位提取、更新及消歧
        # 由 DialogueManager 的核心槽位提交流程完成，返回 not_handled 允许 manager 往下执行
        return HandlerResult.not_handled()
