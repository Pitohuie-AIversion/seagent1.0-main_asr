"""Small standards-compliant MCP server used by transport integration tests."""

from mcp.server.fastmcp import FastMCP


mcp = FastMCP("fake-ros-mcp-server")


@mcp.tool()
def connect_to_robot(
    ip: str = "127.0.0.1",
    port: int = 9090,
    ping_timeout: float = 2.0,
    port_timeout: float = 2.0,
) -> dict:
    return {
        "message": f"WebSocket IP set to {ip}:{port}",
        "connectivity_test": {"port_check": {"open": True}},
    }


@mcp.tool()
def publish_once(topic: str, msg_type: str, msg: dict) -> dict:
    return {
        "success": True,
        "topic": topic,
        "msg_type": msg_type,
        "msg": msg,
    }


@mcp.tool()
def subscribe_once(
    topic: str,
    msg_type: str,
    expects_image: str = "auto",
    timeout: float = 2.0,
    queue_length: int = 1,
    throttle_rate_ms: int = 0,
) -> dict:
    return {
        "msg": {
            "pose": {"pose": {"position": {"x": 1.0, "y": 2.0, "z": -3.0}}},
            "twist": {"linear": {"x": 0.1, "y": 0.2, "z": 0.3}},
            "alt": 4.0,
            "ctr_mode": 7,
            "health": 0,
            "task_list": [],
        }
    }


if __name__ == "__main__":
    mcp.run(transport="stdio")

