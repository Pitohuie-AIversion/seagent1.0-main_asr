"""Compact binary codec for low-bandwidth subsea acoustic weak communication."""
from __future__ import annotations

import hashlib
import struct
import time
from typing import Any

from .crc16 import crc16_ccitt

MAGIC_BYTE = 0x53  # ASCII 'S' for SEAgent
FRAME_SIZE = 32

# Frame Types
FRAME_TASK_INTENT = 0x01
FRAME_TELEMETRY = 0x02
FRAME_ACK = 0x03
FRAME_NACK = 0x04
FRAME_SYNC_REQ = 0x05
FRAME_SYNC_RESP = 0x06

# Task Type Opcode Mapping
TASK_OPCODE_MAP = {
    "unknown": 0,
    "pipeline_inspection": 1,
    "inspection": 1,
    "valve_operation": 2,
    "cleaning": 3,
    "survey": 4,
    "movement": 5,
    "cable_tracking": 6,
    "emergency_stop": 9,
}
OPCODE_TASK_MAP = {v: k for k, v in TASK_OPCODE_MAP.items() if k not in ("inspection", "unknown")}
OPCODE_TASK_MAP[1] = "pipeline_inspection"


class CorruptedFrameError(ValueError):
    """Raised when an acoustic frame fails magic byte or CRC-16 verification."""


class InvalidFrameSizeError(ValueError):
    """Raised when frame length does not match expected acoustic packet size."""


def _hash_unit_id(unit_id: str) -> int:
    """Hash string unit_id into a 16-bit integer."""
    if not unit_id:
        return 0
    return int(hashlib.md5(unit_id.encode("utf-8")).hexdigest()[:4], 16)


def encode_task_intent(intent: dict[str, Any], seq: int = 1) -> bytes:
    """Encode a high-level TaskIntent into a compact 32-byte acoustic binary frame."""
    task_type = str(intent.get("task_type") or "pipeline_inspection").lower()
    opcode = TASK_OPCODE_MAP.get(task_type, 1)

    unit_id = str(intent.get("equipment_unit_id") or intent.get("robot_id") or "")
    unit_hash = _hash_unit_id(unit_id)

    # Depth
    raw_depth = intent.get("water_depth") or intent.get("parameters", {}).get("water_depth") or 0.0
    depth_dm = max(0, min(65535, int(float(raw_depth) * 10)))

    # Coordinates
    x_dm = max(-32768, min(32767, int(float(intent.get("target_x", 0.0)) * 10)))
    y_dm = max(-32768, min(32767, int(float(intent.get("target_y", 0.0)) * 10)))
    heading_deg = max(0, min(3600, int(float(intent.get("target_heading", 0.0)) * 10)))

    # Status / Flags: bits 0..6: 100% battery dummy, bit 7: healthy, bits 8..15: opcode
    status_flags = (100 & 0x7F) | ((opcode & 0xFF) << 8)

    # Timestamp: epoch seconds
    epoch_ts = int(time.time())
    time_info = intent.get("time") or {}
    if time_info.get("start"):
        try:
            from datetime import datetime
            parsed = datetime.fromisoformat(str(time_info["start"]).replace("Z", "+00:00"))
            epoch_ts = int(parsed.timestamp())
        except Exception:
            pass

    # 10 bytes payload: intent_id hash
    intent_id = str(intent.get("intent_id") or "TI_DEFAULT")
    intent_hash = hashlib.sha256(intent_id.encode("utf-8")).digest()[:10]

    # Pack body without CRC
    body = struct.pack(
        ">I H H h h H H 10s",
        epoch_ts,
        unit_hash,
        depth_dm,
        x_dm,
        y_dm,
        heading_deg,
        status_flags,
        intent_hash,
    )
    crc = crc16_ccitt(body)

    # Full frame: Magic(1B) + Type(1B) + Seq(2B) + CRC(2B) + Body(26B) = 32B
    header = struct.pack(">B B H H", MAGIC_BYTE, FRAME_TASK_INTENT, seq & 0xFFFF, crc)
    return header + body


def decode_task_intent(data: bytes) -> dict[str, Any]:
    """Decode and verify a 32-byte acoustic binary frame back into structured TaskIntent."""
    if len(data) != FRAME_SIZE:
        raise InvalidFrameSizeError(f"Frame length {len(data)} != {FRAME_SIZE}")

    magic, frame_type, seq, expected_crc = struct.unpack(">B B H H", data[:6])
    if magic != MAGIC_BYTE:
        raise CorruptedFrameError(f"Invalid magic byte 0x{magic:X} (expected 0x{MAGIC_BYTE:X})")

    body = data[6:]
    actual_crc = crc16_ccitt(body)
    if actual_crc != expected_crc:
        raise CorruptedFrameError(f"CRC-16 mismatch: expected 0x{expected_crc:04X}, computed 0x{actual_crc:04X}")

    epoch_ts, unit_hash, depth_dm, x_dm, y_dm, heading_deg, status_flags, intent_hash = struct.unpack(
        ">I H H h h H H 10s", body
    )

    opcode = (status_flags >> 8) & 0xFF
    task_type = OPCODE_TASK_MAP.get(opcode, "pipeline_inspection")

    return {
        "frame_type": frame_type,
        "seq": seq,
        "timestamp_epoch": epoch_ts,
        "unit_hash": f"0x{unit_hash:04X}",
        "task_type": task_type,
        "water_depth": round(depth_dm / 10.0, 1),
        "target_x": round(x_dm / 10.0, 1),
        "target_y": round(y_dm / 10.0, 1),
        "target_heading": round(heading_deg / 10.0, 1),
        "intent_hash_hex": intent_hash.hex(),
    }


