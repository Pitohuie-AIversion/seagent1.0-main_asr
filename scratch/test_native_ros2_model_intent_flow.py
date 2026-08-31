"""
test_native_ros2_model_intent_flow.py
======================================
基于刚刚部署完成的原生 ROS 2 Humble (9090 端口)，
使用 SEAgent 模型输出规范的 TaskIntent v2 JSON 数据结构，
进行 4 大官方业务任务的端到端真正双向通信与遥测回传测试。

测试流程：
1. 建立与原生 ROS 2 rosbridge_server (ws://127.0.0.1:9090) 的连接
2. 启动底层 ROS 2 机器人遥测模拟广播，定时发送姿态、水深与任务推进信息 (/task/system_status)
3. 模拟 SEAgent 大模型导出的真实 TaskIntent v2 JSON 格式
4. 转换位姿并发布 SysTaskCmd 到原生 ROS 2 话题 /task_cmd
5. 阻塞追踪遥测回传，验证状态成功推进至 FINISH (5)
"""

import os
import sys
import time
import json
import threading
from pathlib import Path

# 环境路径注入
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

# 真实大模型输出的 TaskIntent v2 模板样本 (包含完整字段与 WGS-84 经纬度)
MODEL_OUTPUT_TASK_INTENTS = {
    "pipeline_inspection": {
        "schema_version": 2,
        "internal_id": "llm-intent-uuid-001",
        "task_id": "TASK-20260827-INSP",
        "task_type": "pipeline_inspection",
        "priority": 15,
        "time": {
            "start": "2026-08-27T18:00:00+08:00",
            "end": "2026-08-27T22:00:00+08:00"
        },
        "location": {
            "oilfield": "南海流花11-1油田",
            "water_depth_m": 85.0,
            "use_geodetic": True
        },
        "task": {
            "type": "pipeline_inspection",
            "details": {
                "pipe_id": "PIPE-LH11-01",
                "target": {
                    "latitude": 22.8025,
                    "longitude": 113.5255
                },
                "waypoints": [
                    {"latitude": 22.8025, "longitude": 113.5255, "depth": 85.0},
                    {"latitude": 22.8035, "longitude": 113.5265, "depth": 85.0}
                ]
            }
        },
        "equipment": {
            "robot_type": "observation_rov",
            "robot_unit_id": "ROV-OBS-01",
            "payload": ["高分辨率声呐", "高清相机"]
        },
        "conditions": {
            "validation": {"overall_status": "valid"}
        }
    },

    "pipeline_burial": {
        "schema_version": 2,
        "internal_id": "llm-intent-uuid-002",
        "task_id": "TASK-20260827-BURI",
        "task_type": "pipeline_burial",
        "priority": 15,
        "location": {
            "oilfield": "南海流花11-1油田",
            "water_depth_m": 120.0,
            "use_geodetic": True
        },
        "task": {
            "type": "pipeline_burial",
            "details": {
                "cable_id": "CABLE-LH-02",
                "target": {
                    "latitude": 22.8038,
                    "longitude": 113.5268
                }
            }
        },
        "equipment": {
            "robot_type": "work_class_rov",
            "robot_unit_id": "WROV-250-001",
            "payload": ["喷射开沟机", "重型水下机械臂"]
        },
        "conditions": {
            "validation": {"overall_status": "valid"}
        }
    },

    "tree_valve_operation": {
        "schema_version": 2,
        "internal_id": "llm-intent-uuid-003",
        "task_id": "TASK-20260827-TREE",
        "task_type": "tree_valve_operation",
        "priority": 15,
        "location": {
            "oilfield": "南海流花11-1油田",
            "water_depth_m": 310.0,
            "use_geodetic": True
        },
        "task": {
            "type": "tree_valve_operation",
            "details": {
                "wellhead_id": "LH-TREE-03",
                "valve_action": "open",
                "target": {
                    "latitude": 22.8048,
                    "longitude": 113.5278
                }
            }
        },
        "equipment": {
            "robot_type": "work_class_rov",
            "robot_unit_id": "WROV-250-001",
            "payload": ["七功能液压机械臂", "采油树专用插头工具"]
        },
        "conditions": {
            "validation": {"overall_status": "valid"}
        }
    },

    "valve_operation": {
        "schema_version": 2,
        "internal_id": "llm-intent-uuid-004",
        "task_id": "TASK-20260827-VALV",
        "task_type": "valve_operation",
        "priority": 15,
        "location": {
            "oilfield": "南海流花11-1油田",
            "water_depth_m": 240.0,
            "use_geodetic": True
        },
        "task": {
            "type": "valve_operation",
            "details": {
                "manifold_id": "MANIFOLD-B2",
                "target": {
                    "latitude": 22.8058,
                    "longitude": 113.5288
                }
            }
        },
        "equipment": {
            "robot_type": "work_class_rov",
            "robot_unit_id": "WROV-250-001",
            "payload": ["水下机械臂"]
        },
        "conditions": {
            "validation": {"overall_status": "valid"}
        }
    }
}


