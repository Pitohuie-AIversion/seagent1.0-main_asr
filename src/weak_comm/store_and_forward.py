"""Store-and-forward transaction buffer and delta reconciliation for intermittent acoustic comms."""
from __future__ import annotations

import logging
import threading
from typing import Any

from .codec import encode_task_intent

logger = logging.getLogger(__name__)


class OfflineTransactionQueue:
    """Thread-safe append-only buffer for state updates and tasks during acoustic link blackout."""

    def __init__(self, max_buffer_size: int = 1000):
        self._lock = threading.RLock()
        self._max_size = max_buffer_size
        self._next_seq = 1
        # Maps seq -> {"seq": int, "intent": dict, "frame": bytes, "acked": bool}
        self._transactions: dict[int, dict[str, Any]] = {}
        self._highest_acked_seq = 0

    @property
    def pending_count(self) -> int:
        with self._lock:
            return sum(1 for tx in self._transactions.values() if not tx["acked"])

    def enqueue_intent(self, intent: dict[str, Any]) -> int:
        """Enqueue a task intent, encode into acoustic frame, and return assigned sequence number."""
        with self._lock:
            seq = self._next_seq
            self._next_seq += 1

            frame = encode_task_intent(intent, seq=seq)
            self._transactions[seq] = {
                "seq": seq,
                "intent": intent,
                "frame": frame,
                "acked": False,
            }

            # Enforce max buffer size by purging acknowledged items first
            if len(self._transactions) > self._max_size:
                acked_keys = [k for k, v in self._transactions.items() if v["acked"]]
                for k in acked_keys:
                    self._transactions.pop(k, None)
                    if len(self._transactions) <= self._max_size:
                        break

            return seq

    def mark_acknowledged(self, seq: int) -> bool:
        """Mark a transaction as safely received and acknowledged by the subsea node."""
        with self._lock:
            tx = self._transactions.get(seq)
            if tx:
                tx["acked"] = True
                if seq > self._highest_acked_seq:
                    self._highest_acked_seq = seq
                return True
            return False

    def get_delta_frames_since(self, highest_remote_seq: int) -> list[bytes]:
        """Fetch all unacknowledged binary frames that the remote node has not yet seen."""
        with self._lock:
            frames = []
            for seq in sorted(self._transactions.keys()):
                if seq > highest_remote_seq and not self._transactions[seq]["acked"]:
                    frames.append(self._transactions[seq]["frame"])
            return frames

    def get_transaction(self, seq: int) -> dict[str, Any] | None:
        with self._lock:
            return self._transactions.get(seq)

    def clear(self) -> None:
        with self._lock:
            self._transactions.clear()
            self._next_seq = 1
            self._highest_acked_seq = 0
