"""
test_four_official_business_tasks.py
======================================
SEAgent 4 大官方业务任务类型 ROS 2 双向通信端到端测试脚本

测试的 4 大官方业务 TaskType:
1. pipeline_inspection   (管道/电缆巡检) -> SEARCH_CABLE = 2 (巡缆, 观察级ROV / AUV)
2. pipeline_burial       (管道/电缆埋设) -> CLAMP_CABLE = 1  (夹缆, 工作级ROV)
3. tree_valve_operation  (采油树阀门操作) -> INSERT_PLUG = 4 (插销/阀门, 工作级ROV)
4. valve_operation       (常规阀门/插拔操作) -> INSERT_PLUG = 4 (插销/阀门, 工作级ROV)

双向验证内容：
- 下发流 (Client -> ROS2 /task_cmd): TaskIntent v2 转换、TaskType 映射、WGS-84 坐标与位姿计算、WebSocket 传输
- 回传流 (ROS2 /task/system_status -> Client): SysStatus 遥测解析、任务生命周期追踪 (PLAN -> ONGOING -> FINISH)
"""

import sys
import time
from pathlib import Path

# 添加项目目录与 mcp 目录
SEAGENT_ROOT = Path(__file__).resolve().parent.parent
MCP_DIR = SEAGENT_ROOT / "mcp"
CORE_DIR = MCP_DIR / "core"
MOCK_DIR = MCP_DIR / "mock"
sys.path.insert(0, str(CORE_DIR))
sys.path.insert(0, str(MOCK_DIR))
sys.path.insert(0, str(MCP_DIR))
sys.path.insert(0, str(SEAGENT_ROOT))

from mcp.shim.sealien_protocol import LocalOrigin
from mcp.shim.rosbridge_client import RosbridgeClient, TaskType, TaskStatus, intent_to_syscmd
from mcp.shim.mock_rosbridge_server import MockRosbridgeServer
from mcp.shim.task_status_tracker import TaskStatusTracker


def get_official_business_intent(task_type: str, robot_type: str, lat: float, lon: float, depth: float) -> dict:
    """按 SEAgent 规范生成 4 大官方业务意图的数据样本"""
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
            "payload": ["多功能液压机械臂", "水下视觉防爆摄像头"],
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

    # 如果是巡缆任务，额外补充航点信息 (起点与终点)
    if task_type == "pipeline_inspection":
        intent["task"]["details"]["waypoints"] = [
            {"latitude": lat, "longitude": lon, "depth": depth},
            {"latitude": lat + 0.001, "longitude": lon + 0.001, "depth": depth}
        ]

    return intent


