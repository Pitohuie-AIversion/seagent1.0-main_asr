"""Automatic Repeat reQuest (ARQ) session for unreliable subsea acoustic channels."""
from __future__ import annotations

import logging
import threading
from typing import Any, Callable

from .codec import (
    CorruptedFrameError,
    FRAME_ACK,
    FRAME_NACK,
    decode_ack,
    decode_task_intent,
    decode_telemetry_frame,
    encode_ack,
)

logger = logging.getLogger(__name__)


class AcousticARQSession:
    """Manages reliable frame delivery over intermittent acoustic communication links."""

    def __init__(
        self,
        send_fn: Callable[[bytes], bool] | None = None,
        max_retries: int = 3,
    ):
        self._lock = threading.RLock()
        self._send_fn = send_fn or (lambda data: True)
        self._max_retries = max_retries

        self._pending_frames: dict[int, dict[str, Any]] = {}
        self._received_sequences: set[int] = set()
        self._highest_contiguous_received_seq = 0

    def transmit_frame(self, frame_bytes: bytes, seq: int) -> bool:
        """Transmit a frame and register it for ACK tracking."""
        with self._lock:
            self._pending_frames[seq] = {
                "frame": frame_bytes,
                "retries": 0,
                "acked": False,
            }
            res = self._send_fn(frame_bytes)
            return True if res is None else bool(res)

    def handle_ack(self, ack_bytes: bytes) -> bool:
        """Process an incoming ACK frame and clear pending retransmission."""
        try:
            is_ack, ack_seq = decode_ack(ack_bytes)
        except (ValueError, CorruptedFrameError):
            return False

        with self._lock:
            if is_ack and ack_seq in self._pending_frames:
                self._pending_frames[ack_seq]["acked"] = True
                del self._pending_frames[ack_seq]
                return True
            return False

    def receive_frame(self, frame_bytes: bytes) -> tuple[dict[str, Any] | None, bytes]:
        """Receive an incoming acoustic frame, verify CRC, deduplicate, and return (payload, ack_bytes)."""
        try:
            # Check frame type from second byte
            if len(frame_bytes) < 2:
                raise CorruptedFrameError("Frame too short")
            frame_type = frame_bytes[1]

            if frame_type == 0x01:  # TaskIntent
                payload = decode_task_intent(frame_bytes)
            elif frame_type == 0x02:  # Telemetry
                payload = decode_telemetry_frame(frame_bytes)
            else:
                payload = {"frame_type": frame_type}

            seq = payload.get("seq", 0)

            with self._lock:
                is_duplicate = seq in self._received_sequences
                self._received_sequences.add(seq)

            ack_packet = encode_ack(ack_seq=seq, is_nack=False)
            if is_duplicate:
                return (None, ack_packet)
            return (payload, ack_packet)

        except CorruptedFrameError as exc:
            logger.warning("[ARQ] Frame corrupted: %s", exc)
            nack_packet = encode_ack(ack_seq=0, is_nack=True)
            return (None, nack_packet)

    def get_pending_count(self) -> int:
        with self._lock:
            return len(self._pending_frames)
