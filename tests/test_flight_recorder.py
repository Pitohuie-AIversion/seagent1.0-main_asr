"""Unit tests for Subsea Mission Flight Recorder (Blackbox) & Time-Travel Replay Engine."""

import json
import os
import tempfile
import pytest
from pathlib import Path

from src.orchestration import (
    FlightRecordEvent,
    FlightRecorder,
    MissionDecomposer,
    MissionOrchestrator,
    MissionReplayer,
    MissionState,
)
from src.orchestration.flight_recorder import _REPO_ROOT, _is_within_repo
from src.robot_gateway import RobotGateway, SimulatedRobotDriver
from src.task_graph import TaskNodeStatus


@pytest.fixture(autouse=True)
def clean_flight_recorder(tmp_path):
    FlightRecorder.reset_instance()
    rec = FlightRecorder.get_instance(storage_dir=tmp_path / "flight_records")
    yield rec
    FlightRecorder.reset_instance()


def test_flight_recorder_event_logging_and_crc_checksum(tmp_path):
    recorder = FlightRecorder.get_instance(storage_dir=tmp_path / "flight_records")
    mission_id = "TI_TEST_BLACKBOX_01"

    evt1 = recorder.record_event(
        mission_id=mission_id,
        event_type="MISSION_CREATED",
        payload={"task_name": "Valve Operation"},
    )
    assert evt1.seq == 1
    assert len(evt1.checksum) == 8
    assert evt1.compute_checksum() == evt1.checksum

    evt2 = recorder.record_event(
        mission_id=mission_id,
        event_type="SUBTASK_DISPATCHED",
        payload={"node_id": "step_1"},
        node_id="step_1",
        robot_id="SIM-ROV-001",
    )
    assert evt2.seq == 2

    # Verify integrity
    is_valid, msg, count = recorder.verify_integrity(mission_id)
    assert is_valid is True
    assert count == 2
    assert "intact" in msg

    # Verify export archive
    archive = recorder.export_blackbox_archive(mission_id)
    assert archive["mission_id"] == mission_id
    assert archive["event_count"] == 2
    assert archive["integrity_valid"] is True


