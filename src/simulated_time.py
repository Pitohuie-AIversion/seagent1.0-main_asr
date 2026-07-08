"""
simulated_time.py — 模拟时间管理器（无线程版）
所有时间计算基于基准时间 + 实时偏移，无需后台线程。
"""
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Optional

class SimulatedTime:
    def __init__(self):
        self._lock = threading.Lock()          # 仍需导入 threading
        self._base_real_time: Optional[float] = None
        self._simulated_start: Optional[datetime] = None

    def start(self):
        """初始化模拟时间（若未设置则使用系统时间）"""
        with self._lock:
            if self._simulated_start is None:
                self._simulated_start = datetime.now(ZoneInfo("Asia/Shanghai"))
                self._base_real_time = time.time()

    def stop(self):
        """兼容接口，无实际作用"""
        pass

    def set_current_time(self, dt: datetime):
        """设置模拟当前时间"""
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
        with self._lock:
            self._simulated_start = dt
            self._base_real_time = time.time()

    def get_current_time(self) -> datetime:
        """获取当前模拟时间"""
        with self._lock:
            if self._simulated_start is None or self._base_real_time is None:
                return datetime.now(ZoneInfo("Asia/Shanghai"))
            elapsed = time.time() - self._base_real_time
            return self._simulated_start + timedelta(seconds=elapsed)

    def get_current_timestamp(self) -> float:
        return self.get_current_time().timestamp()

    def get_current_date(self):
        return self.get_current_time().date()

# 全局单例及快捷函数保持不变
_simulated_time = SimulatedTime()
def get_simulated_time() -> SimulatedTime: return _simulated_time
def get_current_datetime() -> datetime: return _simulated_time.get_current_time()
def get_current_timestamp() -> float: return _simulated_time.get_current_timestamp()
def get_current_date(): return _simulated_time.get_current_date()