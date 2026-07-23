"""
src/exceptions.py — 自定义异常类声明
"""


class TaskPersistenceError(Exception):
    """TaskIntent 文件持久化失败"""
    pass


class TaskRollbackError(TaskPersistenceError):
    """TaskIntent 发布失败后的状态回滚异常"""
    pass


class IntentIdConflict(TaskPersistenceError):
    """Intent ID 冲突或重复写入内容不一致"""
    pass


class IdReservationError(Exception):
    """ID 序列号预留或生成失败"""
    pass
