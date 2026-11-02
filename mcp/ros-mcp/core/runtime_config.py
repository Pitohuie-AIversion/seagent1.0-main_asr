"""Validated runtime configuration for ROS subscriptions and dashboard views."""

from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

import yaml


class Ros2RuntimeConfigError(ValueError):
    """Raised when a runtime configuration cannot be applied safely."""


@dataclass(frozen=True)
class GatewayConfig:
    host: str
    port: int
    mode: str


@dataclass(frozen=True)
class ReloadConfig:
    mode: str
    check_interval_seconds: float


@dataclass(frozen=True)
class DashboardConfig:
    refresh_interval_ms: int
    max_raw_message_bytes: int


@dataclass(frozen=True)
class DisplayField:
    path: str
    label: str
    unit: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"path": self.path, "label": self.label, "unit": self.unit}


def _value_at_path(message: Any, path: str) -> Any:
    current = message
    for part in path.split("."):
        if isinstance(current, Mapping):
            if part not in current:
                return None
            current = current[part]
            continue
        if isinstance(current, (list, tuple)) and part.isdigit():
            index = int(part)
            if index >= len(current):
                return None
            current = current[index]
            continue
        if isinstance(current, str) and part.isdigit():
            try:
                decoded = base64.b64decode(current, validate=True)
            except (binascii.Error, ValueError):
                return None
            index = int(part)
            if index >= len(decoded):
                return None
            current = decoded[index]
            continue
        return None
    return current


def _json_safe_value(value: Any) -> Any:
    try:
        json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        return None
    return value