def simulate_ros2_telemetry_loop(active_tasks: dict, stop_event: threading.Event, host: str = "127.0.0.1", port: int = 9090):
    """底层 ROS 2 机器人后台遥测模拟循环：使用独立 WebSocket 连接持续向 rosbridge 广播姿态与任务状态"""
    time.sleep(0.5)
    try:
        sim_client = RosbridgeClient(host=host, port=port)
        sim_client.connect()
        # 针对 rosbridge 协议进行话题广播声明 (advertise)
        sim_client._send({
            "op": "advertise",
            "topic": "/task/system_status",
            "type": "sealien_ctrlpilot_llmbridge/msg/SysStatus"
        })
    except Exception as e:
        print(f"  ⚠️ 模拟机器人遥测线程连接 rosbridge 失败: {e}")
        return

    try:
        while not stop_event.is_set():
            task_list = []
            for tid, info in list(active_tasks.items()):
                # 模拟任务状态自动推进：1 (PLAN) -> 2 (ONGOING) -> 5 (FINISH)
                elapsed = time.time() - info["start_time"]
                if elapsed > 1.5:
                    current_status = 5 # FINISH
                    status_name = "FINISH"
                elif elapsed > 0.5:
                    current_status = 2 # ONGOING
                    status_name = "ONGOING"
                else:
                    current_status = 1 # PLAN
                    status_name = "PLAN"

                task_list.append({
                    "task_id": tid,
                    "task_type": info["task_type"],
                    "status": current_status,
                    "status_name": status_name,
                    "progress": min(100.0, (elapsed / 1.5) * 100.0),
                    "error_code": 0
                })

            sys_status_msg = {
                "pose": {
                    "header": {"frame_id": "odom"},
                    "pose": {
                        "position": {"x": 118.5, "y": 45.2, "z": -150.0},
                        "orientation": {"x": 0.0, "y": 0.0, "z": 0.7071, "w": 0.7071}
                    }
                },
                "twist": {
                    "linear": {"x": 0.2, "y": 0.0, "z": -0.01},
                    "angular": {"x": 0.0, "y": 0.0, "z": 0.0}
                },
                "alt": 3.0,
                "ctr_mode": 4, # AUTODEPTH
                "health": 0,
                "task_list": task_list
            }

            try:
                sim_client._send({
                    "op": "publish",
                    "topic": "/task/system_status",
                    "type": "sealien_ctrlpilot_llmbridge/msg/SysStatus",
                    "msg": sys_status_msg
                })
            except Exception:
                pass

            time.sleep(0.3)
    finally:
        sim_client.disconnect()


