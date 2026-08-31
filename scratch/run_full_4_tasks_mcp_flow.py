"""
run_full_4_tasks_mcp_flow.py
============================
在全新物理子目录重构后的 mcp/ 模块下，
执行 4 大 SEAgent 官方业务任务的完整双端通信 (Client <-> Mock ROS2 Gateway) 闭环演练。

测试的 4 大官方业务 TaskType:
1. pipeline_inspection   (管道/电缆巡检) -> TaskType.SEARCH_CABLE (2)
2. pipeline_burial       (管道/电缆埋设) -> TaskType.CLAMP_CABLE (1)
3. tree_valve_operation  (采油树阀门操作) -> TaskType.INSERT_PLUG (4)
4. valve_operation       (常规阀门/插拔操作) -> TaskType.INSERT_PLUG (4)
"""

import sys
import time
from pathlib import Path

# 添加 SEAgent 项目根目录与 mcp 子目录到 sys.path
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
)
from mock import MockRosbridgeServer


def generate_task_intent(task_type: str, robot_type: str, lat: float, lon: float, depth: float) -> dict:
    """按 SEAgent 规范生成标准 TaskIntent v2 字典"""
    intent = {
        "schema_version": 2,
        "internal_id": "8f3b2a1c-4d5e-49b8-a123-9876543210ab",
        "task_id": f"CT-20260827-{task_type[:4].upper()}",
        "intent_id": "TI2026082799",
        "task_type": task_type,
        "priority": 7,
        "time": {
            "start": "2026-08-27T10:00:00+08:00",
            "end": "2026-08-27T18:00:00+08:00"
        },
        "location": {
            "oilfield": "南海流花11-1油田",
            "water_depth_m": depth,
            "use_geodetic": True
        },
        "task": {
            "type": task_type,
            "details": {
                "wellhead_id": "LH-01井口",
                "target": {
                    "latitude": lat,
                    "longitude": lon
                }
            }
        },
        "equipment": {
            "robot_type": robot_type,
            "payload": ["多功能液压机械臂", "水下双目高清相机"],
            "support_vessel": {
                "name": "海洋石油681",
                "latitude": None,
                "longitude": None
            }
        },
        "conditions": {
            "validation": {"overall_status": "valid"},
            "runtime_validation": {"required": False, "status": "completed"}
        }
    }

    if task_type == "pipeline_inspection":
        intent["task"]["details"]["waypoints"] = [
            {"latitude": lat, "longitude": lon, "depth": depth},
            {"latitude": lat + 0.001, "longitude": lon + 0.001, "depth": depth}
        ]

    return intent


def run_full_mcp_4_tasks_flow():
    print("=" * 85)
    print("🚀 SEAgent 重构版 mcp/ 模块：4 大官方业务任务完整双端通信闭环演练")
    print("=" * 85)

    test_port = 9098
    print(f"\n[步骤 1/4] 启动支持船端 Topside Mock ROS 2 网关 (ws://127.0.0.1:{test_port})...")
    mock_server = MockRosbridgeServer(port=test_port)
    mock_server.start()
    time.sleep(0.5)

    try:
        tasks = [
            {
                "title": "任务 #1: 管道/电缆巡检",
                "type": "pipeline_inspection",
                "robot": "observation_rov",
                "expected_ros2_type": TaskType.SEARCH_CABLE,
                "lat": 22.8025, "lon": 113.5255, "depth": 80.0
            },
            {
                "title": "任务 #2: 管道/电缆埋设",
                "type": "pipeline_burial",
                "robot": "work_class_rov",
                "expected_ros2_type": TaskType.CLAMP_CABLE,
                "lat": 22.8035, "lon": 113.5265, "depth": 120.0
            },
            {
                "title": "任务 #3: 采油树阀门操作",
                "type": "tree_valve_operation",
                "robot": "work_class_rov",
                "expected_ros2_type": TaskType.INSERT_PLUG,
                "lat": 22.8045, "lon": 113.5275, "depth": 300.0
            },
            {
                "title": "任务 #4: 常规阀门操作",
                "type": "valve_operation",
                "robot": "work_class_rov",
                "expected_ros2_type": TaskType.INSERT_PLUG,
                "lat": 22.8055, "lon": 113.5285, "depth": 250.0
            },
        ]

        origin = LocalOrigin(latitude=22.80169, longitude=113.52497)
        print("\n[步骤 2/4] 初始化 mcp.core.RosbridgeClient 并连接 Gateway...")

        with RosbridgeClient(host="127.0.0.1", port=test_port) as client:
            tracker = TaskStatusTracker(client)
            tracker.start()
            time.sleep(0.3)

            print("\n[步骤 3/4] 开始依次下发 4 大任务并实时监听机器人遥测状态回传：")

            for idx, task_data in enumerate(tasks, start=1):
                print(f"\n  ----------------- {task_data['title']} -----------------")
                intent = generate_task_intent(
                    task_data["type"], task_data["robot"],
                    task_data["lat"], task_data["lon"], task_data["depth"]
                )

                # 1. 转换 SysTaskCmd
                task_id = 0x80030 + idx
                sys_cmd = intent_to_syscmd(intent, task_id=task_id, use_geodetic=True, origin=origin)
                
                print(f"  · [下发端] SEAgent task_type='{intent['task_type']}' ➔ ROS2 task_type={sys_cmd.task_type}")
                print(f"  · [下发端] 高精度 WGS-84 ENU 姿态坐标: x={sys_cmd.pos_target[0].x:.3f}m, y={sys_cmd.pos_target[0].y:.3f}m, z={sys_cmd.pos_target[0].z:.1f}m")
                assert sys_cmd.task_type == task_data["expected_ros2_type"], "TaskType 不匹配"
                assert sys_cmd.pos_target[0].z == -task_data["depth"], "水深解算错误"

                # 2. 发送到 WebSocket 通道
                sent_id = client.publish_task_cmd(intent, task_id=task_id)
                print(f"  ➔ [WebSocket下发] 已将 SysTaskCmd 打包发送至 /task_cmd (task_id=0x{sent_id:X})")

                # 3. 接收遥测并等待 FINISH
                finished = tracker.wait_for_finish(sent_id, timeout=4.0)
                assert finished, f"任务 0x{sent_id:X} 未成功推进到完成"

                status_item = tracker.get_task_status(sent_id)
                status_name = status_item.status_name if status_item else "UNKNOWN"
                print(f"  🎉 [遥测回传] 收到 /task/system_status 广播，任务状态成功推进为: {status_name} (status={status_item.status if status_item else 'N/A'})")

        print("\n[步骤 4/4] 演练总结：")
        print("=" * 85)
        print("✅ 全套 4 大 SEAgent 官方业务任务完整双端通信演练 100% 成功完成！")
        print("=========================================================================")

    finally:
        mock_server.stop()


if __name__ == "__main__":
    run_full_mcp_4_tasks_flow()
