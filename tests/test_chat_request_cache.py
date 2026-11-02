"""Retry retention is explicit, bounded and does not alias mutable task data."""
import pytest

from src.web.chat_request_cache import ChatRequestCache, RequestIdConflict, RequestReplayUnavailable


def test_replay_is_detached_and_conflicting_message_is_rejected():
    cache = ChatRequestCache()
    value = {'payload': {'collected': {'payload': ['camera']}}}
    cache.put('request', 'message', value)
    value['payload']['collected']['payload'].append('sonar')
    replay = cache.get('request', 'message')
    assert replay['payload']['collected']['payload'] == ['camera']
    replay['payload']['collected']['payload'].clear()
    assert cache.get('request', 'message')['payload']['collected']['payload'] == ['camera']
    with pytest.raises(RequestIdConflict):
        cache.get('request', 'different')


def test_expiration_is_measured_from_completion_without_extending_on_read():
    now = [10.0]
    cache = ChatRequestCache(ttl_seconds=10, clock=lambda: now[0])
    cache.put('request', 'message', {'code': 200})
    now[0] = 19.0
    assert cache.get('request', 'message') == {'code': 200}
    now[0] = 20.0
    assert cache.get('request', 'message') is None


def test_count_and_byte_budgets_evict_oldest_completed_turns():
    cache = ChatRequestCache(max_entries=2, max_bytes=100)
    for key in ('one', 'two', 'three'):
        cache.put(key, 'message', {'code': 200})
    assert cache.get('one', 'message') is None
    assert cache.get('two', 'message') == {'code': 200}
    cache.put('four', 'message', {'reply': 'x' * 80})
    assert cache.get('two', 'message') is None
    assert cache.get('three', 'message') is None
    assert cache.get('four', 'message')['reply'] == 'x' * 80


def test_oversized_response_retains_a_non_reexecuting_tombstone():
    cache = ChatRequestCache(max_bytes=20)
    cache.put('large', 'message', {'reply': 'x' * 100})
    with pytest.raises(RequestReplayUnavailable):
        cache.get('large', 'message')
    with pytest.raises(RequestIdConflict):
        cache.get('large', 'different')


def test_response_unicode_cannot_prevent_completed_turn_recording():
    cache = ChatRequestCache()
    cache.put('request', 'valid message', {'reply': '\ud800'})
    assert cache.get('request', 'valid message') == {'reply': '\ud800'}
