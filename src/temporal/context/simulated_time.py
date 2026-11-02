"""
simulated_time.py — 模拟时间管理器（无线程版）
所有时间计算基于基准时间 + 实时偏移，无需后台线程。
"""
import os
import threading
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Optional


def get_business_timezone() -> ZoneInfo:
    """获取系统项目配置的业务时区。环境变量 SEAGENT_TIMEZONE 为空时默认为 Asia/Shanghai。

    配置非法时抛出 ValueError (Fail-Closed)。
    """
    tz_env = os.getenv("SEAGENT_TIMEZONE", "Asia/Shanghai")
    if not tz_env or not tz_env.strip():
        tz_str = "Asia/Shanghai"
    else:
        tz_str = tz_env.strip()
    try:
        return ZoneInfo(tz_str)
    except Exception as exc:
        raise ValueError(f"Invalid timezone configuration in SEAGENT_TIMEZONE: {tz_env!r}") from exc


class SimulatedTime:
    def __init__(self):
        self._lock = threading.Lock()          # 仍需导入 threading
        self._base_real_time: Optional[float] = None
        self._simulated_start: Optional[datetime] = None

    def start(self):
        """初始化模拟时间（若未设置则使用系统时间）"""
        tz = ZoneInfo("Asia/Shanghai")
        with self._lock:
            if self._simulated_start is None:
                self._simulated_start = datetime.now(tz)
                self._base_real_time = time.time()

    def stop(self):
        """兼容接口，无实际作用"""
        pass

    def reset(self):
        """重置模拟时间，恢复使用系统真实时间"""
        with self._lock:
            self._simulated_start = None
            self._base_real_time = None

    def set_current_time(self, dt: datetime):
        """设置模拟当前时间"""
        tz = ZoneInfo("Asia/Shanghai")
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tz)
        else:
            dt = dt.astimezone(tz)
        with self._lock:
            self._simulated_start = dt
            self._base_real_time = time.time()

    def get_current_time(self) -> datetime:
        """获取当前模拟时间"""
        tz = ZoneInfo("Asia/Shanghai")
        with self._lock:
            if self._simulated_start is None or self._base_real_time is None:
                return datetime.now(tz)
            elapsed = time.time() - self._base_real_time
            dt = self._simulated_start + timedelta(seconds=elapsed)
            return dt.astimezone(tz)

    def get_current_timestamp(self) -> float:
        return self.get_current_time().timestamp()

    def get_current_date(self) -> date:
        return self.get_current_time().date()


# 全局单例及快捷函数保持不变
_simulated_time = SimulatedTime()
def get_simulated_time() -> SimulatedTime: return _simulated_time
def get_current_datetime() -> datetime: return _simulated_time.get_current_time()
def get_current_timestamp() -> float: return _simulated_time.get_current_timestamp()
def get_current_date() -> date: return _simulated_time.get_current_date()
def get_business_datetime() -> datetime: return get_current_datetime().astimezone(get_business_timezone())
def get_business_date() -> date: return get_business_datetime().date()