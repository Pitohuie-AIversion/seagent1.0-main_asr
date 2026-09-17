"""Dispatch evidence must never resurrect an absent or deleted robot task."""
from unittest.mock import Mock
import pytest
from mcp.shim.bridge_service import SEAgentMCPBridgeService


def intent():
    return {'schema_version': 2, 'intent_id': 'LIFECYCLE-001', 'task_type': 'pipeline_inspection',
        'location': {'water_depth_m': 50}, 'task': {'details': {
        'start_point': {'latitude': 20, 'longitude': 110},
        'end_point': {'latitude': 20.1, 'longitude': 110.1}}}}


def bridge_with_task(tmp_path):
    bridge = SEAgentMCPBridgeService(dispatch_records_dir=tmp_path)
    bridge._running = True
    bridge.client.is_connected = Mock(return_value=True)
    bridge.client.publish_task_cmd = Mock()
    bridge.client.delete_task = Mock(return_value=0x800ff)
    return bridge, bridge.dispatch_intent(intent())


def observe(bridge, task_id=None, status=3):
    bridge.tracker._on_sys_status({'task_list': [] if task_id is None else [
        {'task': {'task_id': task_id, 'task_type': 2}, 'status': status}]})


def test_absent_task_is_unknown_not_active_sent(tmp_path):
    bridge, tid = bridge_with_task(tmp_path)
    observe(bridge, tid)
    observe(bridge)
    snap = bridge.runtime_snapshot()
    assert snap['active_tasks_count'] == 0
    assert snap['task_history'][0]['status'] == 'UNKNOWN'
    assert snap['task_history'][0]['dispatch_state'] == 'SENT'


def test_delete_absence_preserves_request_and_restart_idempotency(tmp_path):
    bridge, tid = bridge_with_task(tmp_path)
    observe(bridge, tid)
    assert bridge.delete_task(tid) == 0x800ff
    # A successful send cannot invent a receiver-side deleted status.
    snapshot = bridge.runtime_snapshot()
    assert snapshot['active_tasks_count'] == 0
    current = snapshot['task_history'][0]
    assert current['status'] == 'DELETE_REQUESTED'
    assert current['last_observed_status'] == 'ONGOING'
    assert current['control_state'] == 'DELETE_REQUESTED'
    observe(bridge)
    snap = bridge.runtime_snapshot()
    assert snap['active_tasks_count'] == 0
    assert snap['task_history'][0]['status'] == 'DELETE_REQUESTED'
    assert snap['task_history'][0]['observed_in_latest'] is False
    assert not snap['task_history'][0].get('deletion_confirmed', False)
    restarted = SEAgentMCPBridgeService(dispatch_records_dir=tmp_path)
    restarted._running = True
    restarted.client.is_connected = Mock(return_value=True)
    restarted.client.publish_task_cmd = Mock()
    assert restarted.dispatch_intent(intent()) == tid
    restarted.client.publish_task_cmd.assert_not_called()
    assert restarted.runtime_snapshot()['active_tasks_count'] == 0
    assert restarted.runtime_snapshot()['task_history'][0]['status'] == 'DELETE_REQUESTED'


def test_terminal_telemetry_not_counted_as_active(tmp_path):
    bridge, tid = bridge_with_task(tmp_path)
    observe(bridge, tid, status=5)
    snap = bridge.runtime_snapshot()
    assert snap['active_tasks_count'] == 0
    assert snap['task_history'][0]['status'] == 'FINISH'


def test_historical_sent_record_without_telemetry_is_unknown(tmp_path):
    bridge, tid = bridge_with_task(tmp_path)
    restarted = SEAgentMCPBridgeService(dispatch_records_dir=tmp_path)
    snap = restarted.runtime_snapshot()
    assert snap['active_tasks_count'] == 0
    assert snap['task_history'][0]['status'] == 'UNKNOWN'


def test_failed_delete_does_not_claim_request_success(tmp_path):
    bridge, tid = bridge_with_task(tmp_path)
    bridge.client.delete_task.side_effect = RuntimeError('transport failed')
    with pytest.raises(RuntimeError): bridge.delete_task(tid)
    observe(bridge, tid)
    assert not bridge.runtime_snapshot()['active_tasks'][0].get('control_state')


def test_new_observation_after_delete_reports_receiver_state(tmp_path):
    from datetime import datetime, timezone
    bridge, tid = bridge_with_task(tmp_path)
    observe(bridge, tid)
    bridge.delete_task(tid)
    observe(bridge, tid)
    # Use a slightly earlier request time, without changing any system clock.
    bridge._control_requests[tid]['delete_requested_at'] = '2000-01-01T00:00:00+00:00'
    bridge.tracker.latest_telemetry().received_at = datetime.now(timezone.utc).isoformat()
    snap=bridge.runtime_snapshot()
    assert snap['active_tasks_count']==1
    assert snap['active_tasks'][0]['status']=='ONGOING'
    assert snap['active_tasks'][0]['control_state']=='DELETE_REQUESTED'


def test_stale_telemetry_is_unknown_not_current_activity(tmp_path):
    bridge,tid=bridge_with_task(tmp_path)
    observe(bridge,tid)
    bridge.tracker.latest_telemetry().received_at='2000-01-01T00:00:00+00:00'
    snap=bridge.runtime_snapshot()
    assert snap['active_tasks_count']==0
    assert snap['task_history'][0]['status']=='UNKNOWN'
    assert snap['task_history'][0]['last_observed_status']=='ONGOING'


def test_unknown_receiver_status_not_counted_as_active(tmp_path):
    bridge,tid=bridge_with_task(tmp_path)
    observe(bridge,tid,status=99)
    snap=bridge.runtime_snapshot()
    assert snap['active_tasks_count']==0
    assert snap['task_history'][0]['status']=='UNKNOWN(99)'
