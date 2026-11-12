"""Unit tests for the Subsea Weak-Communication, Acoustic Codec, ARQ, and Store-and-Forward sync."""

import pytest
from src.weak_comm import (
    AcousticARQSession,
    AcousticChannelSimulator,
    CorruptedFrameError,
    FRAME_SIZE,
    OfflineTransactionQueue,
    crc16_ccitt,
    decode_ack,
    decode_task_intent,
    decode_telemetry_frame,
    encode_ack,
    encode_task_intent,
    encode_telemetry_frame,
)


def test_crc16_calculation():
    # Known test vector for CRC-16-CCITT (poly 0x1021, init 0xFFFF)
    data = b"123456789"
    crc = crc16_ccitt(data)
    assert isinstance(crc, int)
    assert 0 <= crc <= 0xFFFF

    # Changing any byte alters the CRC
    corrupted = b"123456788"
    assert crc16_ccitt(corrupted) != crc


def test_task_intent_compact_encoding_and_decoding():
    intent = {
        "intent_id": "TI-20261112-ACOUSTIC-01",
        "task_type": "pipeline_inspection",
        "equipment_unit_id": "ROV-OBS-001",
        "water_depth": 305.5,
        "target_x": 120.4,
        "target_y": -85.2,
        "target_heading": 180.0,
        "time": {"start": "2026-11-12T12:00:00Z"},
    }

    # Encode to binary frame
    frame = encode_task_intent(intent, seq=42)
    assert len(frame) == FRAME_SIZE  # Exactly 32 bytes

    # Decode and verify fields
    decoded = decode_task_intent(frame)
    assert decoded["seq"] == 42
    assert decoded["task_type"] == "pipeline_inspection"
    assert decoded["water_depth"] == 305.5
    assert decoded["target_x"] == 120.4
    assert decoded["target_y"] == -85.2
    assert decoded["target_heading"] == 180.0
    assert "intent_hash_hex" in decoded


def test_crc16_detects_corrupted_frame():
    intent = {"intent_id": "TI-CORRUPT-TEST", "task_type": "cleaning", "water_depth": 100.0}
    frame = bytearray(encode_task_intent(intent, seq=1))

    # Flip 1 bit in payload body
    frame[15] ^= 0x01

    with pytest.raises(CorruptedFrameError, match="CRC-16 mismatch"):
        decode_task_intent(bytes(frame))


def test_telemetry_frame_encoding_and_decoding():
    frame = encode_telemetry_frame(
        unit_id="ROV-WORK-001",
        depth_m=1250.6,
        x=50.2,
        y=-12.8,
        heading_deg=275.5,
        battery_pct=88.0,
        health="HEALTHY",
        seq=101,
    )
    assert len(frame) == FRAME_SIZE

    decoded = decode_telemetry_frame(frame)
    assert decoded["seq"] == 101
    assert decoded["water_depth_m"] == 1250.6
    assert decoded["pose_x"] == 50.2
    assert decoded["pose_y"] == -12.8
    assert decoded["heading_deg"] == 275.5
    assert decoded["battery_pct"] == 88
    assert decoded["health"] == "HEALTHY"


def test_acoustic_arq_delivery_and_ack():
    transmitted_frames = []

    session = AcousticARQSession(send_fn=lambda f: transmitted_frames.append(f))
    intent = {"intent_id": "TI-ARQ-001", "task_type": "valve_operation", "water_depth": 200.0}
    frame = encode_task_intent(intent, seq=7)

    # 1. Transmit frame
    assert session.transmit_frame(frame, seq=7) is True
    assert session.get_pending_count() == 1
    assert len(transmitted_frames) == 1

    # 2. Receiver decodes and generates ACK
    payload, ack_bytes = session.receive_frame(transmitted_frames[0])
    assert payload is not None
    assert payload["seq"] == 7
    is_ack, ack_seq = decode_ack(ack_bytes)
    assert is_ack is True
    assert ack_seq == 7

    # 3. Transmitter processes ACK
    assert session.handle_ack(ack_bytes) is True
    assert session.get_pending_count() == 0

    # 4. Duplicate frame reception returns payload=None and still emits ACK
    dup_payload, dup_ack = session.receive_frame(transmitted_frames[0])
    assert dup_payload is None
    dup_is_ack, dup_seq = decode_ack(dup_ack)
    assert dup_is_ack is True
    assert dup_seq == 7


def test_store_and_forward_queue_and_delta_reconciliation():
    queue = OfflineTransactionQueue()

    # Enqueue 3 transactions during offline period
    seq1 = queue.enqueue_intent({"intent_id": "TASK-1", "task_type": "survey"})
    seq2 = queue.enqueue_intent({"intent_id": "TASK-2", "task_type": "inspection"})
    seq3 = queue.enqueue_intent({"intent_id": "TASK-3", "task_type": "cleaning"})

    assert queue.pending_count == 3
    assert seq1 == 1 and seq2 == 2 and seq3 == 3

    # Remote node reconnects having seen up to seq 1
    # Delta query returns exactly seq 2 and seq 3
    delta_frames = queue.get_delta_frames_since(highest_remote_seq=1)
    assert len(delta_frames) == 2

    # Verify delta frames decode to TASK-2 and TASK-3
    t2 = decode_task_intent(delta_frames[0])
    t3 = decode_task_intent(delta_frames[1])
    assert t2["seq"] == 2
    assert t3["seq"] == 3

    # Mark acknowledged
    queue.mark_acknowledged(2)
    assert queue.pending_count == 2
    queue.mark_acknowledged(3)
    assert queue.pending_count == 1


def test_channel_simulator_packet_loss():
    sim = AcousticChannelSimulator(packet_loss_rate=0.4, random_seed=123)
    data = b"X" * 32

    received = 0
    for _ in range(100):
        res = sim.transmit(data)
        if res is not None:
            received += 1

    assert sim.packets_transmitted == 100
    assert sim.packets_dropped > 0
    # With 40% loss on 100 packets, received should be around 50-70
    assert 40 <= received <= 80


def test_dev_api_weak_comm_endpoints():
    from web_backend import app

    client = app.test_client()

    # 1. POST /api/dev/weak-comm/encode-task
    resp_encode = client.post("/api/dev/weak-comm/encode-task", json={
        "intent": {
            "intent_id": "TI-WEB-API-TEST",
            "task_type": "pipeline_inspection",
            "water_depth": 250.0,
            "target_x": 10.0,
            "target_y": 20.0,
        },
        "seq": 5,
    })
    assert resp_encode.status_code == 200
    data_enc = resp_encode.get_json()
    assert data_enc["ok"] is True
    assert data_enc["compressed_size_bytes"] == 32
    assert "compression_ratio" in data_enc
    assert data_enc["decoded_verification"]["seq"] == 5

    # 2. POST /api/dev/weak-comm/simulate
    resp_sim = client.post("/api/dev/weak-comm/simulate", json={
        "packet_loss_rate": 0.2,
        "packet_count": 6,
    })
    assert resp_sim.status_code == 200
    data_sim = resp_sim.get_json()
    assert data_sim["ok"] is True
    assert data_sim["simulation_stats"]["packets_transmitted"] == 6
    assert len(data_sim["simulation_stats"]["results"]) == 6
