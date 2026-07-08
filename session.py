from typing import List, Dict, Any, Tuple
class Session:
    """保存单次对话的所有信息"""
    def __init__(self, sid: str):
        self.sid = sid                     # 唯一会话ID
        self.history: List[dict] = []      # 完整对话历史 [{"role": "user/assistant", "content": ...}]
        self.collected: dict = {}          # 已收集的结构化字段 {field_name: value}
        self.task_type: str = None         # 内部任务类型: build_connection, break_connection, pipeline_inspection...
        self.is_emergency: bool = False    # 是否紧急任务
        self.required_fields: List[str] = []   # 当前任务需要的必填字段列表
        self.missing_fields: List[str] = []    # 当前缺失的字段
        self.completed: bool = False           # 是否已完成全部收集且通过校验
        self.constraint_passed: bool = False   # 是否通过硬约束
        self.final_json: dict = None           # 最终生成的任务JSON
        self.rejection_reason: str = None      # 拒绝原因（如果被拒绝）
        self.pending_confirmations: List[dict] = []  # 等待用户确认的字段规范化变更
        self.awaiting_final_confirm: bool = False    # 是否确认最终json

    def update_missing(self):
        """根据已收集字段和必填字段，重新计算缺失字段列表"""
        required = set(self.required_fields)
        existing = set(self.collected.keys())
        self.missing_fields = list(required - existing)

    def is_all_required_collected(self) -> bool:
        """检查是否所有必填字段都已收集"""
        return len(self.missing_fields) == 0