def encode_telemetry_frame(
    unit_id: str,
    depth_m: float,
    x: float,
    y: float,
    heading_deg: float,
    battery_pct: float,
    health: str = "HEALTHY",
    seq: int = 1,
) -> bytes:
    """Encode robot telemetry state into a 32-byte binary acoustic packet."""
    unit_hash = _hash_unit_id(unit_id)
    depth_dm = max(0, min(65535, int(abs(depth_m) * 10)))
    x_dm = max(-32768, min(32767, int(x * 10)))
    y_dm = max(-32768, min(32767, int(y * 10)))
    head_quant = max(0, min(3600, int(heading_deg * 10)))

    # Flags: battery (0..6), health (bit 7: 0=healthy, 1=fault)
    bat_val = max(0, min(100, int(battery_pct)))
    health_bit = 0 if health == "HEALTHY" else 1
    status_flags = (bat_val & 0x7F) | ((health_bit & 0x01) << 7)

    epoch_ts = int(time.time())
    payload = b"\x00" * 10

    body = struct.pack(
        ">I H H h h H H 10s",
        epoch_ts,
        unit_hash,
        depth_dm,
        x_dm,
        y_dm,
        head_quant,
        status_flags,
        payload,
    )
    crc = crc16_ccitt(body)
    header = struct.pack(">B B H H", MAGIC_BYTE, FRAME_TELEMETRY, seq & 0xFFFF, crc)
    return header + body


def decode_telemetry_frame(data: bytes) -> dict[str, Any]:
    """Decode a 32-byte acoustic telemetry frame."""
    if len(data) != FRAME_SIZE:
        raise InvalidFrameSizeError(f"Frame length {len(data)} != {FRAME_SIZE}")

    magic, frame_type, seq, expected_crc = struct.unpack(">B B H H", data[:6])
    if magic != MAGIC_BYTE:
        raise CorruptedFrameError(f"Invalid magic byte 0x{magic:X}")

    body = data[6:]
    actual_crc = crc16_ccitt(body)
    if actual_crc != expected_crc:
        raise CorruptedFrameError(f"CRC-16 mismatch: 0x{actual_crc:04X} != 0x{expected_crc:04X}")

    epoch_ts, unit_hash, depth_dm, x_dm, y_dm, head_quant, status_flags, _ = struct.unpack(
        ">I H H h h H H 10s", body
    )

    battery_pct = status_flags & 0x7F
    is_fault = bool((status_flags >> 7) & 0x01)

    return {
        "frame_type": frame_type,
        "seq": seq,
        "timestamp_epoch": epoch_ts,
        "unit_hash": f"0x{unit_hash:04X}",
        "water_depth_m": round(depth_dm / 10.0, 1),
        "pose_x": round(x_dm / 10.0, 1),
        "pose_y": round(y_dm / 10.0, 1),
        "heading_deg": round(head_quant / 10.0, 1),
        "battery_pct": battery_pct,
        "health": "FAULT" if is_fault else "HEALTHY",
    }


def encode_ack(ack_seq: int, is_nack: bool = False) -> bytes:
    """Encode an 8-byte compact ACK or NACK packet."""
    frame_type = FRAME_NACK if is_nack else FRAME_ACK
    body = struct.pack(">H", ack_seq & 0xFFFF)
    crc = crc16_ccitt(body)
    # Magic(1B) + Type(1B) + Seq(2B) + CRC(2B) + AckSeq(2B) = 8B
    return struct.pack(">B B H H H", MAGIC_BYTE, frame_type, 0, crc, ack_seq & 0xFFFF)


def decode_ack(data: bytes) -> tuple[bool, int]:
    """Decode ACK/NACK packet and return (is_ack, ack_seq)."""
    if len(data) < 8:
        raise InvalidFrameSizeError(f"ACK frame size {len(data)} < 8")
    magic, frame_type, _, expected_crc, ack_seq = struct.unpack(">B B H H H", data[:8])
    if magic != MAGIC_BYTE:
        raise CorruptedFrameError(f"Invalid magic byte 0x{magic:X}")
    body = struct.pack(">H", ack_seq)
    if crc16_ccitt(body) != expected_crc:
        raise CorruptedFrameError("ACK CRC mismatch")
    return (frame_type == FRAME_ACK, ack_seq)
