"""
src/exceptions.py — 自定义异常类声明
"""


class ControlAuditPersistenceError(RuntimeError):
    """控制请求或草稿取消的历史审计持久化失败。"""


class ControlAuditConflictError(RuntimeError):
    """控制请求 ID 已存在且内容冲突。"""


class ControlAuditConflict(ControlAuditConflictError):
    """ControlAuditConflictError 的简写别名。"""


class ControlAuditCommitUncertainError(RuntimeError):
    """控制请求原子替换完成后落盘验证或父目录同步失败，落盘状态无法确定。"""


class ControlAuditCommitUncertain(ControlAuditCommitUncertainError):
    """ControlAuditCommitUncertainError 的简写别名。"""


class ControlAuditCorruptionError(RuntimeError):
    """控制审计事件文件存在但内容损坏、不可解析或 schema 非法。
    语义：文件存在但不可信 → fail closed（不得当作未找到）。"""



class ServiceNotInitializedError(RuntimeError):
    """全局 AI 服务或知识库未初始化。"""


class TaskPersistenceError(Exception):
    """TaskIntent 文件持久化失败。"""


class TaskRollbackError(TaskPersistenceError):
    """TaskIntent 发布失败后的状态回滚异常。"""


class IntentIdConflict(Exception):
    """Intent ID 冲突或重复写入内容不一致。"""


class IdReservationError(Exception):
    """ID 序列号预留或生成失败。"""


class StatePersistenceError(RuntimeError):
    """机器人实时状态无法可靠持久化。"""


class StateVersionConflict(StatePersistenceError):
    """机器人状态的乐观锁版本与当前持久化版本不一致。"""

    def __init__(
        self,
        status_ref: str,
        expected_version: int,
        current_version: int,
    ):
        self.status_ref = status_ref
        self.expected_version = expected_version
        self.current_version = current_version
        super().__init__(
            "Robot state version conflict "
            f"for {status_ref}: expected {expected_version}, "
            f"current {current_version}"
        )


class StateSnapshotValidationError(StatePersistenceError):
    """机器人状态快照无法解析或不符合持久化 schema。"""


class StateSelectorError(ValueError):
    """机器人选择器无法唯一解析为已配置的 status_ref。"""
