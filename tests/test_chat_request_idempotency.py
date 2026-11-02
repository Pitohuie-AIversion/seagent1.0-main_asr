"""A lost HTTP response must not apply an additive edit a second time."""
import concurrent.futures
import json
import threading
from datetime import datetime, timedelta
from unittest.mock import Mock

import pytest

import web_backend
import src.web.routes_chat as routes
import src.web.state as state
from src.dialogue_manager import DialogueManager


ENDPOINTS = ['/api/chat', '/api/chat/stream']


def send(endpoint, sid, request_id, message='结束时间延长一小时'):
    request = {'session_id': sid, 'message': message}
    if request_id is not None:
        request['request_id'] = request_id
    with web_backend.app.test_client() as client:
        response = client.post(endpoint, json=request)
        if response.mimetype != 'text/event-stream':
            return response.status_code, response.get_json()
        for event in response.get_data(as_text=True).split('\n\n'):
            if event.startswith(('event: result\n', 'event: error\n')):
                body = json.loads(event.split('data: ', 1)[1])
                return body['code'], body
        raise AssertionError('Stream has no terminal result/error')


@pytest.fixture
def task(monkeypatch):
    sid = 'request-idempotency-task'
    manager = DialogueManager(session_id=sid)
    manager.task_state['end_time'] = '2026-10-17T12:00:00'
    manager._last_built_json = manager.task_state
    calls = []

    def extend(message, request_id=None, event_sink=None):
        calls.append((message, request_id))
        end = datetime.fromisoformat(manager.task_state['end_time']) + timedelta(hours=1)
        manager.task_state['end_time'] = end.isoformat()
        manager._last_built_json = manager.task_state
        return '结束时间已延长一小时'

    monkeypatch.setattr(manager, 'process', extend)
    monkeypatch.setattr(routes, 'print_status', lambda _: None)
    monkeypatch.setitem(state._sessions_manager, sid, manager)
    monkeypatch.setitem(web_backend.app.config, 'TESTING', True)
    monkeypatch.setitem(web_backend.app.config, 'SEAGENT_API_TOKENS', [])
    return manager, calls


@pytest.mark.parametrize('first', ENDPOINTS)
@pytest.mark.parametrize('retry', ENDPOINTS)
def test_same_id_replays_result_across_transports_without_second_mutation(task, first, retry):
    manager, calls = task
    original = send(first, manager.session_id, 'one-logical-edit')
    repeated = send(retry, manager.session_id, 'one-logical-edit')
    assert original[0] == repeated[0] == 200
    assert repeated[1] == original[1]
    assert manager.task_state['end_time'] == '2026-10-17T13:00:00'
    assert len(calls) == 1


@pytest.mark.parametrize('endpoint', ENDPOINTS)
def test_same_id_with_different_message_is_conflict(task, endpoint):
    manager, calls = task
    send(endpoint, manager.session_id, 'same-id')
    status, payload = send(endpoint, manager.session_id, 'same-id', '结束时间缩短一小时')
    assert status == 409
    assert payload['error'] == 'RequestIdConflict'
    assert payload['retryable'] is False
    assert manager.task_state['end_time'] == '2026-10-17T13:00:00'
    assert len(calls) == 1


@pytest.mark.parametrize('endpoint', ENDPOINTS)
def test_concurrent_duplicate_waits_for_one_execution(task, monkeypatch, endpoint):
    manager, calls = task
    original_process = manager.process
    entered, release = threading.Event(), threading.Event()

    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(5), 'test did not release processing'
        return original_process(*args, **kwargs)

    monkeypatch.setattr(manager, 'process', blocked)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(send, endpoint, manager.session_id, 'concurrent-id')
        assert entered.wait(5)
        second = pool.submit(send, endpoint, manager.session_id, 'concurrent-id')
        release.set()
        left, right = first.result(timeout=10), second.result(timeout=10)
    assert left == right
    assert len(calls) == 1
    assert manager.task_state['end_time'] == '2026-10-17T13:00:00'


@pytest.mark.parametrize('endpoint', ENDPOINTS)
def test_failure_after_mutation_is_replayed_without_reprocessing(task, monkeypatch, endpoint):
    manager, calls = task
    process = manager.process

    def fail(*args, **kwargs):
        process(*args, **kwargs)
        raise TypeError('failure after mutation')

    monkeypatch.setattr(manager, 'process', fail)
    first = send(endpoint, manager.session_id, 'failed-edit')
    repeated = send(endpoint, manager.session_id, 'failed-edit')
    assert first == repeated
    assert first[0] == 500 and first[1]['error'] == 'InternalServerError'
    assert len(calls) == 1
    assert manager.task_state['end_time'] == '2026-10-17T13:00:00'


def test_replay_keeps_original_snapshot_without_reverting_newer_turn(task):
    manager, calls = task
    first = send('/api/chat', manager.session_id, 'old')
    second = send('/api/chat', manager.session_id, 'new')
    replay = send('/api/chat', manager.session_id, 'old')
    assert first == replay
    assert first[1]['collected']['end_time'] == '2026-10-17T13:00:00'
    assert second[1]['collected']['end_time'] == manager.task_state['end_time'] == '2026-10-17T14:00:00'
    assert len(calls) == 2


def test_missing_request_id_creates_distinct_turns(task):
    manager, calls = task
    first = send('/api/chat', manager.session_id, None)
    second = send('/api/chat', manager.session_id, None)
    assert first[1]['request_id'] != second[1]['request_id']
    assert len(calls) == 2


def test_same_request_id_is_scoped_to_manager_lifetime(task, monkeypatch):
    manager, calls = task
    send('/api/chat', manager.session_id, 'scope')
    other = DialogueManager(session_id='other-request-idempotency-task')
    process = Mock(return_value='另一个会话')
    monkeypatch.setattr(other, 'process', process)
    monkeypatch.setitem(state._sessions_manager, other.session_id, other)
    assert send('/api/chat', other.session_id, 'scope')[0] == 200
    process.assert_called_once()
    with web_backend.app.test_client() as client:
        assert client.post('/api/reset', json={'session_id': manager.session_id}).status_code == 200
    replacement = DialogueManager(session_id=manager.session_id)
    process_after_reset = Mock(return_value='重新开始')
    monkeypatch.setattr(replacement, 'process', process_after_reset)
    monkeypatch.setitem(state._sessions_manager, replacement.session_id, replacement)
    assert send('/api/chat', replacement.session_id, 'scope')[0] == 200
    process_after_reset.assert_called_once()


def test_busy_stream_is_rejected_before_processing_and_can_retry_same_id(task, monkeypatch):
    import src.web.chat_stream as streaming
    manager, calls = task
    with monkeypatch.context() as patcher:
        patcher.setattr(streaming, '_STREAM_WORKERS', threading.BoundedSemaphore(0))
        status, payload = send('/api/chat/stream', manager.session_id, 'busy-request')
    assert status == 503 and payload['error'] == 'ServerBusy'
    assert payload['retryable'] is True
    assert calls == []
    assert send('/api/chat/stream', manager.session_id, 'busy-request')[0] == 200
    assert len(calls) == 1
