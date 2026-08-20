"""
Mock rosbridge WebSocket 服务器
模拟真实 rosbridge_suite 的行为（rosbridge_protocol v2.0）：
- 接受 WebSocket 连接（默认端口 9091，避免与真实 9090 冲突）
- 处理 publish（写入 /task_cmd）
- 处理 subscribe + 按需推送 /task/system_status 遥测
- 使用 asyncio + websockets 实现，可在后台线程运行
"""
import asyncio
import json
import threading
from datetime import datetime, timezone


MOCK_TELEMETRY = {
    "WROV-250-001": {
        "unit_id": "WROV-250-001",
        "online": True,
        "battery_percentage": 94.5,
        "current_depth": 312.4,
        "pose": {"x": 115.3421, "y": 20.8912, "z": -312.4, "yaw": 1.57},
    },
    "LROV-150-001": {
        "unit_id": "LROV-150-001",
        "online": True,
        "battery_percentage": 88.0,
        "current_depth": 85.0,
        "pose": {"x": 109.1234, "y": 18.5432, "z": -85.0, "yaw": 0.78},
    },
}

received_publishes = []  # 记录所有 publish 消息


async def handle_client(websocket):
    subscriptions = set()
    async for raw in websocket:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            continue

        op = msg.get("op")

        if op == "subscribe":
            topic = msg.get("topic", "")
            subscriptions.add(topic)
            # 立即推送一次数据
            if topic == "/task/system_status":
                push = {
                    "op": "publish",
                    "topic": "/task/system_status",
                    "msg": {
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "robots": MOCK_TELEMETRY,
                    },
                }
                await websocket.send(json.dumps(push))

        elif op == "publish":
            topic = msg.get("topic", "")
            payload = msg.get("msg", {})
            received_publishes.append({
                "received_at": datetime.now(timezone.utc).isoformat(),
                "topic": topic,
                "payload": payload,
            })
            # 回复确认（rosbridge 通常不回复 publish，但我们加一条便于测试）
            ack = {"op": "ack", "topic": topic, "status": "ok"}
            await websocket.send(json.dumps(ack))

        elif op == "call_service":
            # 简单回复
            await websocket.send(json.dumps({"op": "service_response", "result": True, "values": {}}))


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
