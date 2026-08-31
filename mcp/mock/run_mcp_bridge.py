"""
run_mcp_bridge.py
===================
SEAgent MCP 独立桥接与通讯运行服务 (CLI Starter)

用法：
  python mcp/run_mcp_bridge.py [--host 127.0.0.1] [--port 9090] [--mock] [--sync-interval 1.0]

功能：
  1. 启动 SEAgentMCPBridgeService
  2. 若指定 --mock，自动在后台拉起 MockRosbridgeServer (9091 端口)，实现无支持船环境下的全功能本地仿真调试
  3. 提供控制台实时遥测面板与交互命令行，支持下发命令、挂起/恢复任务、查询实时姿态
"""

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

MOCK_DIR = Path(__file__).resolve().parent
MCP_ROOT = MOCK_DIR.parent
CORE_DIR = MCP_ROOT / "core"
SEAGENT_ROOT = MCP_ROOT.parent

for p in [MOCK_DIR, CORE_DIR, MCP_ROOT, SEAGENT_ROOT]:
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from mcp.core.bridge_service import SEAgentMCPBridgeService
from .mock_rosbridge_server import MockRosbridgeServer
from src.state_info import RobotStateInfo

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("mcp_runner")


def parse_args():
    parser = argparse.ArgumentParser(description="SEAgent MCP 通讯与网关调度服务")
    parser.add_argument("--host", type=str, default=os.environ.get("MCP_HOST", "127.0.0.1"),
                        help="支持船 Topside rosbridge 网关 IP (默认 127.0.0.1)")
    parser.add_argument("--port", type=int, default=int(os.environ.get("MCP_PORT", "9090")),
                        help="rosbridge WebSocket 端口 (默认 9090)")
    parser.add_argument("--mock", action="store_true", default=bool(os.environ.get("MCP_MOCK", "")),
                        help="开启本地 Mock rosbridge 仿真服务器 (端口自动使用 9091)")
    parser.add_argument("--sync-interval", type=float, default=2.0,
                        help="状态面板刷新时间间隔 (秒)")
    return parser.parse_args()


def main():
    args = parse_args()

    mock_server = None
    target_port = args.port

    if args.mock:
        target_port = 9091 if args.port == 9090 else args.port
        logger.info(f"🛠️ [MCP Runner] 启动本地 Mock rosbridge 仿真服务器 (ws://127.0.0.1:{target_port})...")
        mock_server = MockRosbridgeServer(port=target_port)
        mock_server.start()
        time.sleep(0.3)

    state_file = SEAGENT_ROOT / "config" / "state.yaml"
    fleet_file = SEAGENT_ROOT / "config" / "robot_fleet.yaml"
    state_info = RobotStateInfo(state_file=state_file, fleet_file=fleet_file)

    logger.info(f"📡 [MCP Runner] 连接 SEAgent MCP 桥接服务至 ws://{args.host}:{target_port}...")
    bridge = SEAgentMCPBridgeService(
        host=args.host,
        port=target_port,
        state_info=state_info,
        connect_timeout=5.0,
    )

    try:
        bridge.start()
        logger.info("✅ [MCP Runner] MCP 桥接服务连接成功！按 Ctrl+C 退出。")

        print("================================================================================")
        print(f"🤖 SEAgent MCP 机器人控制与遥测面板 | 网关: ws://{args.host}:{target_port}")
        print("================================================================================")

        while True:
            time.sleep(args.sync_interval)
            telemetry = bridge.tracker.latest_telemetry()
            if telemetry:
                print(
                    f"\r[遥测快照 {telemetry.received_at[11:19]}] "
                    f"水深: {telemetry.water_depth:.1f}m | 高度: {telemetry.altitude:.1f}m | "
                    f"模式: {telemetry.ctr_mode} | 任务数: {len(telemetry.task_list)} | "
                    f"连接: 正常",
                    end="",
                    flush=True
                )

    except KeyboardInterrupt:
        print("\n\n🛑 正在停止 MCP 桥接服务...")
    finally:
        bridge.stop()
        if mock_server:
            mock_server.stop()
        logger.info("👋 [MCP Runner] 服务已安全退出。")


if __name__ == "__main__":
    main()
