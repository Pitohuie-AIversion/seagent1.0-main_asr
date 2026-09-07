"""
src/handlers/base.py - 分层状态机（HSM）基础接口与上下文契约
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional, Dict


@dataclass
class DialogueContext:
    """一次会话处理周期的上下文数据契约"""
    manager: Any
    user_message: str
    request_id: str = "req_default"
    old_phase: str = "collecting"
    metadata: Dict[str, Any] = field(default_factory=dict)

    # 运行时标记与中间产物
    handled: bool = False
    reply: Optional[str] = None
    target_phase: Optional[str] = None
    transition_reason: Optional[str] = None


@dataclass
class HandlerResult:
    """生命周期处理器执行结果"""
    handled: bool
    reply: Optional[str] = None
    target_phase: Optional[str] = None
    transition_reason: Optional[str] = None
    dialogue_mode_transition: Optional[str] = None
    data: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def not_handled(cls) -> HandlerResult:
        """未处理，流转至下一生命周期处理器"""
        return cls(handled=False)

    @classmethod
    def success(
        cls,
        reply: str,
        target_phase: Optional[str] = None,
        transition_reason: Optional[str] = None,
        dialogue_mode_transition: Optional[str] = None,
        data: Optional[Dict[str, Any]] = None,
    ) -> HandlerResult:
        """已处理，返回回复及可能的状态转换"""
        return cls(
            handled=True,
            reply=reply,
            target_phase=target_phase,
            transition_reason=transition_reason,
            dialogue_mode_transition=dialogue_mode_transition,
            data=data or {},
        )


class BaseDialogueHandler(ABC):
    """分层生命周期处理器抽象基类"""

    def __init__(self, manager: Any):
        self.manager = manager

    @abstractmethod
    def can_handle(self, ctx: DialogueContext) -> bool:
        """判断当前上下文是否属于该处理器的处理职责范围"""
        pass

    @abstractmethod
    def handle(self, ctx: DialogueContext) -> HandlerResult:
        """执行生命周期处理"""
        pass
