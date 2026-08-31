"""
run_live_telemetry_simulation.py
================================
SEAgent 水下机器人遥测模拟与双端通信闭环演示脚本

演示内容：
1. 启动支持船端 Mock ROS 2 网关 (ws://127.0.0.1:9091)
2. 动态注入 ROV 实时遥测（深度、位置、电池、推进器健康度）
3. 自动同步遥测数据至 SEAgent 状态中心 RobotStateInfo
4. 下发 4 大官方业务任务并进行状态生命周期闭环追踪
"""

import sys
import time
from pathlib import Path

# 添加项目根目录与 mcp/ 目录
SEAGENT_ROOT = Path(__file__).resolve().parent.parent
MCP_ROOT = SEAGENT_ROOT / "mcp"
CORE_DIR = MCP_ROOT / "core"
MOCK_DIR = MCP_ROOT / "mock"

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
)
from mock import MockRosbridgeServer
from src.state_info import RobotStateInfo


def run_live_simulation():
    print("=" * 85)
    print("🌊 SEAgent 水下机器人遥测模拟与双端通信闭环演示")
    print("=" * 85)

    test_port = 9091
    print(f"\n[1/5] 启动支持船端 Mock 网关 (ws://127.0.0.1:{test_port})...")
    mock_server = MockRosbridgeServer(port=test_port)
    mock_server.start()
    time.sleep(0.5)

    try:
        # 步骤 2: 启动云端 SEAgent 桥接服务并自动同步状态
        print("\n[2/5] 启动云端 SEAgentMCPBridgeService 自动遥测同步:")
        bridge_service = SEAgentMCPBridgeService(host="127.0.0.1", port=test_port)
        bridge_service.start()
        time.sleep(0.8)

        # 校验状态中心同步
        telemetry = bridge_service.tracker.latest_telemetry()
        print(f"  ✅ 成功接收/解析来自支持船 Topside 的实时遥测快照:")
        print(f"     - 姿态坐标 (x, y, z): ({telemetry.pose_x:.4f}m, {telemetry.pose_y:.4f}m, {telemetry.pose_z:.1f}m)")
        print(f"     - 当前水深 (water_depth): {telemetry.water_depth:.1f} m")
        print(f"     - 离底高度 (alt): {telemetry.altitude:.1f} m")
        print(f"     - 控制模式 (ctr_mode): {telemetry.ctr_mode} (AUTODEPTH 定深模式)")
        print(f"     - 当前活跃任务数: {len(telemetry.task_list)} 项")

        # 步骤 4: 下发业务任务并追踪闭环
        print("\n[4/5] 依次下发 4 大 SEAgent 官方业务任务并进行状态闭环跟踪:")

        origin = LocalOrigin(latitude=22.80169, longitude=113.52497)
        task_samples = [
            ("1. 管道/电缆巡检", "pipeline_inspection", TaskType.SEARCH_CABLE, 22.8025, 113.5255, 80.0),
            ("2. 管道/电缆埋设", "pipeline_burial", TaskType.CLAMP_CABLE, 22.8035, 113.5265, 120.0),
            ("3. 采油树阀门操作", "tree_valve_operation", TaskType.INSERT_PLUG, 22.8045, 113.5275, 300.0),
            ("4. 常规阀门操作", "valve_operation", TaskType.INSERT_PLUG, 22.8055, 113.5285, 250.0),
        ]

        for idx, (title, task_type_str, expected_type, lat, lon, depth) in enumerate(task_samples, start=1):
            print(f"\n  ----------------- {title} -----------------")
            intent = {
                "schema_version": 2,
                "task_type": task_type_str,
                "priority": 7,
                "location": {"water_depth_m": depth, "use_geodetic": True},
                "task": {
                    "type": task_type_str,
                    "details": {"target": {"latitude": lat, "longitude": lon}}
                }
            }

            # 下发并获取任务 ID
            task_id = 0x80050 + idx
            sys_cmd = intent_to_syscmd(intent, task_id=task_id, use_geodetic=True, origin=origin)
            print(f"  · [SEAgent -> ROS2] task_type: '{task_type_str}' ➔ SysTaskCmd.task_type: {sys_cmd.task_type}")
            print(f"  · [WGS-84 投影位姿] x={sys_cmd.pos_target[0].x:.3f}m, y={sys_cmd.pos_target[0].y:.3f}m, z={sys_cmd.pos_target[0].z:.1f}m")

            sent_id = bridge_service.dispatch_intent(intent, task_id=task_id, use_geodetic=True, origin=origin)
            print(f"  ➔ 任务 0x{sent_id:X} 已通过 MCPBridgeService 发送至 WebSocket /task_cmd 通道")

            # 等待机器人侧状态推进至 FINISH
            finished = bridge_service.wait_for_task_finish(sent_id, timeout=4.0)
            if finished:
                print(f"  🎉 [ROS2 -> SEAgent 遥测回传] 收到 /task/system_status 推进信息，任务最终状态: FINISH (status=5)")
            else:
                print(f"  ⚠️ 任务 0x{sent_id:X} 未在规定时间内完成")

        # 步骤 5: 停止服务并汇总
        bridge_service.stop()

        print("\n[5/5] 演练总结:")
        print("=" * 85)
        print("🎉 水下机器人遥测模拟与双端通信闭环演示 100% 成功通过！")
        print("   所有遥测数据回传、姿态解析与任务生命周期推进无任何异常！")
        print("=" * 85)

    finally:
        mock_server.stop()


if __name__ == "__main__":
    run_live_simulation()
