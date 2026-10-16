"""Bounded, process-local replay of recent HTTP turns, under the session lock."""
from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import json
import time


class RequestIdConflict(ValueError):
    """One request ID cannot identify two different normalized messages."""


class RequestReplayUnavailable(RuntimeError):
    """The turn completed but its response was too large to retain safely."""


@dataclass(frozen=True)
class _CompletedTurn:
    message_digest: str
    completed_at: float
    response_json: str | None
    size: int


class ChatRequestCache:
    """Replay up to 128 turns for 10 minutes, within a 4 MiB response budget.

    Callers hold the manager's session lock across lookup, processing and put.
    This is a retry window for a live manager, not durable exactly-once delivery.
    Payloads are serialized so later slot edits cannot mutate an earlier reply.
    """

    def __init__(self, *, max_entries=128, ttl_seconds=600.0, max_bytes=4 * 1024 * 1024, clock=time.monotonic):
        self.max_entries = max_entries
        self.ttl_seconds = ttl_seconds
        self.max_bytes = max_bytes
        self.clock = clock
        self._entries = OrderedDict()
        self._bytes = 0

    @staticmethod
    def _digest(value):
        return hashlib.sha256(value.encode('utf-8')).hexdigest()

    def _remove_oldest(self):
        _, entry = self._entries.popitem(last=False)
        self._bytes -= entry.size

    def _expire(self):
        now = self.clock()
        while self._entries and now - next(iter(self._entries.values())).completed_at >= self.ttl_seconds:
            self._remove_oldest()

    def get(self, request_id, message):
        self._expire()
        entry = self._entries.get(self._digest(request_id))
        if entry is None:
            return None
        if entry.message_digest != self._digest(message):
            raise RequestIdConflict(request_id)
        if entry.response_json is None:
            raise RequestReplayUnavailable(request_id)
        return json.loads(entry.response_json)

    def put(self, request_id, message, outcome):
        encoded = json.dumps(outcome, ensure_ascii=True)
        size = len(encoded.encode('utf-8'))
        # Keep a tombstone for an oversized response: do not rerun the mutation.
        if size > self.max_bytes:
            encoded, size = None, 0
        self._expire()
        key = self._digest(request_id)
        previous = self._entries.pop(key, None)
        if previous is not None:
            self._bytes -= previous.size
        while self._entries and (len(self._entries) >= self.max_entries or self._bytes + size > self.max_bytes):
            self._remove_oldest()
        self._entries[key] = _CompletedTurn(self._digest(message), self.clock(), encoded, size)
        self._bytes += size