def test_flight_recorder_detects_tampering_or_corruption(tmp_path):
    recorder = FlightRecorder.get_instance(storage_dir=tmp_path / "flight_records")
    mission_id = "TI_TEST_TAMPER_01"

    recorder.record_event(mission_id=mission_id, event_type="STEP_1", payload={"val": 100})
    recorder.record_event(mission_id=mission_id, event_type="STEP_2", payload={"val": 200})

    log_path = recorder._get_log_path(mission_id)
    assert log_path.is_file()

    # Read events, tamper with payload of step 2, and rewrite
    with open(log_path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    evt_dict = json.loads(lines[1])
    evt_dict["payload"]["val"] = 999  # Tampered without updating CRC-32!
    lines[1] = json.dumps(evt_dict) + "\n"

    with open(log_path, "w", encoding="utf-8") as f:
        f.writelines(lines)

    # Force re-read from disk by clearing memory cache
    recorder._memory_cache.clear()

    is_valid, msg, count = recorder.verify_integrity(mission_id)
    assert is_valid is False
    assert "checksum mismatch" in msg


def test_mission_time_travel_replay(tmp_path):
    recorder = FlightRecorder.get_instance(storage_dir=tmp_path / "flight_records")
    gw = RobotGateway.get_instance()
    gw.register_driver(SimulatedRobotDriver(unit_id="SIM-ROV-001"), is_default=True)

    orchestrator = MissionOrchestrator(gateway=gw, flight_recorder=recorder)
    intent = {
        "intent_id": "TI_REPLAY_TEST",
        "task_type": "valve_operation",
        "water_depth": 300.0,
        "assigned_robot_id": "SIM-ROV-001",
    }
    graph = orchestrator.create_mission(intent)
    mission_id = graph.graph_id

    # Run step 1: approach
    orchestrator.step_mission(graph)
    # Run step 2: clean
    orchestrator.step_mission(graph)
    # Run step 3: force fail turn_valve to trigger dynamic replanning
    valve_node_id = "TI_REPLAY_TEST_turn_valve"
    orchestrator.step_mission(
        graph,
        force_fail_node_id=valve_node_id,
        failure_reason="Valve stuck with marine growth",
    )

    events = recorder.get_mission_events(mission_id)
    assert len(events) >= 5

    # Replay to seq 1: initial creation
    snap_1 = MissionReplayer.replay_to_seq(mission_id=mission_id, target_seq=1, recorder=recorder)
    assert snap_1.replayed_up_to_seq == 1
    assert len(snap_1.completed_nodes) == 0

    # Replay to event where turn_valve failed
    fail_events = [e for e in events if e.event_type == "SUBTASK_FAILED"]
    assert len(fail_events) >= 1
    fail_seq = fail_events[0].seq

    snap_fail = MissionReplayer.replay_to_seq(mission_id=mission_id, target_seq=fail_seq, recorder=recorder)
    assert valve_node_id in snap_fail.failed_nodes

    # Replay to the replanning event
    replan_events = [e for e in events if e.event_type == "REPLANNING_APPLIED"]
    assert len(replan_events) >= 1
    replan_seq = replan_events[0].seq

    snap_replan = MissionReplayer.replay_to_seq(mission_id=mission_id, target_seq=replan_seq, recorder=recorder)
    assist_node_id = f"{valve_node_id}_assist"
    assert any(n["node_id"] == assist_node_id for n in snap_replan.graph_state["nodes"])


def test_dev_api_flight_recorder_endpoints(tmp_path):
    from web_backend import app
    client = app.test_client()

    recorder = FlightRecorder.get_instance()
    mission_id = "TI_API_RECORDER_TEST"
    recorder.record_event(mission_id=mission_id, event_type="MISSION_CREATED", payload={"title": "Test Mission"})
    recorder.record_event(mission_id=mission_id, event_type="SUBTASK_COMPLETED", payload={"telemetry": {"depth": 120.0}}, node_id="n1", robot_id="ROV-1")

    # 1. GET /api/dev/mission/recorder/events
    resp_events = client.get(f"/api/dev/mission/recorder/events?mission_id={mission_id}")
    assert resp_events.status_code == 200
    data_events = resp_events.get_json()
    assert data_events["ok"] is True
    assert data_events["events_count"] == 2
    assert data_events["integrity"]["is_valid"] is True

    # 2. POST /api/dev/mission/recorder/replay
    replay_payload = {
        "mission_id": mission_id,
        "target_seq": 2,
    }
    resp_replay = client.post("/api/dev/mission/recorder/replay", json=replay_payload)
    assert resp_replay.status_code == 200
    data_replay = resp_replay.get_json()
    assert data_replay["ok"] is True
    assert data_replay["snapshot"]["replayed_up_to_seq"] == 2
    assert "n1" in data_replay["snapshot"]["completed_nodes"]

    # 3. GET /api/dev/mission/recorder/archive
    resp_archive = client.get(f"/api/dev/mission/recorder/archive?mission_id={mission_id}")
    assert resp_archive.status_code == 200
    data_archive = resp_archive.get_json()
    assert data_archive["ok"] is True
    assert data_archive["archive"]["event_count"] == 2
    assert data_archive["archive"]["integrity_valid"] is True


def test_default_storage_dir_outside_repo_and_writes_outside_repo(monkeypatch, tmp_path):
    monkeypatch.delenv("SEAGENT_FLIGHT_RECORD_DIR", raising=False)
    monkeypatch.delenv("SEAGENT_RESULT_DIR", raising=False)
    fake_home = tmp_path / "fake_home"
    monkeypatch.setenv("HOME", str(fake_home))

    FlightRecorder.reset_instance()
    rec = FlightRecorder.get_instance()

    expected_dir = (fake_home / ".seagent" / "flight_records").resolve()
    assert rec.storage_dir == expected_dir
    assert not _is_within_repo(rec.storage_dir)
    assert not rec.storage_dir.resolve().is_relative_to(_REPO_ROOT)

    mission_id = "TI_OUTSIDE_REPO_01"
    rec.record_event(
        mission_id=mission_id,
        event_type="MISSION_CREATED",
        payload={"ok": True},
    )

    log_path = rec._get_log_path(mission_id)
    assert log_path.is_file()
    assert not log_path.resolve().is_relative_to(_REPO_ROOT)
    assert not (_REPO_ROOT / "data" / "flight_records" / f"{mission_id}.flight.jsonl").exists()


def test_default_storage_dir_env_precedence(monkeypatch, tmp_path):
    res_dir = tmp_path / "res"
    flight_dir = tmp_path / "custom_flight"
    fake_home = tmp_path / "fake_home"
    monkeypatch.setenv("HOME", str(fake_home))

    monkeypatch.delenv("SEAGENT_FLIGHT_RECORD_DIR", raising=False)
    monkeypatch.setenv("SEAGENT_RESULT_DIR", str(res_dir))
    rec_res = FlightRecorder(storage_dir=None)
    assert rec_res.storage_dir == (res_dir / "flight_records").resolve()

    monkeypatch.setenv("SEAGENT_FLIGHT_RECORD_DIR", str(flight_dir))
    rec_env = FlightRecorder(storage_dir=None)
    assert rec_env.storage_dir == flight_dir.resolve()

    monkeypatch.setenv("SEAGENT_FLIGHT_RECORD_DIR", "   ")
    rec_ws_flight = FlightRecorder(storage_dir=None)
    assert rec_ws_flight.storage_dir == (res_dir / "flight_records").resolve()

    monkeypatch.setenv("SEAGENT_RESULT_DIR", "   ")
    rec_ws_both = FlightRecorder(storage_dir=None)
    assert rec_ws_both.storage_dir == (fake_home / ".seagent" / "flight_records").resolve()

    explicit_dir = tmp_path / "explicit"
    rec_explicit = FlightRecorder(storage_dir=explicit_dir)
    assert rec_explicit.storage_dir == explicit_dir

    rec_blank_arg = FlightRecorder(storage_dir="   ")
    assert rec_blank_arg.storage_dir == (fake_home / ".seagent" / "flight_records").resolve()


def test_default_storage_dir_fallbacks_to_tempdir(monkeypatch, tmp_path):
    monkeypatch.delenv("SEAGENT_FLIGHT_RECORD_DIR", raising=False)
    monkeypatch.delenv("SEAGENT_RESULT_DIR", raising=False)
    sys_tmp = tmp_path / "sys_tmp"
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(sys_tmp))
    expected_temp = (sys_tmp / "seagent_flight_records").resolve()

    # Case A: PermissionError on mkdir for home candidate
    fake_home_a = tmp_path / "home_perm_err"
    monkeypatch.setenv("HOME", str(fake_home_a))
    original_mkdir = Path.mkdir

    def selective_mkdir(self, *args, **kwargs):
        if str(self).startswith(str(fake_home_a.resolve())):
            raise PermissionError("Access denied")
        return original_mkdir(self, *args, **kwargs)

    with monkeypatch.context() as mp:
        mp.setattr(Path, "mkdir", selective_mkdir)
        rec_a = FlightRecorder(storage_dir=None)
        assert rec_a.storage_dir == expected_temp
        rec_a.record_event(mission_id="TI_FALLBACK_A", event_type="MISSION_CREATED", payload={"case": "A"})
        assert rec_a._get_log_path("TI_FALLBACK_A").is_file()
        assert rec_a._get_log_path("TI_FALLBACK_A").parent == expected_temp

    # Case B: os.access returns False for existing home candidate
    fake_home_b = tmp_path / "home_no_access"
    home_target_b = (fake_home_b / ".seagent" / "flight_records").resolve()
    home_target_b.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(fake_home_b))
    original_access = os.access

    def selective_access(path, mode, *args, **kwargs):
        if Path(path).resolve() == home_target_b:
            return False
        return original_access(path, mode, *args, **kwargs)

    with monkeypatch.context() as mp:
        mp.setattr(os, "access", selective_access)
        rec_b = FlightRecorder(storage_dir=None)
        assert rec_b.storage_dir == expected_temp

    # Case C: Path.home() raises RuntimeError
    def failing_home():
        raise RuntimeError("Could not determine home directory")

    with monkeypatch.context() as mp:
        mp.setattr(Path, "home", staticmethod(failing_home))
        rec_c = FlightRecorder(storage_dir=None)
        assert rec_c.storage_dir == expected_temp

    # Case D: Path.home() resolves inside _REPO_ROOT
    with monkeypatch.context() as mp:
        mp.setattr(Path, "home", staticmethod(lambda: _REPO_ROOT))
        rec_d = FlightRecorder(storage_dir=None)
        assert rec_d.storage_dir == expected_temp
        assert not _is_within_repo(rec_d.storage_dir)

    # Case E: unwritable SEAGENT_FLIGHT_RECORD_DIR falls back to temp dir
    bad_flight_dir = tmp_path / "unwritable_flight"
    with monkeypatch.context() as mp:
        mp.setenv("SEAGENT_FLIGHT_RECORD_DIR", str(bad_flight_dir))

        def block_bad_flight_mkdir(self, *args, **kwargs):
            if str(self).startswith(str(bad_flight_dir.resolve())):
                raise PermissionError("Access denied")
            return original_mkdir(self, *args, **kwargs)

        mp.setattr(Path, "mkdir", block_bad_flight_mkdir)
        rec_e = FlightRecorder(storage_dir=None)
        assert rec_e.storage_dir == expected_temp


def test_get_instance_singleton_behavior(monkeypatch, tmp_path):
    monkeypatch.setenv("SEAGENT_FLIGHT_RECORD_DIR", str(tmp_path / "singleton_dir"))
    FlightRecorder.reset_instance()

    r1 = FlightRecorder.get_instance()
    r2 = FlightRecorder.get_instance(storage_dir=tmp_path / "other_dir")
    assert r1 is r2
    assert r1.storage_dir == (tmp_path / "singleton_dir").resolve()
