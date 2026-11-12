"""Subsea Weak-Communication and Intermittent Acoustic Synchronization Package."""

from .arq import AcousticARQSession
from .channel_simulator import AcousticChannelSimulator
from .codec import (
    CorruptedFrameError,
    FRAME_ACK,
    FRAME_NACK,
    FRAME_SIZE,
    FRAME_TASK_INTENT,
    FRAME_TELEMETRY,
    InvalidFrameSizeError,
    decode_ack,
    decode_task_intent,
    decode_telemetry_frame,
    encode_ack,
    encode_task_intent,
    encode_telemetry_frame,
)
from .crc16 import crc16_ccitt
from .store_and_forward import OfflineTransactionQueue

__all__ = [
    "AcousticARQSession",
    "AcousticChannelSimulator",
    "CorruptedFrameError",
    "FRAME_ACK",
    "FRAME_NACK",
    "FRAME_SIZE",
    "FRAME_TASK_INTENT",
    "FRAME_TELEMETRY",
    "InvalidFrameSizeError",
    "OfflineTransactionQueue",
    "crc16_ccitt",
    "decode_ack",
    "decode_task_intent",
    "decode_telemetry_frame",
    "encode_ack",
    "encode_task_intent",
    "encode_telemetry_frame",
]
