"""OpenTelemetry Distributed Tracing and Observability for SEAgent."""
from __future__ import annotations

import functools
import inspect
import logging
import os
from contextlib import contextmanager
from typing import Any, Callable, Generator

logger = logging.getLogger(__name__)

# Try importing OpenTelemetry components
_OTEL_AVAILABLE = False
_in_memory_exporter = None
_tracer_provider = None

try:
    from opentelemetry import trace as otel_trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    try:
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    except ImportError:
        from opentelemetry.sdk.trace.export import InMemorySpanExporter

    from opentelemetry.trace import Status, StatusCode

    _in_memory_exporter = InMemorySpanExporter()
    _tracer_provider = TracerProvider()
    _tracer_provider.add_span_processor(SimpleSpanProcessor(_in_memory_exporter))

    # Optional remote OTLP exporter if configured in environment
    otlp_endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    if otlp_endpoint:
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

            _tracer_provider.add_span_processor(SimpleSpanProcessor(OTLPSpanExporter(endpoint=otlp_endpoint)))
            logger.info("[Observability] Configured OTLP Span Exporter to %s", otlp_endpoint)
        except Exception as exc:
            logger.warning("[Observability] Could not initialize OTLP exporter: %s", exc)

    otel_trace.set_tracer_provider(_tracer_provider)
    _OTEL_AVAILABLE = True
except Exception as exc:
    logger.debug("[Observability] OpenTelemetry SDK running in fallback mode: %s", exc)
    _OTEL_AVAILABLE = False


class _NoOpSpan:
    """Fallback span when OpenTelemetry is unavailable or inactive."""

    def set_attribute(self, key: str, value: Any) -> None:
        pass

    def set_attributes(self, attributes: dict[str, Any]) -> None:
        pass

    def record_exception(self, exception: BaseException) -> None:
        pass

    def set_status(self, status: Any, description: str | None = None) -> None:
        pass


def is_tracing_available() -> bool:
    """Return True if OpenTelemetry SDK is active and configured."""
    return _OTEL_AVAILABLE


def get_tracer(name: str = "seagent"):
    """Get an OpenTelemetry tracer instance or dummy tracer."""
    if _OTEL_AVAILABLE:
        return otel_trace.get_tracer(name)
    return None


def get_recorded_spans():
    """Retrieve all recorded spans from the in-memory exporter (for tests & debugging)."""
    if _in_memory_exporter is not None:
        return _in_memory_exporter.get_finished_spans()
    return []


def clear_recorded_spans() -> None:
    """Clear all finished spans from the in-memory exporter."""
    if _in_memory_exporter is not None:
        _in_memory_exporter.clear()


def export_spans_as_dicts(limit: int = 50) -> list[dict[str, Any]]:
    """Export recorded in-memory spans as JSON-serializable dictionaries."""
    spans = get_recorded_spans()
    result = []
    for s in spans[-limit:]:
        trace_id = f"{s.context.trace_id:032x}" if getattr(s, "context", None) else ""
        span_id = f"{s.context.span_id:016x}" if getattr(s, "context", None) else ""
        parent_id = None
        if getattr(s, "parent", None) and hasattr(s.parent, "span_id"):
            parent_id = f"{s.parent.span_id:016x}"

        duration_ms = None
        if getattr(s, "start_time", None) and getattr(s, "end_time", None):
            duration_ms = round((s.end_time - s.start_time) / 1e6, 2)

        status_str = "OK"
        if getattr(s, "status", None):
            if hasattr(s.status, "status_code"):
                status_str = str(s.status.status_code.name)
            else:
                status_str = str(s.status)

        result.append({
            "name": getattr(s, "name", "unknown"),
            "trace_id": trace_id,
            "span_id": span_id,
            "parent_id": parent_id,
            "start_time_ns": getattr(s, "start_time", None),
            "end_time_ns": getattr(s, "end_time", None),
            "duration_ms": duration_ms,
            "status": status_str,
            "attributes": dict(getattr(s, "attributes", None) or {}),
        })
    return result



@contextmanager
def trace_span(
    name: str,
    attributes: dict[str, Any] | None = None,
    tracer_name: str = "seagent",
) -> Generator[Any, None, None]:
    """Context manager to trace a code block with OpenTelemetry span semantics."""
    if not _OTEL_AVAILABLE:
        yield _NoOpSpan()
        return

    tracer = otel_trace.get_tracer(tracer_name)
    with tracer.start_as_current_span(name) as span:
        if attributes:
            for k, v in attributes.items():
                if v is not None:
                    span.set_attribute(k, v if isinstance(v, (bool, str, int, float)) else str(v))
        try:
            yield span
        except Exception as exc:
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            raise


def traced_span(
    span_name: str | None = None,
    tracer_name: str = "seagent",
    attributes_fn: Callable[..., dict[str, Any]] | None = None,
):
    """Decorator to automatically wrap a function execution in an OpenTelemetry span."""

    def decorator(fn: Callable):
        name = span_name or f"{fn.__module__}.{fn.__qualname__}"

        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def async_wrapper(*args, **kwargs):
                attrs = None
                if attributes_fn:
                    try:
                        attrs = attributes_fn(*args, **kwargs)
                    except Exception:
                        pass
                with trace_span(name, attributes=attrs, tracer_name=tracer_name) as span:
                    result = await fn(*args, **kwargs)
                    return result

            return async_wrapper
        else:

            @functools.wraps(fn)
            def sync_wrapper(*args, **kwargs):
                attrs = None
                if attributes_fn:
                    try:
                        attrs = attributes_fn(*args, **kwargs)
                    except Exception:
                        pass
                with trace_span(name, attributes=attrs, tracer_name=tracer_name) as span:
                    result = fn(*args, **kwargs)
                    return result

            return sync_wrapper

    return decorator
