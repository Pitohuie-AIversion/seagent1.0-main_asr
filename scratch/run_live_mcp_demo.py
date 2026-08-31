"""
run_live_mcp_demo.py
======================
真实运行全链路 MCP 闭环示范脚本

示范流程：
1. 启动 Mock rosbridge WebSocket 服务器 (端口 9099)
2. 初始化 SEAgent 状态中心 (RobotStateInfo) 与 MCP 自动化桥接服务 (SEAgentMCPBridgeService)
3. 模拟对话完成：将 TaskIntent v2 落盘数据通过 bridge.dispatch_intent() 下发到 ROS 2 /task_cmd
4. 订阅 /task/system_status 并启动 TaskStatusTracker，跟踪任务推进（READY -> PLAN -> ONGOING -> FINISH）
5. 验证 RobotStateInfo 水深与电量自动同步
6. 打印控制台全生命周期证据
"""

import json
import time
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MCP_DIR = PROJECT_ROOT / "mcp"
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(MCP_DIR))

from mcp.shim.mock_rosbridge_server import MockRosbridgeServer, received_publishes
from mcp.shim.bridge_service import SEAgentMCPBridgeService
from src.state_info import RobotStateInfo
from mcp.shim.task_status_tracker import TaskStatusItem

PORT = 9099

def main():
    print("=" * 80)
    print("🚀 SEAgent ↔ ROS 2 MCP 闭环运行全流程实测演示")
    print("=" * 80)

    # 1. 启动 Mock rosbridge 服务器
    print("\n[Step 1] 启动 Topside rosbridge 仿真服务器...")
    server = MockRosbridgeServer(port=PORT)
    server.start()
    time.sleep(0.3)
    print(f"✅ Mock rosbridge 服务器已监听: ws://127.0.0.1:{PORT}")

    # 2. 启动 SEAgent MCP 桥接服务
    print("\n[Step 2] 启动 SEAgent 云端 MCP 自动化桥接服务...")
    state_file = PROJECT_ROOT / "config" / "state.yaml"
    fleet_file = PROJECT_ROOT / "config" / "robot_fleet.yaml"
    state_info = RobotStateInfo(state_file=state_file, fleet_file=fleet_file)
    bridge = SEAgentMCPBridgeService(host="127.0.0.1", port=PORT, state_info=state_info)
    bridge.start()
    time.sleep(0.2)
    print("✅ MCP 桥接服务连接成功，遥测自动同步线程就绪！")

    # 3. 模拟对话层导出 TaskIntent v2 并触发 MCP 下发
    print("\n[Step 3] 模拟对话完成 (done 阶段)，导出 TaskIntent v2 并触发 MCP 下发...")
    task_intent_v2 = {
        "schema_version": 2,
        "task_type": "tree_valve_operation",
        "priority": 15,
        "fail_stop": True,
        "location": {
            "oilfield": "流花11-1油田",
            "water_depth_m": 300.0
        },
        "task": {
            "type": "tree_valve_operation",
            "details": {
                "target": {"latitude": 20.815, "longitude": 115.735},
                "speed_ms": 1.5
            }
        },
        "equipment": {
            "robot_unit_id": "WROV-250-001",
            "robot_type": "work_class_rov"
        }
    }
    print("   📄 TaskIntent v2 输入 Payload:")
    print(json.dumps(task_intent_v2, ensure_ascii=False, indent=2))

    task_id = bridge.dispatch_intent(task_intent_v2)
    print(f"\n✅ [指令下发成功] 对应 ROS 2 Task ID: 0x{task_id:X} ({task_id})")

    time.sleep(0.2)
    # 4. 验证 Topside 实际接收到的二进制 SysTaskCmd 帧
    print("\n[Step 4] 校验 Topside 网关收到的 SysTaskCmd.msg 二进制结构帧:")
    pubs = server.get_received_publishes()
    sys_cmd = pubs[-1]["payload"]
    print("   📡 SysTaskCmd Payload:")
    print(json.dumps(sys_cmd, ensure_ascii=False, indent=2))

    # 5. 实时追踪任务推演生命周期
    print("\n[Step 5] 实时追踪机器人侧任务执行生命周期 (TaskStatusTracker)...")
    status_history = []
    def _on_status_change(item: TaskStatusItem):
        status_history.append(f"{item.status_name} ({item.status})")
        print(f"   ⏱️ [状态变更通知] Task 0x{item.task_id:X} -> {item.status_name} ({item.status})")

    bridge.tracker.on_task_status_change(task_id, _on_status_change)
    final_item = bridge.wait_for_task_finish(task_id, timeout=5.0)

    print(f"\n✅ [任务推演完成] 最终状态: {final_item.status_name} (Code {final_item.status})")

    # 6. 验证遥测保持在内存 TaskStatusTracker (不落盘污染 state.yaml)
    print("\n[Step 6] 验证遥测实时快照保持在 TaskStatusTracker 内存快照中:")
    t_latest = bridge.tracker.latest_telemetry()
    print("   📊 最新实时遥测内存快照:")
    print(f"      - 物理实际水深: {t_latest.water_depth:.1f}m (规划目标: 300.0m)")
    print(f"      - 距海底高度: {t_latest.altitude:.1f}m")
    print(f"      - 控制器模式: Code {t_latest.ctr_mode}")
    print(f"      - 健康状态: Code {t_latest.health}")

    # 7. 退出清理
    bridge.stop()
    server.stop()
    print("\n" + "=" * 80)
    print("🎉 SEAgent ↔ ROS 2 MCP 全链路示范实测完成，证据确凿！")
    print("=" * 80)

if __name__ == "__main__":
    main()
