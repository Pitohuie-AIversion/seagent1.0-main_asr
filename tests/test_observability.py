"""Unit tests for OpenTelemetry distributed tracing and observability."""

import pytest
from src.observability import (
    clear_recorded_spans,
    get_recorded_spans,
    get_tracer,
    is_tracing_available,
    trace_span,
    traced_span,
)


@pytest.fixture(autouse=True)
def reset_spans():
    if not is_tracing_available():
        pytest.skip("OpenTelemetry SDK is not installed or available in this environment")
    clear_recorded_spans()
    yield
    clear_recorded_spans()


def test_tracing_availability():
    assert is_tracing_available() is True
    tracer = get_tracer("test_tracer")
    assert tracer is not None


def test_trace_span_context_manager():
    with trace_span("seagent.test.operation", attributes={"unit_id": "ROV-001", "depth": 150.5}) as span:
        assert span is not None

    spans = get_recorded_spans()
    assert len(spans) == 1
    s = spans[0]
    assert s.name == "seagent.test.operation"
    assert s.attributes["unit_id"] == "ROV-001"
    assert s.attributes["depth"] == 150.5
    assert s.status.is_ok or s.status.status_code.name == "UNSET"


def test_trace_span_exception_recording():
    with pytest.raises(ValueError, match="simulated failure"):
        with trace_span("seagent.test.failure", attributes={"step": "validation"}):
            raise ValueError("simulated failure")

    spans = get_recorded_spans()
    assert len(spans) == 1
    s = spans[0]
    assert s.name == "seagent.test.failure"
    assert s.status.status_code.name == "ERROR"
    assert "simulated failure" in (s.status.description or "")
    assert len(s.events) >= 1  # Exception event recorded
    assert s.events[0].name == "exception"


def test_nested_spans():
    with trace_span("seagent.parent", attributes={"level": "parent"}):
        with trace_span("seagent.child", attributes={"level": "child"}):
            pass

    spans = get_recorded_spans()
    assert len(spans) == 2
    # Child finishes before parent
    child_span = spans[0]
    parent_span = spans[1]
    assert child_span.name == "seagent.child"
    assert parent_span.name == "seagent.parent"
    assert child_span.parent.span_id == parent_span.context.span_id


def test_traced_span_decorator_sync():
    @traced_span(span_name="seagent.decorated_sync", attributes_fn=lambda x: {"arg_val": x})
    def compute_something(x: int) -> int:
        return x * 2

    res = compute_something(42)
    assert res == 84

    spans = get_recorded_spans()
    assert len(spans) == 1
    assert spans[0].name == "seagent.decorated_sync"
    assert spans[0].attributes["arg_val"] == 42


@pytest.mark.asyncio
async def test_traced_span_decorator_async():
    @traced_span(span_name="seagent.decorated_async")
    async def async_fetch():
        return "async_result"

    res = await async_fetch()
    assert res == "async_result"

    spans = get_recorded_spans()
    assert len(spans) == 1
    assert spans[0].name == "seagent.decorated_async"


def test_system_components_emit_spans():
    from src.robot_gateway import RobotGateway, SimulatedRobotDriver
    from src.task_graph import TaskGraph, TaskNode

    # 1. Test RobotGateway span emission
    gw = RobotGateway()
    driver = SimulatedRobotDriver(unit_id="ROV-OTEL-001")
    gw.register_driver(driver, is_default=True)
    gw.dispatch_task({"task_id": "otel_task_1", "task_type": "inspection"})

    spans = get_recorded_spans()
    gw_spans = [s for s in spans if s.name == "seagent.robot_gateway.dispatch"]
    assert len(gw_spans) == 1
    assert gw_spans[0].attributes["target_unit"] == "ROV-OTEL-001"
    assert gw_spans[0].attributes["status"] == "SENT"

    # 2. Test TaskGraph span emission
    clear_recorded_spans()
    graph = TaskGraph()
    graph.add_node(TaskNode(node_id="A", task_type="survey"))
    graph.add_node(TaskNode(node_id="B", task_type="inspection"))
    graph.add_dependency("A", "B")
    batches = graph.get_execution_batches()
    assert len(batches) == 2

    graph_spans = get_recorded_spans()
    sched_spans = [s for s in graph_spans if s.name == "seagent.task_graph.schedule"]
    assert len(sched_spans) == 1
    assert sched_spans[0].attributes["node_count"] == 2
    assert sched_spans[0].attributes["batch_count"] == 2


def test_export_spans_as_dicts_and_dev_endpoints():
    from src.observability import clear_recorded_spans, export_spans_as_dicts, trace_span
    from web_backend import app

    clear_recorded_spans()
    with trace_span("seagent.test.export_op", attributes={"attr1": "val1", "num": 42}):
        pass

    dicts = export_spans_as_dicts(limit=10)
    assert len(dicts) == 1
    assert dicts[0]["name"] == "seagent.test.export_op"
    assert dicts[0]["attributes"]["attr1"] == "val1"
    assert dicts[0]["attributes"]["num"] == 42
    assert dicts[0]["trace_id"] != ""
    assert dicts[0]["span_id"] != ""

    # Test dev API endpoints
    client = app.test_client()
    resp = client.get("/api/dev/observability/spans?limit=10")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["spans_count"] >= 1
    assert data["spans"][0]["name"] == "seagent.test.export_op"

    # Test clear endpoint
    resp_clear = client.post("/api/dev/observability/clear")
    assert resp_clear.status_code == 200
    assert resp_clear.get_json()["ok"] is True

    # Spans should now be empty
    assert len(export_spans_as_dicts()) == 0

