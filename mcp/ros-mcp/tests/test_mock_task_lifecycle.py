"""Real WebSocket pause/resume/delete tests against isolated mock ports."""
import socket
import time
import pytest
from mcp.shim.bridge_service import SEAgentMCPBridgeService
from mcp.shim.mock_rosbridge_server import MockRosbridgeServer


def wait_for(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate(): return
        time.sleep(.015)
    assert predicate(), 'Mock lifecycle transition timed out'


@pytest.fixture
def live(tmp_path):
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    server = MockRosbridgeServer(port=port);server.start()
    time.sleep(.1)
    bridge = SEAgentMCPBridgeService(port=port, dispatch_records_dir=tmp_path)
    bridge.start()
    try: yield bridge,server
    finally: bridge.stop();server.stop()


def submit(bridge, identity):
    return bridge.dispatch_intent({'schema_version':2,'intent_id':identity,
        'task_type':'underwater_move','location':{'water_depth_m':50},
        'task':{'details':{'target':{'latitude':20,'longitude':110}}}})


def test_pause_stays_paused_then_resume_completes(live):
    bridge,server=live;tid=submit(bridge,'pause-resume')
    wait_for(lambda: server.get_active_tasks().get(tid,{}).get('status')==3)
    bridge.suspend_task(tid)
    wait_for(lambda: server.get_active_tasks().get(tid,{}).get('status')==6)
    time.sleep(.7)
    assert server.get_active_tasks()[tid]['status']==6
    bridge.resume_task(tid)
    wait_for(lambda: server.get_active_tasks().get(tid,{}).get('status')==5)


def test_delete_cancels_paused_task_progression(live):
    bridge,server=live;tid=submit(bridge,'pause-delete')
    wait_for(lambda: server.get_active_tasks().get(tid,{}).get('status')==3)
    bridge.suspend_task(tid)
    wait_for(lambda: server.get_active_tasks().get(tid,{}).get('status')==6)
    bridge.delete_task(tid)
    wait_for(lambda: tid not in server.get_active_tasks())
    bridge.resume_task(tid)
    time.sleep(.7)
    assert tid not in server.get_active_tasks()
    wait_for(lambda: bridge.get_task_status(tid) is None)
    assert bridge.runtime_snapshot()['active_tasks_count']==0


def test_bulk_pause_resume_and_delete_cancel_every_runner(live):
    bridge,server=live
    tids=[submit(bridge,'bulk-1'),submit(bridge,'bulk-2')]
    wait_for(lambda: all(server.get_active_tasks().get(tid,{}).get('status')==3 for tid in tids))
    bridge.client.suspend_all()
    wait_for(lambda: all(server.get_active_tasks().get(tid,{}).get('status')==6 for tid in tids))
    time.sleep(.7)
    assert all(server.get_active_tasks()[tid]['status']==6 for tid in tids)
    bridge.client.resume_all()
    wait_for(lambda: all(server.get_active_tasks().get(tid,{}).get('status')==5 for tid in tids))
    bridge.client.delete_all()
    wait_for(lambda: not server.get_active_tasks())
    time.sleep(.35)
    assert not server.get_active_tasks()


def test_pause_during_planning_delay_resumes_remaining_steps(live):
    bridge,server=live;tid=submit(bridge,'early-pause')
    wait_for(lambda: tid in server.get_active_tasks())
    bridge.suspend_task(tid)
    wait_for(lambda: server.get_active_tasks().get(tid,{}).get('status')==6)
    time.sleep(.5)
    assert server.get_active_tasks()[tid]['status']==6
    bridge.resume_task(tid)
    bridge.resume_task(tid)  # Repeated controls must not fork advancement.
    wait_for(lambda: server.get_active_tasks().get(tid,{}).get('status')==5)
    bridge.suspend_task(tid)
    bridge.resume_task(tid)
    time.sleep(.1)
    assert server.get_active_tasks()[tid]['status']==5
