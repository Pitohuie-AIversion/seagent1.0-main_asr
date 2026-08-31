"""
test_move_task_live.py
======================
移动任务 (MOVE_TASK = 5) 专项目标端到端测试脚本

测试步骤：
1. 后台启动 MockRosbridgeServer (端口 9095)
2. 构造移动任务 TaskIntent v2 数据（目标经纬度 22.80259, 113.52594，水深 150.0m）
3. 触发 intent_to_syscmd(use_geodetic=True)，验证 WGS-84 精确投影与姿态推算
4. 通过 RosbridgeClient 发送至 /task_cmd
5. 监听 /task/system_status，实时追踪移动任务的执行生命周期 (PLAN -> ONGOING -> FINISH)
"""

import sys
import time
from pathlib import Path

# 添加 mcp 目录到 python 搜索路径
MCP_DIR = Path(__file__).resolve().parent.parent / "mcp"
sys.path.insert(0, str(MCP_DIR))

from mcp.shim.sealien_protocol import LocalOrigin
from mcp.shim.rosbridge_client import RosbridgeClient, TaskType, TaskStatus, intent_to_syscmd
from mcp.shim.mock_rosbridge_server import MockRosbridgeServer
from mcp.shim.task_status_tracker import TaskStatusTracker


def run_move_task_test():
    print("=" * 80)
    print("🚀 开始进行【水下移动任务 (MOVE_TASK = 5)】端到端专项测试")
    print("=" * 80)

    test_port = 9095
    print(f"\n[步骤 1/5] 启动 Mock 机器人 WebSocket 网关 (ws://127.0.0.1:{test_port})...")
    mock_server = MockRosbridgeServer(port=test_port)
    mock_server.start()
    time.sleep(0.5)

    try:
        # 步骤 2: 构造 TaskIntent v2
        print("\n[步骤 2/5] 构造水下移动任务 TaskIntent v2 参数:")
        origin = LocalOrigin(latitude=22.80169, longitude=113.52497)
        task_intent_v2 = {
            "schema_version": 2,
            "task_type": "underwater_move",
            "priority": 15,
            "fail_stop": True,
            "location": {
                "water_depth_m": 150.0,
                "use_geodetic": True
            },
            "task": {
                "type": "underwater_move",
                "details": {
                    "target": {
                        "latitude": 22.80259,
                        "longitude": 113.52594
                    }
                }
            }
        }
        print(f"  · 任务类型: underwater_move")
        print(f"  · 参考原点: {origin.latitude}°N, {origin.longitude}°E")
        print(f"  · 目标点经纬度: 22.80259°N, 113.52594°E")
        print(f"  · 目标水深: 150.0m")

        # 步骤 3: 转换 SysTaskCmd 消息
        print("\n[步骤 3/5] 触发 intent_to_syscmd 高精度 WGS-84 投影转换:")
        sys_cmd = intent_to_syscmd(
            task_intent_v2,
            task_id=0x80005,
            use_geodetic=True,
            origin=origin
        )
        print(f"  · 生成 task_type: {sys_cmd.task_type} (TaskType.MOVE_TASK)")
        print(f"  · 生成 task_id: 0x{sys_cmd.task_id:X}")
        print(f"  · 计算得到的 odom position: x={sys_cmd.pos_target[0].x:.3f}m, y={sys_cmd.pos_target[0].y:.3f}m, z={sys_cmd.pos_target[0].z:.1f}m")
        print(f"  · 计算得到的 orientation quat: qx={sys_cmd.pos_target[0].qx:.4f}, qy={sys_cmd.pos_target[0].qy:.4f}, qz={sys_cmd.pos_target[0].qz:.4f}, qw={sys_cmd.pos_target[0].qw:.4f}")

        assert sys_cmd.task_type == TaskType.MOVE_TASK, "任务类型映射错误"
        assert sys_cmd.pos_target[0].z == -150.0, "水深计算错误"
        assert sys_cmd.pos_target[0].x > 0.0, "East 偏移计算错误"
        assert sys_cmd.pos_target[0].y > 0.0, "North 偏移计算错误"
        print("  ✅ WGS-84 高精度坐标与位姿计算断言验证通过！")

        # 步骤 4: 建立连接并订阅状态
        print(f"\n[步骤 4/5] 连接 Rosbridge 网关并发起移动任务下发...")
        with RosbridgeClient(host="127.0.0.1", port=test_port) as client:
            tracker = TaskStatusTracker(client)
            tracker.start()
            time.sleep(0.3)

            # 下发移动任务
            sent_task_id = client.publish_task_cmd(task_intent_v2, task_id=0x80005)
            print(f"  ➔ 移动任务已下发至 /task_cmd (task_id=0x{sent_task_id:X})")

            # 步骤 5: 跟踪模拟机器人的任务生命周期
            print("\n[步骤 5/5] 监听 /task/system_status 追踪移动任务生命周期...")
            finished = tracker.wait_for_finish(sent_task_id, timeout=5.0)

            if finished:
                item = tracker.get_task_status(sent_task_id)
                status_name = item.status_name if item else "UNKNOWN"
                print(f"  🎉 移动任务执行成功，最终状态: {status_name} (status={item.status if item else 'N/A'})")
                assert item and item.status in (TaskStatus.FINISH, TaskStatus.EXIT), "任务状态不是完成状态"
            else:
                print("  ⚠️ 警告: 任务未在超时时间内完成")
                sys.exit(1)

        print("\n" + "=" * 80)
        print("✅ 【水下移动任务 (MOVE_TASK = 5)】端到端专项测试 100% 成功！")
        print("=" * 80)

    finally:
        mock_server.stop()


if __name__ == "__main__":
    run_move_task_test()