def extract_display_fields(
    message: Mapping[str, Any], fields: Iterable[DisplayField | Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Extract configured dot-paths without executing expressions."""
    result = []
    for item in fields:
        if isinstance(item, DisplayField):
            path, label, unit = item.path, item.label, item.unit
        else:
            path = str(item.get("path", ""))
            label = str(item.get("label", path))
            unit = str(item.get("unit", ""))
        result.append({
            "path": path,
            "label": label,
            "unit": unit,
            "value": _json_safe_value(_value_at_path(message, path)),
        })
    return result


@dataclass(frozen=True)
class SubscriptionConfig:
    id: str
    enabled: bool
    topic: str
    message_type: str
    parser: str
    stale_after_seconds: float
    title: str
    show_raw_message: bool
    fields: tuple[DisplayField, ...]
    max_raw_message_bytes: int

    def build_view(
        self,
        message: Optional[Mapping[str, Any]],
        *,
        received_at: Optional[str],
        message_count: int,
    ) -> dict[str, Any]:
        payload = dict(message or {})
        raw_message: Optional[dict[str, Any]] = None
        raw_truncated = False
        if self.show_raw_message and message is not None:
            try:
                encoded = json.dumps(
                    payload,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            except (TypeError, ValueError):
                # A malformed/non-finite ROS value must not break the whole
                # monitoring API. Keep configured scalar fields visible and
                # omit only the unsafe raw payload.
                raw_truncated = True
            else:
                if len(encoded) <= self.max_raw_message_bytes:
                    raw_message = payload
                else:
                    raw_truncated = True

        fresh = False
        if received_at:
            try:
                received = datetime.fromisoformat(received_at)
                age = (datetime.now(timezone.utc) - received).total_seconds()
                fresh = 0.0 <= age <= self.stale_after_seconds
            except (TypeError, ValueError):
                fresh = False

        return {
            "id": self.id,
            "title": self.title,
            "topic": self.topic,
            "message_type": self.message_type,
            "parser": self.parser,
            "status": "LIVE" if fresh else ("STALE" if received_at else "WAITING"),
            "fresh": fresh,
            "received_at": received_at,
            "message_count": int(message_count),
            "fields": extract_display_fields(payload, self.fields),
            "raw_message": raw_message,
            "raw_truncated": raw_truncated,
        }


@dataclass(frozen=True)
class Ros2RuntimeConfig:
    version: str
    gateway: GatewayConfig
    reload: ReloadConfig
    dashboard: DashboardConfig
    subscriptions: tuple[SubscriptionConfig, ...]

    @property
    def enabled_subscriptions(self) -> tuple[SubscriptionConfig, ...]:
        return tuple(item for item in self.subscriptions if item.enabled)

    @property
    def system_status(self) -> SubscriptionConfig:
        for item in self.enabled_subscriptions:
            if item.parser == "system_status":
                return item
        raise Ros2RuntimeConfigError("缺少启用的 system_status 核心订阅")


_ID_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise Ros2RuntimeConfigError(f"{name} 必须是映射")
    return value


def _load_yaml(path: Path) -> Mapping[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return _mapping(yaml.safe_load(handle) or {}, str(path))
    except (OSError, yaml.YAMLError) as exc:
        raise Ros2RuntimeConfigError(f"无法读取 ROS2 运行配置 {path}: {exc}") from exc


def _parse_fields(raw: Any, subscription_id: str) -> tuple[DisplayField, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise Ros2RuntimeConfigError(f"订阅 {subscription_id} display.fields 必须是列表")
    fields = []
    for index, item in enumerate(raw):
        data = _mapping(item, f"订阅 {subscription_id} display.fields[{index}]")
        path = str(data.get("path", "")).strip()
        label = str(data.get("label", "")).strip()
        if not path or not label:
            raise Ros2RuntimeConfigError(
                f"订阅 {subscription_id} 展示字段必须包含 path 和 label"
            )
        fields.append(DisplayField(path=path, label=label, unit=str(data.get("unit", ""))))
    return tuple(fields)


def load_ros2_runtime_config(
    runtime_path: str | Path, protocol_spec_path: str | Path
) -> Ros2RuntimeConfig:
    """Load and cross-check the mutable runtime policy against the core protocol."""
    runtime_path = Path(runtime_path)
    protocol_spec_path = Path(protocol_spec_path)
    data = _load_yaml(runtime_path)
    protocol = _load_yaml(protocol_spec_path)

    gateway_data = _mapping(data.get("gateway"), "gateway")
    host = str(gateway_data.get("host", "")).strip()
    if not host or len(host) > 253 or any(char.isspace() for char in host):
        raise Ros2RuntimeConfigError("gateway.host 格式非法")
    try:
        port = int(gateway_data.get("port"))
    except (TypeError, ValueError) as exc:
        raise Ros2RuntimeConfigError("gateway.port 必须是整数") from exc
    if not 1 <= port <= 65535:
        raise Ros2RuntimeConfigError("gateway.port 必须在 1..65535 范围内")
    mode = str(gateway_data.get("mode", "real")).strip()
    if mode not in {"real", "mock"}:
        raise Ros2RuntimeConfigError("gateway.mode 必须是 real 或 mock")

    reload_data = _mapping(data.get("reload", {}), "reload")
    reload_mode = str(reload_data.get("mode", "automatic"))
    if reload_mode not in {"automatic", "manual", "restart"}:
        raise Ros2RuntimeConfigError("reload.mode 必须是 automatic、manual 或 restart")
    check_interval = float(reload_data.get("check_interval_seconds", 1.0))
    if not 0.2 <= check_interval <= 60.0:
        raise Ros2RuntimeConfigError("reload.check_interval_seconds 必须在 0.2..60 范围内")

    dashboard_data = _mapping(data.get("dashboard", {}), "dashboard")
    refresh_ms = int(dashboard_data.get("refresh_interval_ms", 1000))
    raw_limit = int(dashboard_data.get("max_raw_message_bytes", 65536))
    if not 250 <= refresh_ms <= 60000:
        raise Ros2RuntimeConfigError("dashboard.refresh_interval_ms 必须在 250..60000 范围内")
    if not 0 <= raw_limit <= 1048576:
        raise Ros2RuntimeConfigError("dashboard.max_raw_message_bytes 必须在 0..1048576 范围内")

    raw_subscriptions = data.get("subscriptions")
    if not isinstance(raw_subscriptions, list) or not raw_subscriptions:
        raise Ros2RuntimeConfigError("subscriptions 必须是非空列表")
    subscriptions = []
    ids = set()
    topic_types: dict[str, str] = {}
    for index, raw_item in enumerate(raw_subscriptions):
        item = _mapping(raw_item, f"subscriptions[{index}]")
        subscription_id = str(item.get("id", "")).strip()
        if not _ID_PATTERN.fullmatch(subscription_id):
            raise Ros2RuntimeConfigError(f"订阅 ID 非法: {subscription_id!r}")
        if subscription_id in ids:
            raise Ros2RuntimeConfigError(f"重复订阅 ID: {subscription_id}")
        ids.add(subscription_id)

        topic = str(item.get("topic", "")).strip()
        message_type = str(item.get("message_type", "")).strip()
        if not topic.startswith("/") or any(char.isspace() for char in topic):
            raise Ros2RuntimeConfigError(f"订阅 {subscription_id} topic 格式非法")
        if not message_type or "/" not in message_type or any(char.isspace() for char in message_type):
            raise Ros2RuntimeConfigError(f"订阅 {subscription_id} message_type 格式非法")
        previous_type = topic_types.setdefault(topic, message_type)
        if previous_type != message_type:
            raise Ros2RuntimeConfigError(f"同一 topic {topic} 配置了不同消息类型")

        parser = str(item.get("parser", "raw"))
        if parser not in {"system_status", "raw"}:
            raise Ros2RuntimeConfigError(f"订阅 {subscription_id} parser 不受支持: {parser}")
        stale_after = float(item.get("stale_after_seconds", 5.0))
        if not 0.2 <= stale_after <= 3600.0:
            raise Ros2RuntimeConfigError(
                f"订阅 {subscription_id} stale_after_seconds 必须在 0.2..3600 范围内"
            )
        display = _mapping(item.get("display", {}), f"订阅 {subscription_id} display")
        subscriptions.append(SubscriptionConfig(
            id=subscription_id,
            enabled=bool(item.get("enabled", True)),
            topic=topic,
            message_type=message_type,
            parser=parser,
            stale_after_seconds=stale_after,
            title=str(display.get("title", subscription_id)).strip() or subscription_id,
            show_raw_message=bool(display.get("show_raw_message", False)),
            fields=_parse_fields(display.get("fields", []), subscription_id),
            max_raw_message_bytes=raw_limit,
        ))

    config = Ros2RuntimeConfig(
        version=str(data.get("version", "1.0")),
        gateway=GatewayConfig(host=host, port=port, mode=mode),
        reload=ReloadConfig(mode=reload_mode, check_interval_seconds=check_interval),
        dashboard=DashboardConfig(
            refresh_interval_ms=refresh_ms, max_raw_message_bytes=raw_limit
        ),
        subscriptions=tuple(subscriptions),
    )
    system_specs = [item for item in config.enabled_subscriptions if item.parser == "system_status"]
    if len(system_specs) != 1:
        raise Ros2RuntimeConfigError("必须且只能启用一个 system_status 核心订阅")

    protocol_gateway = _mapping(protocol.get("websocket_gateway"), "静态协议 websocket_gateway")
    protocol_topics = _mapping(protocol_gateway.get("topics"), "静态协议 topics")
    for subscription in config.subscriptions:
        protocol_item = protocol_topics.get(subscription.id)
        if protocol_item is None:
            continue
        protocol_subscription = _mapping(
            protocol_item, f"静态协议 {subscription.id}"
        )
        expected_topic = str(protocol_subscription.get("name", ""))
        expected_type = str(protocol_subscription.get("type", ""))
        if (
            subscription.topic != expected_topic
            or subscription.message_type != expected_type
        ):
            raise Ros2RuntimeConfigError(
                f"{subscription.id} 订阅与静态协议不一致: "
                f"expected {expected_topic} [{expected_type}]"
            )
    return config
