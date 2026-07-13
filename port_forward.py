"""
port_forward.py - 将 AutoDL 公网端口 6006 转发到 Flask 本地端口 8890
使用 Python asyncio 实现，零依赖。
"""
import asyncio
import sys

LOCAL_PORT = 6006
if len(sys.argv) > 1:
    try:
        LOCAL_PORT = int(sys.argv[1])
    except ValueError:
        pass
TARGET_HOST = "127.0.0.1"
TARGET_PORT = 8890


async def pipe(reader, writer):
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def handle_client(client_reader, client_writer):
    try:
        target_reader, target_writer = await asyncio.open_connection(TARGET_HOST, TARGET_PORT)
    except Exception as e:
        print(f"[port_forward] Cannot connect to {TARGET_HOST}:{TARGET_PORT}: {e}")
        client_writer.close()
        return

    await asyncio.gather(
        pipe(client_reader, target_writer),
        pipe(target_reader, client_writer),
    )


async def main():
    server = await asyncio.start_server(handle_client, "0.0.0.0", LOCAL_PORT)
    print(f"✅ Port forward: 0.0.0.0:{LOCAL_PORT} → {TARGET_HOST}:{TARGET_PORT}")
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[port_forward] Stopped.")
        sys.exit(0)