def run_four_official_tasks_bidirectional_test():
    print("=" * 85)
    print("🚀 SEAgent 4 大官方业务 TaskType ROS 2 双向通信端到端测试")
    print("=" * 85)

    test_port = 9097
    print(f"\n[1/4] 启动支持船 Topside Mock ROS 2 网关 (ws://127.0.0.1:{test_port})...")
    mock_server = MockRosbridgeServer(port=test_port)
    mock_server.start()
    time.sleep(0.5)

    try:
        # 定义 4 大官方业务测试集
        official_tasks = [
            {
                "name": "1. 管道/电缆巡检",
                "task_type": "pipeline_inspection",
                "robot_type": "observation_rov",
                "expected_ros2_type": TaskType.SEARCH_CABLE, # 2
                "expected_ros2_name": "SEARCH_CABLE (巡缆)",
                "lat": 22.8025, "lon": 113.5255, "depth": 80.0
            },
            {
                "name": "2. 管道/电缆埋设",
                "task_type": "pipeline_burial",
                "robot_type": "work_class_rov",
                "expected_ros2_type": TaskType.CLAMP_CABLE,  # 1
                "expected_ros2_name": "CLAMP_CABLE (夹缆/埋设)",
                "lat": 22.8035, "lon": 113.5265, "depth": 120.0
            },
            {
                "name": "3. 采油树阀门操作",
                "task_type": "tree_valve_operation",
                "robot_type": "work_class_rov",
                "expected_ros2_type": TaskType.INSERT_PLUG,  # 4
                "expected_ros2_name": "INSERT_PLUG (插销/采油树)",
                "lat": 22.8045, "lon": 113.5275, "depth": 300.0
            },
            {
                "name": "4. 常规阀门/插拔操作",
                "task_type": "valve_operation",
                "robot_type": "work_class_rov",
                "expected_ros2_type": TaskType.INSERT_PLUG,  # 4
                "expected_ros2_name": "INSERT_PLUG (插销/常规阀门)",
                "lat": 22.8055, "lon": 113.5285, "depth": 250.0
            },
        ]

        origin = LocalOrigin(latitude=22.80169, longitude=113.52497)
        print("\n[2/4] 连接 SEAgent 生产级 Rosbridge Client 并启动状态追踪器...")

        with RosbridgeClient(host="127.0.0.1", port=test_port) as client:
            tracker = TaskStatusTracker(client)
            tracker.start()
            time.sleep(0.3)

            print("\n[3/4] 逐一执行 4 大业务 TaskType 的【下发流 + 转换校验 + 遥测回传】闭环测试：")

            for idx, task_info in enumerate(official_tasks, start=1):
                print(f"\n  ----------------- 测试用例 #{idx}: {task_info['name']} -----------------")
                intent = get_official_business_intent(
                    task_info["task_type"],
                    task_info["robot_type"],
                    task_info["lat"],
                    task_info["lon"],
                    task_info["depth"]
                )

                print(f"  · SEAgent 业务 TaskType: '{intent['task_type']}' (匹配机器人: {task_info['robot_type']})")
                print(f"  · 大地坐标输入: {task_info['lat']}°N, {task_info['lon']}°E, 水深: {task_info['depth']}m")

                # 阶段 1: 触发转换并断言
                task_id = 0x80020 + idx
                sys_cmd = intent_to_syscmd(intent, task_id=task_id, use_geodetic=True, origin=origin)
                
                print(f"  · 映射得到的 ROS 2 task_type: {sys_cmd.task_type} (预期的 {task_info['expected_ros2_name']})")
                print(f"  · WGS-84 ENU 转换坐标: x={sys_cmd.pos_target[0].x:.3f}m, y={sys_cmd.pos_target[0].y:.3f}m, z={sys_cmd.pos_target[0].z:.1f}m")
                print(f"  · 目标位姿数量: {len(sys_cmd.pos_target)} 个")

                # 校验断言
                assert sys_cmd.task_type == task_info["expected_ros2_type"], f"TaskType 映射错误: {sys_cmd.task_type} != {task_info['expected_ros2_type']}"
                assert sys_cmd.pos_target[0].z == -task_info["depth"], "水深转换错误"
                print("  ✅ 下发转换契约与 WGS-84 坐标断言全数通过")

                # 阶段 2: 下发至 ROS2 /task_cmd 话题
                sent_id = client.publish_task_cmd(intent, task_id=task_id)
                print(f"  ➔ 成功将 SysTaskCmd 下发至 ROS 2 /task_cmd (task_id=0x{sent_id:X})")

                # 阶段 3: 监听 /task/system_status 进行遥测状态闭环跟踪
                finished = tracker.wait_for_finish(sent_id, timeout=4.0)
                assert finished, f"任务 0x{sent_id:X} 未在规定时间内返回 FINISH 状态"
                
                task_item = tracker.get_task_status(sent_id)
                status_str = task_item.status_name if task_item else "UNKNOWN"
                print(f"  🎉 收到 /task/system_status 遥测回传，任务状态成功推进至: {status_str} (status={task_item.status if task_item else 'N/A'})")

        print("\n[4/4] 汇总与验证结论：")
        print("=" * 85)
        print("🎉 SEAgent 4 大官方业务 TaskType 的 ROS 2 双向通信测试 100% 成功！")
        print("  1. pipeline_inspection  ➔ SEARCH_CABLE (2) 双向收发与遥测闭环成功")
        print("  2. pipeline_burial      ➔ CLAMP_CABLE  (1) 双向收发与遥测闭环成功")
        print("  3. tree_valve_operation ➔ INSERT_PLUG  (4) 双向收发与遥测闭环成功")
        print("  4. valve_operation      ➔ INSERT_PLUG  (4) 双向收发与遥测闭环成功")
        print("=" * 85)

    finally:
        mock_server.stop()


if __name__ == "__main__":
    run_four_official_tasks_bidirectional_test()
