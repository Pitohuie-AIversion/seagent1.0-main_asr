"""
test_all_intents_as_move_task.py
================================
模型端任务数据结构测试脚本：
将包含 采油树作业、管道巡检、电缆埋设、设备控制 等各种任务模板，
统一重包/识别为 移动任务 (underwater_move -> TaskType.MOVE_TASK = 5)，
验证模型端生成的数据结构转换与下发契约。
"""

import sys
import time
from pathlib import Path

# 添加项目目录与 mcp 目录
SEAGENT_ROOT = Path(__file__).resolve().parent.parent
MCP_DIR = SEAGENT_ROOT / "mcp"
sys.path.insert(0, str(MCP_DIR))
sys.path.insert(0, str(SEAGENT_ROOT))

from mcp.shim.sealien_protocol import LocalOrigin
from mcp.shim.rosbridge_client import RosbridgeClient, TaskType, TaskStatus, intent_to_syscmd
from mcp.shim.mock_rosbridge_server import MockRosbridgeServer
from mcp.shim.task_status_tracker import TaskStatusTracker


def get_seagent_canonical_intent_template(task_name: str, lat: float, lon: float, depth: float) -> dict:
    """标准 SEAgent TaskIntent v2 模板样本生成器"""
    return {
        "schema_version": 2,
        "internal_id": "8f3b2a1c-4d5e-49b8-a123-9876543210ab",
        "task_id": "CT-20260827-001",
        "intent_id": "TI2026082701",
        "task_type": task_name,
        "priority": 7,
        "time": {
            "start": "2026-08-27T10:00:00+08:00",
            "end": "2026-08-27T18:00:00+08:00"
        },
        "location": {
            "oilfield": "南海流花油田",
            "water_depth_m": depth,
            "use_geodetic": True
        },
        "task": {
            "type": task_name,
            "details": {
                "wellhead_id": "LH-01井口",
                "target": {
                    "latitude": lat,
                    "longitude": lon
                }
            }
        },
        "equipment": {
            "robot_type": "work_class_rov",
            "payload": ["多功能液压机械臂", "双目视觉模块"],
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


def run_model_structure_move_wrapper_test():
    print("=" * 80)
    print("🎯 SEAgent 任务模板与模型端数据结构测试 (统一重包为移动任务 underwater_move)")
    print("=" * 80)

    test_port = 9096
    print(f"\n[步骤 1/4] 启动本地 Mock 机器人 Gateway (ws://127.0.0.1:{test_port})...")
    mock_server = MockRosbridgeServer(port=test_port)
    mock_server.start()
    time.sleep(0.5)

    try:
        # 测试用例样本集合（原各种业务意图）
        test_samples = [
            ("采油树阀门作业", "tree_valve_operation", 22.8020, 113.5250, 300.0),
            ("管道巡检任务", "pipeline_inspection", 22.8030, 113.5260, 80.0),
            ("电缆埋设作业", "cable_burial", 22.8040, 113.5270, 120.0),
            ("标准移动任务", "underwater_move", 22.8050, 113.5280, 150.0),
        ]

        origin = LocalOrigin(latitude=22.80169, longitude=113.52497)
        print(f"\n[步骤 2/4] 加载 SEAgent TaskIntent v2 规范模板，统一包装为 underwater_move：")

        with RosbridgeClient(host="127.0.0.1", port=test_port) as client:
            tracker = TaskStatusTracker(client)
            tracker.start()
            time.sleep(0.3)

            for idx, (label, orig_type, lat, lon, depth) in enumerate(test_samples, start=1):
                print(f"\n  ----------------- 样本 #{idx}: {label} ({orig_type}) -----------------")
                # 生成原意图
                raw_intent = get_seagent_canonical_intent_template(orig_type, lat, lon, depth)
                
                # 【核心包装步骤】：统一重包/识别为 移动任务 (underwater_move)
                wrapped_intent = dict(raw_intent)
                wrapped_intent["task_type"] = "underwater_move"
                wrapped_intent["task"]["type"] = "underwater_move"

                print(f"  · 原始业务意图: {orig_type} ➔ 模型包装为: {wrapped_intent['task_type']}")
                print(f"  · 目标经纬度: ({lat}°N, {lon}°E), 水深: {depth}m")

                # 转换为 ROS 2 消息结构
                sys_cmd = intent_to_syscmd(wrapped_intent, task_id=0x80010 + idx, use_geodetic=True, origin=origin)
                
                print(f"  · 底层 SysTaskCmd task_type: {sys_cmd.task_type} (MOVE_TASK = 5)")
                print(f"  · 底层 SysTaskCmd task_id: 0x{sys_cmd.task_id:X}")
                print(f"  · 计算位姿: x={sys_cmd.pos_target[0].x:.3f}m, y={sys_cmd.pos_target[0].y:.3f}m, z={sys_cmd.pos_target[0].z:.1f}m")

                # 校验转换结果
                assert sys_cmd.task_type == TaskType.MOVE_TASK, f"样本 #{idx} 任务类型映射不为 MOVE_TASK"
                assert sys_cmd.pos_target[0].z == -depth, f"样本 #{idx} 水深不匹配"

                # 模拟发送至 ROS2 通道
                sent_id = client.publish_task_cmd(wrapped_intent, task_id=0x80010 + idx)
                print(f"  ➔ 成功下发至 WebSocket 通道 /task_cmd (task_id=0x{sent_id:X})")

                # 等待机器人完成
                finished = tracker.wait_for_finish(sent_id, timeout=3.0)
                assert finished, f"样本 #{idx} 在机器人侧未成功推进完成"
                print(f"  ✅ 样本 #{idx} 机器人侧执行跟踪完成: FINISH (status=5)")

        print("\n" + "=" * 80)
        print("🎉 全部 4 组模板数据结构测试通过！模型端重包为移动任务结构校验 100% 成功。")
        print("=" * 80)

    finally:
        mock_server.stop()


if __name__ == "__main__":
    run_model_structure_move_wrapper_test()
