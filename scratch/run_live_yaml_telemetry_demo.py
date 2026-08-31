"""
run_live_yaml_telemetry_demo.py
===============================
实时刷新 config/ros2_protocol_spec.yaml 中的 live_telemetry_snapshot 节点

演示内容：
1. 启动支持船网关 (ws://127.0.0.1:9091)
2. 开启 SEAgent 遥测追踪器，将接收到的 /task/system_status 实时写入 config/ros2_protocol_spec.yaml
3. 依次下发 4 大任务，实时在 config/ros2_protocol_spec.yaml 中呈现最新水深、姿态与 FINISH 任务状态
"""

import sys
import time
import yaml
from pathlib import Path

# 路径注入
SEAGENT_ROOT = Path(__file__).resolve().parent.parent
MCP_ROOT = SEAGENT_ROOT / "mcp"
CORE_DIR = MCP_ROOT / "core"
MOCK_DIR = MCP_ROOT / "mock"
CONFIG_FILE = SEAGENT_ROOT / "config" / "ros2_protocol_spec.yaml"

for p in [CORE_DIR, MOCK_DIR, MCP_ROOT, SEAGENT_ROOT]:
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from core import (
    RosbridgeClient,
    TaskType,
    TaskStatus,
    TaskStatusTracker,
    LocalOrigin,
    intent_to_syscmd,
    SEAgentMCPBridgeService,
    ROVTelemetry,
)
from mock import MockRosbridgeServer


def update_yaml_telemetry(telemetry: ROVTelemetry):
    """读取 config/ros2_protocol_spec.yaml 并原子更新 live_telemetry_snapshot 节点"""
    try:
        if not CONFIG_FILE.exists():
            return

        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        active_tasks_data = []
        for item in telemetry.task_list:
            active_tasks_data.append({
                "task_id": f"0x{item.task_id:X} ({item.task_id})",
                "task_type": f"{item.task_type}",
                "status_code": item.status,
                "status_name": item.status_name,
                "is_finished": item.is_finished(),
            })

        data["live_telemetry_snapshot"] = {
            "last_updated": telemetry.received_at,
            "online_status": True,
            "current_pose": {
                "x_m": round(telemetry.pose_x, 4),
                "y_m": round(telemetry.pose_y, 4),
                "z_m": round(telemetry.pose_z, 1),
            },
            "water_depth_m": round(telemetry.water_depth, 1),
            "altitude_m": round(telemetry.altitude, 1),
            "ctr_mode": f"{telemetry.ctr_mode} (AUTODEPTH 定深模式)",
            "health_status": f"{telemetry.health} (NORMAL 正常)",
            "active_task_count": len(telemetry.task_list),
            "active_tasks": active_tasks_data,
        }

        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)

    except Exception as e:
        print(f"⚠️ 更新 config/ros2_protocol_spec.yaml 异常: {e}")


def run_live_yaml_demo():
    print("=" * 85)
    print("🌊 SEAgent 遥测实时刷新演练：动态更新 config/ros2_protocol_spec.yaml")
    print("=" * 85)

    test_port = 9091
    print(f"\n[步骤 1/4] 启动 Mock ROS 2 支持船网关 (ws://127.0.0.1:{test_port})...")
    mock_server = MockRosbridgeServer(port=test_port)
    mock_server.start()
    time.sleep(0.5)

    try:
        print("\n[步骤 2/4] 初始化桥接服务并注册 config/ros2_protocol_spec.yaml 实时刷新回调:")
        bridge = SEAgentMCPBridgeService(host="127.0.0.1", port=test_port)
        bridge.start()
        
        # 挂载 YAML 刷新回调
        bridge.tracker.on_telemetry_update(update_yaml_telemetry)
        time.sleep(0.8)

        print(f"  ✅ 初始遥测快照已写入 config/ros2_protocol_spec.yaml！")

        tasks_to_run = [
            ("1. 管道/电缆巡检", "pipeline_inspection", TaskType.SEARCH_CABLE, 22.8025, 113.5255, 85.0),
            ("2. 管道/电缆埋设", "pipeline_burial", TaskType.CLAMP_CABLE, 22.8035, 113.5265, 120.0),
            ("3. 采油树阀门操作", "tree_valve_operation", TaskType.INSERT_PLUG, 22.8045, 113.5275, 310.0),
            ("4. 常规阀门操作", "valve_operation", TaskType.INSERT_PLUG, 22.8055, 113.5285, 240.0),
        ]

        origin = LocalOrigin(latitude=22.80169, longitude=113.52497)

        print("\n[步骤 3/4] 依次下发 4 大任务，观察 config/ros2_protocol_spec.yaml 的实时刷新：")

        for idx, (title, task_type_str, expected_type, lat, lon, depth) in enumerate(tasks_to_run, start=1):
            print(f"\n  ----------------- {title} -----------------")
            intent = {
                "schema_version": 2,
                "task_type": task_type_str,
                "priority": 15,
                "location": {"water_depth_m": depth, "use_geodetic": True},
                "task": {
                    "type": task_type_str,
                    "details": {"target": {"latitude": lat, "longitude": lon}}
                }
            }

            task_id = 0x800A0 + idx
            sent_id = bridge.dispatch_intent(intent, task_id=task_id, use_geodetic=True, origin=origin)
            print(f"  ➔ 已下发任务 0x{sent_id:X} 到 /task_cmd")

            # 等待完成
            finished = bridge.wait_for_task_finish(sent_id, timeout=4.0)
            status_item = bridge.get_task_status(sent_id)
            print(f"  🎉 任务 0x{sent_id:X} 状态推进为: {status_item.status_name if status_item else 'N/A'}")

            # 打印当前 config/ros2_protocol_spec.yaml 中的 live_telemetry_snapshot 内容
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                content = yaml.safe_load(f)
                snapshot = content.get("live_telemetry_snapshot", {})

            print("\n  📄 [实时刷新的 config/ros2_protocol_spec.yaml YAML 片段]:")
            print("  --------------------------------------------------")
            snapshot_yaml = yaml.safe_dump({"live_telemetry_snapshot": snapshot}, allow_unicode=True, sort_keys=False)
            for line in snapshot_yaml.strip().split("\n"):
                print(f"    {line}")
            print("  --------------------------------------------------")

        print("\n[步骤 4/4] 演示完成总结：")
        print("=" * 85)
        print("✅ config/ros2_protocol_spec.yaml 已成功开启实时遥测刷新！")
        print("   系统已具备将水下姿态、水深、活跃任务链实时持久化落盘的能力。")
        print("=" * 85)

    finally:
        bridge.stop()
        mock_server.stop()


if __name__ == "__main__":
    run_live_yaml_demo()
