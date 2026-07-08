"""
state_info.py — 机器人状态查询模块
根据机器人ID读取 state.yaml 中的实时状态
返回格式与 constraints.yaml 检查逻辑兼容
"""

import yaml
from typing import Optional, Dict, Any
from pathlib import Path


class RobotStateInfo:
    def __init__(self):
        # 固定路径：所有读写都指向 config/state.yaml
        self.state_file = Path(__file__).parent.parent / "config" / "state.yaml"

    def set_status(self, equipment_name: str, params: dict):
        try:
            # 每次从文件加载最新数据
            state_data = self._load_state()
            robots = state_data.setdefault("robots", {})

            # 如果设备不存在，自动创建
            if equipment_name not in robots:
                robots[equipment_name] = {}

            # 取出当前设备状态
            state = robots[equipment_name]

            # 先合并用户传入的参数（除 update_timestamp 外可以先合并）
            state.update(params)

            # 处理 update_timestamp：如果用户未提供、提供空字符串或 None，则用模拟时间填充
            if not state.get("update_timestamp"):
                from .simulated_time import get_current_datetime
                state["update_timestamp"] = get_current_datetime().strftime("%Y-%m-%dT%H:%M:%S+08:00")

            # 写入文件（持久化）
            self._save_state(state_data)
        except Exception as e:
            print(f"❌ set_status 出错: {e}")
            import traceback
            traceback.print_exc()

    def get_robot_state(self, equipment_name: str) -> Optional[Dict[str, Any]]:
        """每次都从文件读取最新状态"""
        state_data = self._load_state()
        return state_data.get("robots", {}).get(equipment_name)

    def get_all_info(self, equipment_name: str = None) -> Dict[str, Any]:
        state_data = self._load_state()
        robots = state_data.get("robots", {})
        if equipment_name:
            return robots.get(equipment_name)
        return robots

    def _load_state(self) -> Dict[str, Any]:
        """从文件加载"""
        if not self.state_file.exists():
            return {"robots": {}}
        with open(self.state_file, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {"robots": {}}

    def _save_state(self, data: Dict[str, Any]):
        """写入文件（覆盖整个文件）"""
        self.state_file.parent.mkdir(exist_ok=True)
        with open(self.state_file, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)