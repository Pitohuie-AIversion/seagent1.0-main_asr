"""MCP-protocol client for the upstream ``ros-mcp-server``.

This module deliberately keeps the two transports separate:

* MCP (stdio or Streamable HTTP) is used between SEAgent and ros-mcp-server.
* rosbridge WebSocket is owned by ros-mcp-server and is never opened here.

The public methods mirror the subset of :class:`RosbridgeClient` consumed by
``SEAgentMCPBridgeService`` so the task validation and web API contracts remain
unchanged while the production transport becomes real MCP tool calls.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from contextlib import AbstractAsyncContextManager
from typing import Any, Callable, Dict, Optional, Sequence

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from .rosbridge_client import (
    CONFIG_MESSAGE_TYPE,
    CONFIG_TOPIC,
    LEGACY_MSGMANAGEMENT,
    LEGACY_PLAN_THRESHOLD,
    STATUS_MESSAGE_TYPE,
    STATUS_TOPIC,
    TASK_MESSAGE_TYPE,
    TASK_TOPIC,
    LocalOrigin,
    PilotMode,
    ProtocolValidationError,
    SysTaskCmd,
    TaskManageAction,
    TaskType,
    _legacy_task_payload,
    build_task_manage_cmd,
    generate_task_id,
    intent_to_syscmd,
    validate_sys_task_cmd,
)


logger = logging.getLogger(__name__)


class RosMCPProtocolError(RuntimeError):
    """Raised when the MCP server returns an invalid or failed tool result."""


def _exception_details(exc: BaseException) -> str:
    """Render nested async exception groups without hiding the root failure."""
    nested = getattr(exc, "exceptions", None)
    if nested:
        details = [_exception_details(item) for item in nested]
        return " | ".join(dict.fromkeys(detail for detail in details if detail))
    message = str(exc).strip()
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


def _decode_tool_result(result: Any) -> Dict[str, Any]:
    """Normalize MCP SDK and FastMCP result variants into one dictionary."""
    if getattr(result, "isError", False):
        messages = [
            str(getattr(item, "text", ""))
            for item in getattr(result, "content", [])
            if getattr(item, "text", None)
        ]
        raise RosMCPProtocolError("; ".join(messages) or "MCP tool call failed")

    structured = getattr(result, "structuredContent", None)
    if structured is None:
        structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict) and structured:
        # FastMCP may wrap a structured return value in ``result``.
        if set(structured) == {"result"} and isinstance(structured["result"], dict):
            return dict(structured["result"])
        return dict(structured)

    for item in getattr(result, "content", []):
        text = getattr(item, "text", None)
        if not text:
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload

    raise RosMCPProtocolError("MCP tool returned no JSON object")


class RosMCPClient:
    """Long-lived synchronous facade over an asynchronous MCP client session."""

    REQUIRED_TOOLS = frozenset({"connect_to_robot", "publish_once", "subscribe_once"})

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 9090,
        connect_timeout: float = 5.0,
        *,
        server_command: str = "ros-mcp",
        server_args: Optional[Sequence[str]] = None,
        server_url: Optional[str] = None,
        tool_timeout: float = 15.0,
        startup_timeout: float = 30.0,
        telemetry_poll_interval: float = 1.0,
        telemetry_timeout: float = 2.0,
    ):
        self.host = host
        self.port = int(port)
        self.connect_timeout = float(connect_timeout)
        self.server_command = server_command
        self.server_args = list(server_args or ["--transport=stdio"])
        self.server_url = server_url
        self.tool_timeout = float(tool_timeout)
        self.startup_timeout = float(startup_timeout)
        self.telemetry_poll_interval = float(telemetry_poll_interval)
        self.telemetry_timeout = float(telemetry_timeout)

        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._session: Optional[ClientSession] = None
        self._call_lock: Optional[asyncio.Lock] = None
        self._shutdown_event: Optional[asyncio.Event] = None
        self._ready = threading.Event()
        self._lifecycle_lock = threading.RLock()
        self._callback_lock = threading.Lock()
        self._telemetry_callbacks: list[Callable[[dict], None]] = []
        self._startup_error: Optional[BaseException] = None
        self._last_transport_error: Optional[str] = None
        self._tool_names: frozenset[str] = frozenset()

    @property
    def last_transport_error(self) -> Optional[str]:
        return self._last_transport_error

    @property
    def tool_names(self) -> frozenset[str]:
        return self._tool_names

    def _transport_context(self) -> AbstractAsyncContextManager:
        if self.server_url:
            return streamable_http_client(self.server_url)
        params = StdioServerParameters(
            command=self.server_command,
            args=self.server_args,
            env=None,
        )
        return stdio_client(params)

    def connect(self) -> None:
        """Start the MCP session and select the target rosbridge gateway."""
        with self._lifecycle_lock:
            if self.is_connected():
                return
            self._ready.clear()
            self._startup_error = None
            self._last_transport_error = None
            self._thread = threading.Thread(
                target=self._thread_main,
                daemon=True,
                name="seagent-ros-mcp-client",
            )
            self._thread.start()

        if not self._ready.wait(self.startup_timeout):
            self.disconnect()
            raise TimeoutError(
                f"ROS MCP Server 初始化超时 ({self.startup_timeout:.1f}s)"
            )
        if self._startup_error is not None:
            error = self._startup_error
            details = _exception_details(error)
            self.disconnect()
            raise ConnectionError(f"ROS MCP Server 初始化失败: {details}") from error
        if not self.is_connected():
            raise ConnectionError("ROS MCP Server 会话未就绪")

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._session_main())
        except BaseException as exc:
            self._startup_error = exc
            self._last_transport_error = _exception_details(exc)
            logger.error(
                "ROS MCP 会话异常: %s",
                self._last_transport_error,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            self._ready.set()
        finally:
            self._session = None
            self._call_lock = None
            self._shutdown_event = None
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            finally:
                loop.close()
                self._loop = None

    async def _session_main(self) -> None:
        async with self._transport_context() as streams:
            read, write = streams[0], streams[1]
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools_result = await session.list_tools()
                tool_names = frozenset(tool.name for tool in tools_result.tools)
                missing = self.REQUIRED_TOOLS - tool_names
                if missing:
                    raise RosMCPProtocolError(
                        "ROS MCP Server 缺少必需工具: " + ", ".join(sorted(missing))
                    )

                self._session = session
                self._call_lock = asyncio.Lock()
                self._shutdown_event = asyncio.Event()
                self._tool_names = tool_names

                connection = await self._call_tool_async(
                    "connect_to_robot",
                    {
                        "ip": self.host,
                        "port": self.port,
                        "ping_timeout": self.connect_timeout,
                        "port_timeout": self.connect_timeout,
                    },
                )
                port_state = (
                    connection.get("connectivity_test", {})
                    .get("port_check", {})
                    .get("open")
                )
                if port_state is not True:
                    raise ConnectionError(
                        "ROS MCP Server 未确认 rosbridge 端口可用: "
                        f"{self.host}:{self.port}"
                    )

                self._ready.set()
                poll_task = asyncio.create_task(self._telemetry_loop())
                try:
                    await self._shutdown_event.wait()
                finally:
                    poll_task.cancel()
                    await asyncio.gather(poll_task, return_exceptions=True)

    async def _call_tool_async(
        self, name: str, arguments: Dict[str, Any]
    ) -> Dict[str, Any]:
        if self._session is None or self._call_lock is None:
            raise ConnectionError("ROS MCP Client 会话尚未初始化")
        async with self._call_lock:
            result = await self._session.call_tool(name, arguments)
        payload = _decode_tool_result(result)
        if payload.get("error"):
            raise RosMCPProtocolError(f"{name}: {payload['error']}")
        return payload

    def call_tool(
        self,
        name: str,
        arguments: Optional[Dict[str, Any]] = None,
        *,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Call one discovered ROS MCP tool from synchronous SEAgent code."""
        loop = self._loop
        if loop is None or not self.is_connected():
            raise ConnectionError("ROS MCP Client 未连接")
        future = asyncio.run_coroutine_threadsafe(
            self._call_tool_async(name, arguments or {}), loop
        )
        try:
            return future.result(timeout=timeout or self.tool_timeout)
        except Exception as exc:
            self._last_transport_error = str(exc)
            raise

    async def _telemetry_loop(self) -> None:
        assert self._shutdown_event is not None
        while not self._shutdown_event.is_set():
            with self._callback_lock:
                callbacks = list(self._telemetry_callbacks)
            if callbacks:
                try:
                    result = await self._call_tool_async(
                        "subscribe_once",
                        {
                            "topic": STATUS_TOPIC,
                            "msg_type": STATUS_MESSAGE_TYPE,
                            "expects_image": "false",
                            "timeout": self.telemetry_timeout,
                            "queue_length": 1,
                            "throttle_rate_ms": 0,
                        },
                    )
                    message = result.get("msg")
                    if isinstance(message, dict):
                        for callback in callbacks:
                            try:
                                callback(message)
                            except Exception:
                                logger.exception("ROS MCP 遥测回调失败")
                    self._last_transport_error = None
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._last_transport_error = str(exc)
                    logger.warning("ROS MCP 遥测轮询失败: %s", exc)
            try:
                await asyncio.wait_for(
                    self._shutdown_event.wait(), timeout=self.telemetry_poll_interval
                )
            except asyncio.TimeoutError:
                pass

    def disconnect(self) -> None:
        with self._lifecycle_lock:
            loop = self._loop
            shutdown_event = self._shutdown_event
            thread = self._thread
            if loop is not None and shutdown_event is not None and loop.is_running():
                loop.call_soon_threadsafe(shutdown_event.set)
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=max(2.0, self.connect_timeout))
            self._thread = None
            self._session = None
            self._ready.clear()

    def is_connected(self) -> bool:
        thread = self._thread
        return bool(
            self._ready.is_set()
            and self._startup_error is None
            and self._session is not None
            and thread is not None
            and thread.is_alive()
        )

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *_: Any) -> None:
        self.disconnect()

    def publish(self, topic: str, msg_type: str, payload: dict) -> None:
        result = self.call_tool(
            "publish_once",
            {"topic": topic, "msg_type": msg_type, "msg": payload},
        )
        if result.get("success") is not True:
            raise RosMCPProtocolError(
                f"publish_once 未确认发布成功: {result}"
            )

    def publish_task_cmd(
        self,
        task_intent: Dict[str, Any],
        task_id: Optional[int] = None,
        use_geodetic: bool = False,
        origin: Optional[LocalOrigin] = None,
    ) -> int:
        cmd = intent_to_syscmd(
            task_intent,
            task_id=task_id,
            use_geodetic=use_geodetic,
            origin=origin,
        )
        if LEGACY_MSGMANAGEMENT:
            payload = _legacy_task_payload(cmd, task_intent)
        else:
            payload = cmd.to_dict()
        self.publish(TASK_TOPIC, TASK_MESSAGE_TYPE, payload)
        return cmd.task_id

    def publish_syscmd_raw(self, cmd: SysTaskCmd) -> None:
        validate_sys_task_cmd(cmd)
        payload = _legacy_task_payload(cmd) if LEGACY_MSGMANAGEMENT else cmd.to_dict()
        self.publish(TASK_TOPIC, TASK_MESSAGE_TYPE, payload)

    def task_manage(
        self,
        action: TaskManageAction,
        target_task_id: Optional[int] = None,
    ) -> int:
        normalized_action = TaskManageAction(int(action))
        if LEGACY_MSGMANAGEMENT:
            if normalized_action != TaskManageAction.DELETE_ALL:
                raise ProtocolValidationError(
                    f"msgmanagement 兼容模式不支持 {normalized_action.name}"
                )
            if target_task_id is not None:
                raise ProtocolValidationError("DELETE_ALL 不使用 target_task_id")
            command_id = generate_task_id()
            self.publish(
                TASK_TOPIC,
                TASK_MESSAGE_TYPE,
                {
                    "task": 255,
                    "hole_id": 0,
                    "x": 0.0,
                    "y": 0.0,
                    "z": 0.0,
                    "roll": 0.0,
                    "pitch": 0.0,
                    "yaw": 0.0,
                },
            )
            return command_id
        command = build_task_manage_cmd(normalized_action, target_task_id)
        self.publish_syscmd_raw(command)
        return command.task_id

    def suspend_task(self, target_task_id: int) -> int:
        return self.task_manage(TaskManageAction.SUSPEND, target_task_id)

    def resume_task(self, target_task_id: int) -> int:
        return self.task_manage(TaskManageAction.RESUME, target_task_id)

    def delete_task(self, target_task_id: int) -> int:
        return self.task_manage(TaskManageAction.DELETE, target_task_id)

    def delete_all(self) -> int:
        return self.task_manage(TaskManageAction.DELETE_ALL)

    def clear_block(self) -> int:
        return self.task_manage(TaskManageAction.CLEAR_BLOCK)

    def ctrl_task(
        self,
        device_id: int,
        value: float,
        priority: int = 15,
        fail_stop: bool = False,
    ) -> int:
        command = SysTaskCmd(
            task_type=int(TaskType.CTRL_TASK),
            task_id=generate_task_id(),
            frame_id="",
            priority=priority,
            pos_target=[],
            params=[float(device_id), float(value)],
            fail_stop=fail_stop,
        )
        self.publish_syscmd_raw(command)
        return command.task_id

    def set_pilot_mode(self, mode: PilotMode) -> None:
        if LEGACY_MSGMANAGEMENT:
            payload = {
                "task_type": 3,
                "task_src": 1,
                "plan_threshold": LEGACY_PLAN_THRESHOLD,
                "ctr_mode": int(mode),
            }
        else:
            payload = {"ctr_mode": int(mode)}
        self.publish(CONFIG_TOPIC, CONFIG_MESSAGE_TYPE, payload)

    def subscribe_system_status(self, callback: Callable[[dict], None]) -> None:
        with self._callback_lock:
            if callback not in self._telemetry_callbacks:
                self._telemetry_callbacks.append(callback)

    def subscribe_from_config(
        self,
        callbacks: Optional[Dict[str, Callable[[dict], None]]] = None,
    ) -> list[str]:
        # The production bridge currently consumes only system status. Extra
        # catalog subscriptions can be added as explicit MCP polling policies.
        return []