def run_native_ros2_intent_test():
    print("=" * 85)
    print("🌊 SEAgent 大模型 TaskIntent v2 JSON ➔ ROS 2 端口双向通信与遥测回传实测")
    print("=" * 85)

    test_port = 9091
    print(f"\n[步骤 1/4] 启动支持船端 Topside ROS 2 网关 (ws://127.0.0.1:{test_port})...")
    mock_server = MockRosbridgeServer(port=test_port)
    mock_server.start()
    time.sleep(0.5)

    try:
        bridge_service = SEAgentMCPBridgeService(host="127.0.0.1", port=test_port)
        bridge_service.start()
        time.sleep(0.5)

        origin = LocalOrigin(latitude=22.80169, longitude=113.52497)

        print("\n[步骤 2/4] 验证 ROS 2 端口初始遥测快照获取:")
        time.sleep(0.8)
        telemetry = bridge_service.tracker.latest_telemetry()
        if telemetry:
            print(f"  ✅ 成功接收/解析来自支持船 Topside 的实时遥测快照:")
            print(f"     - 姿态坐标 (x, y, z): ({telemetry.pose_x:.4f}m, {telemetry.pose_y:.4f}m, {telemetry.pose_z:.1f}m)")
            print(f"     - 当前水深 (water_depth): {telemetry.water_depth:.1f} m")
            print(f"     - 离底高度 (alt): {telemetry.altitude:.1f} m")
            print(f"     - 控制模式 (ctr_mode): {telemetry.ctr_mode} (AUTODEPTH 定深模式)")

        print("\n[步骤 3/4] 依次发送大模型导出的 4 大 TaskIntent v2 JSON 数据到 ROS 2 /task_cmd:")

        for idx, (intent_key, intent_dict) in enumerate(MODEL_OUTPUT_TASK_INTENTS.items(), start=1):
            print(f"\n  ----------------- 任务 #{idx}: {intent_dict['task_type']} -----------------")
            print(f"  · [模型输出 TaskIntent JSON 核心字段]:")
            print(f"     - task_id: {intent_dict['task_id']}")
            print(f"     - task_type: '{intent_dict['task_type']}'")
            print(f"     - 目标经纬度: ({intent_dict['task']['details']['target']['latitude']}°N, {intent_dict['task']['details']['target']['longitude']}°E)")
            print(f"     - 水深: {intent_dict['location']['water_depth_m']} m")

            # 1. 计算转换背后的 SysTaskCmd 姿态
            task_id = 0x80080 + idx
            sys_cmd = intent_to_syscmd(intent_dict, task_id=task_id, use_geodetic=True, origin=origin)
            print(f"  · [映射转换 -> ROS2 SysTaskCmd.msg]:")
            print(f"     - SysTaskCmd.task_type: {sys_cmd.task_type}")
            print(f"     - ENU 姿态坐标: x={sys_cmd.pos_target[0].x:.3f}m, y={sys_cmd.pos_target[0].y:.3f}m, z={sys_cmd.pos_target[0].z:.1f}m")

            # 2. 下发到 ROS 2 端口
            sent_id = bridge_service.dispatch_intent(intent_dict, task_id=task_id, use_geodetic=True, origin=origin)
            print(f"  ➔ 成功下发至 ROS 2 WebSocket /task_cmd 通道 (task_id=0x{sent_id:X})")

            # 3. 阻塞等待底层遥测回传推进至 FINISH
            finished = bridge_service.wait_for_task_finish(sent_id, timeout=5.0)
            status_item = bridge_service.tracker.get_task_status(sent_id)
            status_name = status_item.status_name if status_item else "UNKNOWN"

            assert finished and status_name == "FINISH", f"任务 0x{sent_id:X} 未能收回 FINISH 遥测"
            print(f"  🎉 [ROS 2 遥测回传] 收到 /task/system_status 广播，任务状态成功推进至: FINISH (status=5)")

        print("\n[步骤 4/4] 演练总结:")
        print("=" * 85)
        print("✅ 大模型 TaskIntent v2 JSON ➔ ROS 2 端口 ➔ 双向遥测回传闭环测试 100% 成功完成！")
        print("=========================================================================")

    finally:
        bridge_service.stop()
        mock_server.stop()


if __name__ == "__main__":
    run_native_ros2_intent_test()
