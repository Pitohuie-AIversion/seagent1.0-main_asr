"""
mock_rosbridge_server.py
=========================
Mock rosbridge WebSocket 服务端（符合完整内部协议）

模拟支持船 Topside 的 rosbridge_server 节点行为：
- 接受 WebSocket 连接（默认端口 9091，避免与真实 9090 冲突）
- 处理 publish（接收 /task_cmd、/task/sys_config 指令）
- 处理 subscribe（推送 /task/system_status 完整 SysStatus 遥测）
- 支持 TASK_MANAGE 指令解析与模拟执行状态推进

Mock 遥测数据符合 sealien_ctrlpilot_msgmanagement/SysStatus.msg 完整结构
（参考 UI接口协议.md 第 3 节）
"""

import asyncio
import json
import threading
from datetime import datetime, timezone


# ============================================================================
# Mock 遥测数据（符合 SysStatus.msg 完整结构）
# ============================================================================

def _make_sys_status(task_list=None):
    """构造符合 SysStatus.msg 规范的完整 mock 数据。
    
    fleet_status 为 Topside 网关的非标准扩展字段，
    聚合多机器人状态供云端 SEAgent 状态中心使用。
    """
    return {
        # === 标准 SysStatus.msg 字段 ===
        "pose": {
            "header": {"frame_id": "odom"},
            "pose": {
                "position":    {"x": 115.3421, "y": 20.8912, "z": -312.4},
                "orientation": {"x": 0.0, "y": 0.0, "z": 0.7071, "w": 0.7071},
            },
        },
        "twist": {
            "linear":  {"x": 0.3,  "y": 0.0, "z": -0.05},
            "angular": {"x": 0.0,  "y": 0.0, "z": 0.01},
        },
        "alt": 2.5,         # 距海底高度 2.5m
        "ctr_mode": 4,      # AUTODEPTH
        "health": 0,        # 无异常
        "task_list": task_list or [],
        # === Topside 网关扩展：多机舰队状态聚合 ===
        "fleet_status": {
            "WROV-250-001": {
                "online": True,
                "current_depth":      312.4,
                "battery_percentage":  94.5,
                "ctr_mode": 4,
            },
            "LROV-150-001": {
                "online": True,
                "current_depth":       85.0,
                "battery_percentage":  88.0,
                "ctr_mode": 9,
            },
        },
    }


# ============================================================================
# 服务端内部状态
# ============================================================================

received_publishes = []          # 所有收到的 publish 消息
active_tasks = {}                # task_id -> task_status_item
_pending_status_steps = {}       # task_id -> [status_序列]
_STATUS_PROGRESSION = [          # 任务状态正常推进序列
    1,   # PLAN
    2,   # ENTER
    3,   # ONGOING
    3,   # ONGOING（多停留一步）
    5,   # FINISH
]


async def handle_client(websocket):
    subscriptions = set()

    async def push_sys_status():
        """推送当前完整 SysStatus（含任务列表）"""
        task_list = list(active_tasks.values())
        msg = {
            "op":    "publish",
            "topic": "/task/system_status",
            "msg":   _make_sys_status(task_list),
        }
        await websocket.send(json.dumps(msg))

    async for raw in websocket:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            continue

        op = msg.get("op")

        # ---- subscribe ------------------------------------------------
        if op == "subscribe":
            topic = msg.get("topic", "")
            subscriptions.add(topic)
            if topic == "/task/system_status":
                await push_sys_status()

        # ---- publish ---------------------------------------------------
        elif op == "publish":
            topic   = msg.get("topic", "")
            payload = msg.get("msg", {})

            received_publishes.append({
                "received_at": datetime.now(timezone.utc).isoformat(),
                "topic":       topic,
                "payload":     payload,
            })

            if topic == "/task_cmd":
                task_type = payload.get("task_type")
                task_id   = payload.get("task_id", 0)

                if task_type == 0:
                    # TASK_MANAGE: 解析 params[0]=action
                    params = payload.get("params", [])
                    action = int(params[0]) if params else -1
                    await _handle_task_manage(action, params)
                else:
                    # 新任务入队，初始状态 READY
                    active_tasks[task_id] = {
                        "task": payload,
                        "status": 0,  # READY
                    }
                    _pending_status_steps[task_id] = list(_STATUS_PROGRESSION)

                # 回复 ack（便于测试断言）
                ack = {
                    "op":     "ack",
                    "topic":  topic,
                    "status": "ok",
                    "task_id": task_id,
                }
                await websocket.send(json.dumps(ack))

                # 推送更新后的状态
                if "/task/system_status" in subscriptions:
                    await push_sys_status()

                # 后台推进任务状态（模拟执行进度）
                if task_type != 0 and task_id in _pending_status_steps:
                    asyncio.ensure_future(
                        _advance_task_status(task_id, websocket, subscriptions)
                    )

            elif topic == "/task/sys_config":
                # 系统模式配置
                ack = {"op": "ack", "topic": topic, "status": "ok"}
                await websocket.send(json.dumps(ack))

            else:
                # 其它话题（如 /vision/keypoints）：若被订阅，回传给客户端
                if topic in subscriptions:
                    await websocket.send(json.dumps({
                        "op": "publish", "topic": topic, "msg": payload
                    }))

        # ---- call_service ----------------------------------------------
        elif op == "call_service":
            await websocket.send(json.dumps({
                "op": "service_response", "result": True, "values": {}
            }))


async def _handle_task_manage(action: int, params: list):
    """处理 TASK_MANAGE 指令"""
    target_id = int(params[1]) if len(params) > 1 else None
    if action == 0 and target_id in active_tasks:    # SUSPEND
        active_tasks[target_id]["status"] = 6        # PAUSE
        _pending_status_steps.pop(target_id, None)
    elif action == 1 and target_id in active_tasks:  # RESUME
        active_tasks[target_id]["status"] = 3        # ONGOING
        _pending_status_steps[target_id] = [5]       # 恢复后推进至 FINISH
    elif action == 2:                                 # SUSPEND_ALL
        _pending_status_steps.clear()
        for t in active_tasks.values():
            t["status"] = 6
    elif action == 3:                                 # RESUME_ALL
        for tid, t in active_tasks.items():
            if t["status"] == 6:
                t["status"] = 3
                _pending_status_steps[tid] = [5]
    elif action == 4 and target_id in active_tasks:  # DELETE
        active_tasks.pop(target_id, None)
        _pending_status_steps.pop(target_id, None)
    elif action == 5:                                 # DELETE_ALL
        active_tasks.clear()
        _pending_status_steps.clear()
    elif action == 7:                                 # CLEAR_BLOCK
        _pending_status_steps.clear()
        for t in active_tasks.values():
            t["status"] = 0  # 重置为 READY


async def _advance_task_status(task_id, websocket, subscriptions):
    """后台协程：按固定时序推进任务执行状态并推送遥测"""
    await asyncio.sleep(0.3)  # 模拟规划延迟
    steps = _pending_status_steps.pop(task_id, [])
    for status in steps:
        if task_id not in active_tasks:
            break
        active_tasks[task_id]["status"] = status
        if "/task/system_status" in subscriptions:
            try:
                task_list = list(active_tasks.values())
                push = {
                    "op":    "publish",
                    "topic": "/task/system_status",
                    "msg":   _make_sys_status(task_list),
                }
                await websocket.send(json.dumps(push))
            except Exception:
                break
        await asyncio.sleep(0.2)


# ============================================================================
# 服务端入口
# ============================================================================

async def _run_server(port: int, stop_event: asyncio.Event):
    import websockets
    async with websockets.serve(handle_client, "127.0.0.1", port):
        await stop_event.wait()


class MockRosbridgeServer:
    """在后台线程运行 Mock rosbridge WebSocket 服务器"""

    def __init__(self, port: int = 9091):
        self.port = port
        self._thread = None
        self._loop = None
        self._stop_event = None

    def start(self):
        received_publishes.clear()
        active_tasks.clear()
        _pending_status_steps.clear()
        ready = threading.Event()

        def _run():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._stop_event = asyncio.Event()

            async def _main():
                ready.set()
                await _run_server(self.port, self._stop_event)

            self._loop.run_until_complete(_main())

        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()
        ready.wait(timeout=3.0)

    def stop(self):
        if self._loop and self._stop_event:
            self._loop.call_soon_threadsafe(self._stop_event.set)

    def get_received_publishes(self):
        return list(received_publishes)

    def get_active_tasks(self):
        return dict(active_tasks)


if __name__ == "__main__":
    import time
    srv = MockRosbridgeServer(port=9091)
    srv.start()
    print("Mock rosbridge running on ws://127.0.0.1:9091")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        srv.stop()
