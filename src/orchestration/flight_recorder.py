"""Subsea Mission Flight Recorder (Blackbox) & Time-Travel Deterministic Replay Engine.

Records append-only mission events with CRC-32 integrity signatures and provides
time-travel replay reconstruction of DAG states, robot telemetry, and replanning actions.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
import zlib
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.observability import trace_span

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _is_within_repo(path: Path, repo_root: Path = _REPO_ROOT) -> bool:
    """Return True if path resolves to repo_root or inside repo_root."""
    try:
        resolved_path = path.expanduser().resolve()
        resolved_repo = repo_root.resolve()
        return resolved_path == resolved_repo or resolved_path.is_relative_to(resolved_repo)
    except (OSError, RuntimeError, ValueError):
        return False


def _prepare_writable_dir(path: Path) -> Path | None:
    """Ensure path exists as a writable directory, returning resolved Path or None."""
    try:
        resolved = path.expanduser().resolve()
        resolved.mkdir(parents=True, exist_ok=True)
        if not resolved.is_dir():
            return None
        if not os.access(resolved, os.W_OK | os.X_OK):
            return None
        return resolved
    except (PermissionError, OSError, RuntimeError):
        return None


def _resolve_default_storage_dir() -> Path:
    """Resolve default FlightRecorder storage_dir outside the repository root.

    Priority:
    1. SEAGENT_FLIGHT_RECORD_DIR (if set and non-empty after strip)
    2. SEAGENT_RESULT_DIR / "flight_records" (if set and non-empty after strip)
    3. Path.home() / ".seagent" / "flight_records" (when outside repo and writable)
    4. Path(tempfile.gettempdir()) / "seagent_flight_records" fallback
    """
    flight_dir_env = os.environ.get("SEAGENT_FLIGHT_RECORD_DIR", "").strip()
    if flight_dir_env:
        prepared = _prepare_writable_dir(Path(flight_dir_env))
        if prepared is not None:
            return prepared
        logger.warning(
            "[FlightRecorder] Configured SEAGENT_FLIGHT_RECORD_DIR %s is not writable; falling back to temp dir",
            flight_dir_env,
        )
        temp_candidate = Path(tempfile.gettempdir()) / "seagent_flight_records"
        return _prepare_writable_dir(temp_candidate) or temp_candidate

    result_dir_env = os.environ.get("SEAGENT_RESULT_DIR", "").strip()
    if result_dir_env:
        prepared = _prepare_writable_dir(Path(result_dir_env) / "flight_records")
        if prepared is not None:
            return prepared
        logger.warning(
            "[FlightRecorder] Configured SEAGENT_RESULT_DIR %s/flight_records is not writable; falling back to temp dir",
            result_dir_env,
        )
        temp_candidate = Path(tempfile.gettempdir()) / "seagent_flight_records"
        return _prepare_writable_dir(temp_candidate) or temp_candidate

    try:
        home_candidate = Path.home() / ".seagent" / "flight_records"
    except (RuntimeError, OSError) as exc:
        logger.warning("[FlightRecorder] Could not determine home directory (%s); falling back to temp dir", exc)
        home_candidate = None

    if home_candidate is not None:
        if _is_within_repo(home_candidate):
            logger.warning(
                "[FlightRecorder] Home storage directory %s resolves inside repository %s; falling back to temp dir",
                home_candidate,
                _REPO_ROOT,
            )
        else:
            prepared = _prepare_writable_dir(home_candidate)
            if prepared is not None:
                return prepared
            logger.warning(
                "[FlightRecorder] Default directory %s is not writable; falling back to temp dir",
                home_candidate,
            )

    temp_candidate = Path(tempfile.gettempdir()) / "seagent_flight_records"
    prepared_temp = _prepare_writable_dir(temp_candidate)
    if prepared_temp is not None:
        return prepared_temp
    logger.warning("[FlightRecorder] Could not prepare fallback storage directory %s", temp_candidate)
    return temp_candidate


@dataclass
class FlightRecordEvent:
    """An immutable, append-only flight recorder event with CRC-32 checksum."""
    event_id: str
    mission_id: str
    seq: int
    timestamp: float
    iso_time: str
    event_type: str
    node_id: str | None = None
    robot_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    checksum: str = ""

    def compute_checksum(self) -> str:
        """Calculate CRC-32 hexadecimal checksum for tamper detection and crash integrity."""
        raw_payload = json.dumps(self.payload, sort_keys=True, ensure_ascii=False)
        sig_str = f"{self.event_id}|{self.mission_id}|{self.seq}|{self.timestamp:.4f}|{self.event_type}|{raw_payload}"
        crc = zlib.crc32(sig_str.encode("utf-8")) & 0xFFFFFFFF
        return f"{crc:08x}"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FlightRecordEvent:
        return cls(
            event_id=data["event_id"],
            mission_id=data["mission_id"],
            seq=int(data["seq"]),
            timestamp=float(data["timestamp"]),
            iso_time=data["iso_time"],
            event_type=data["event_type"],
            node_id=data.get("node_id"),
            robot_id=data.get("robot_id"),
            payload=data.get("payload") or {},
            checksum=data.get("checksum", ""),
        )


@dataclass
class ReplaySnapshot:
    """Reconstructed system state at a specific mission sequence or point in time."""
    mission_id: str
    replayed_up_to_seq: int
    target_time: float
    total_events_in_mission: int
    events_replayed_count: int
    graph_state: dict[str, Any]
    mermaid: str
    completed_nodes: list[str]
    failed_nodes: list[str]
    robot_telemetry: dict[str, Any]
    active_corridors: list[dict[str, Any]]
    replanning_history: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class FlightRecorder:
    """Thread-safe append-only mission blackbox logger with durable disk persistence."""

    _instance: FlightRecorder | None = None
    _singleton_lock = threading.RLock()

    def __init__(self, storage_dir: Path | str | None = None):
        self._lock = threading.RLock()
        if storage_dir is None or (isinstance(storage_dir, str) and not storage_dir.strip()):
            self.storage_dir = _resolve_default_storage_dir()
        else:
            self.storage_dir = Path(storage_dir).expanduser()
            try:
                self.storage_dir.mkdir(parents=True, exist_ok=True)
            except Exception as exc:
                logger.warning("[FlightRecorder] Could not create storage directory %s: %s", self.storage_dir, exc)

        # In-memory index of mission events: mission_id -> list[FlightRecordEvent]
        self._memory_cache: dict[str, list[FlightRecordEvent]] = {}
        self._seq_counters: dict[str, int] = {}

    @classmethod
    def get_instance(cls, storage_dir: Path | str | None = None) -> FlightRecorder:
        with cls._singleton_lock:
            if cls._instance is None:
                cls._instance = cls(storage_dir=storage_dir)
            return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        with cls._singleton_lock:
            cls._instance = None

    def record_event(
        self,
        mission_id: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
        node_id: str | None = None,
        robot_id: str | None = None,
    ) -> FlightRecordEvent:
        """Record and durably commit an immutable flight event."""
        with self._lock:
            curr_t = time.time()
            iso_t = datetime.now(timezone.utc).isoformat()
            seq = self._seq_counters.get(mission_id, 0) + 1
            self._seq_counters[mission_id] = seq

            evt_id = f"evt_{mission_id}_{seq:05d}"
            payload_dict = dict(payload or {})

            evt = FlightRecordEvent(
                event_id=evt_id,
                mission_id=mission_id,
                seq=seq,
                timestamp=curr_t,
                iso_time=iso_t,
                event_type=event_type,
                node_id=node_id,
                robot_id=robot_id,
                payload=payload_dict,
            )
            evt.checksum = evt.compute_checksum()

            # Update in-memory cache
            self._memory_cache.setdefault(mission_id, []).append(evt)

            # Durably append to disk
            self._persist_event(evt)
            return evt

    def _get_log_path(self, mission_id: str) -> Path:
        safe_id = "".join(c for c in mission_id if c.isalnum() or c in ("-", "_"))
        return self.storage_dir / f"{safe_id}.flight.jsonl"

    def _persist_event(self, evt: FlightRecordEvent) -> None:
        try:
            path = self._get_log_path(evt.mission_id)
            line = json.dumps(evt.to_dict(), ensure_ascii=False) + "\n"
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
        except Exception as exc:
            logger.error("[FlightRecorder] Failed to persist event %s to disk: %s", evt.event_id, exc)

    def get_mission_events(
        self,
        mission_id: str,
        min_seq: int = 1,
        max_seq: int | None = None,
    ) -> list[FlightRecordEvent]:
        """Load and return mission events from memory or disk log."""
        with self._lock:
            events = self._memory_cache.get(mission_id)
            if events is None:
                events = self._load_events_from_disk(mission_id)
                self._memory_cache[mission_id] = events
                if events:
                    self._seq_counters[mission_id] = events[-1].seq

            filtered = [e for e in events if e.seq >= min_seq]
            if max_seq is not None:
                filtered = [e for e in filtered if e.seq <= max_seq]
            return list(filtered)

    def _load_events_from_disk(self, mission_id: str) -> list[FlightRecordEvent]:
        path = self._get_log_path(mission_id)
        if not path.is_file():
            return []

        loaded: list[FlightRecordEvent] = []
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        loaded.append(FlightRecordEvent.from_dict(json.loads(line)))
        except Exception as exc:
            logger.error("[FlightRecorder] Error reading log %s: %s", path, exc)
        return loaded

    def verify_integrity(self, mission_id: str) -> tuple[bool, str, int]:
        """Verify CRC-32 integrity and monotonic sequence continuity.
        Returns: (is_valid, message, verified_event_count)."""
        with self._lock:
            events = self.get_mission_events(mission_id)
            if not events:
                return True, "No events recorded for mission", 0

            expected_seq = 1
            for evt in events:
                if evt.seq != expected_seq:
                    msg = f"Sequence gap detected: expected {expected_seq}, found {evt.seq}"
                    logger.warning("[FlightRecorder] Integrity failure on %s: %s", mission_id, msg)
                    return False, msg, expected_seq - 1

                recomputed = evt.compute_checksum()
                if evt.checksum != recomputed:
                    msg = f"CRC-32 checksum mismatch on seq {evt.seq}: recorded {evt.checksum}, computed {recomputed}"
                    logger.warning("[FlightRecorder] Integrity failure on %s: %s", mission_id, msg)
                    return False, msg, expected_seq - 1

                expected_seq += 1

            return True, f"All {len(events)} events verified intact with valid CRC-32 signatures", len(events)

    def export_blackbox_archive(self, mission_id: str) -> dict[str, Any]:
        """Export comprehensive, signed blackbox archive for external investigation or audit."""
        with self._lock:
            events = self.get_mission_events(mission_id)
            is_valid, status_msg, count = self.verify_integrity(mission_id)

            start_t = events[0].timestamp if events else None
            end_t = events[-1].timestamp if events else None
            duration_s = (end_t - start_t) if (start_t and end_t) else 0.0

            return {
                "archive_version": "1.0-subsea-blackbox",
                "mission_id": mission_id,
                "exported_at": datetime.now(timezone.utc).isoformat(),
                "integrity_valid": is_valid,
                "integrity_message": status_msg,
                "event_count": len(events),
                "duration_seconds": round(duration_s, 3),
                "start_time": datetime.fromtimestamp(start_t, tz=timezone.utc).isoformat() if start_t else None,
                "end_time": datetime.fromtimestamp(end_t, tz=timezone.utc).isoformat() if end_t else None,
                "events": [e.to_dict() for e in events],
            }


class MissionReplayer:
    """Time-travel deterministic replayer reconstructing subsea state from flight logs."""

    @staticmethod
    def replay_to_seq(
        mission_id: str,
        target_seq: int,
        recorder: FlightRecorder | None = None,
    ) -> ReplaySnapshot:
        """Reconstruct system DAG and robot telemetry up to target sequence number."""
        from src.task_graph import TaskGraph, TaskNode, TaskNodeStatus

        rec = recorder or FlightRecorder.get_instance()
        all_events = rec.get_mission_events(mission_id)
        if not all_events:
            raise ValueError(f"No recorded flight events found for mission '{mission_id}'")

        filtered = [e for e in all_events if e.seq <= target_seq]
        target_time = filtered[-1].timestamp if filtered else all_events[0].timestamp

        return MissionReplayer._reconstruct(mission_id, filtered, len(all_events), target_time, target_seq)

    @staticmethod
    def replay_to_timestamp(
        mission_id: str,
        target_timestamp: float,
        recorder: FlightRecorder | None = None,
    ) -> ReplaySnapshot:
        """Reconstruct system DAG and robot telemetry up to a target timestamp."""
        rec = recorder or FlightRecorder.get_instance()
        all_events = rec.get_mission_events(mission_id)
        if not all_events:
            raise ValueError(f"No recorded flight events found for mission '{mission_id}'")

        filtered = [e for e in all_events if e.timestamp <= target_timestamp]
        target_seq = filtered[-1].seq if filtered else 0

        return MissionReplayer._reconstruct(mission_id, filtered, len(all_events), target_timestamp, target_seq)

    @staticmethod
    def _reconstruct(
        mission_id: str,
        events: list[FlightRecordEvent],
        total_events: int,
        target_time: float,
        target_seq: int,
    ) -> ReplaySnapshot:
        from src.task_graph import TaskGraph, TaskNode, TaskNodeStatus

        graph: TaskGraph | None = None
        completed_nodes: list[str] = []
        failed_nodes: list[str] = []
        robot_telemetry: dict[str, Any] = {}
        active_corridors: list[dict[str, Any]] = []
        replanning_history: list[dict[str, Any]] = []

        for evt in events:
            # Reconstruct initial DAG
            if evt.event_type == "MISSION_CREATED" and "graph" in evt.payload:
                try:
                    graph = TaskGraph.from_dict(evt.payload["graph"])
                except Exception:
                    pass

            # Update node completion
            elif evt.event_type == "SUBTASK_COMPLETED":
                if evt.node_id:
                    completed_nodes.append(evt.node_id)
                    if graph and evt.node_id in graph.nodes:
                        graph.update_node_status(
                            evt.node_id,
                            TaskNodeStatus.COMPLETED,
                            result=evt.payload.get("telemetry"),
                        )
                if evt.robot_id and "telemetry" in evt.payload:
                    robot_telemetry[evt.robot_id] = evt.payload["telemetry"]

            # Update node failure
            elif evt.event_type == "SUBTASK_FAILED":
                if evt.node_id:
                    failed_nodes.append(evt.node_id)
                    if graph and evt.node_id in graph.nodes:
                        graph.update_node_status(
                            evt.node_id,
                            TaskNodeStatus.FAILED,
                            error=evt.payload.get("error"),
                        )

            # Reconstruct dynamic replanning structure
            elif evt.event_type == "REPLANNING_APPLIED":
                replanning_history.append(evt.payload)
                if "updated_graph" in evt.payload:
                    try:
                        graph = TaskGraph.from_dict(evt.payload["updated_graph"])
                    except Exception:
                        pass

            # Corridor reservations
            elif evt.event_type == "CORRIDOR_RESERVED":
                corridors = evt.payload.get("corridors") or []
                active_corridors.extend(corridors)
            elif evt.event_type == "CORRIDOR_CLEARED":
                active_corridors.clear()

            # Telemetry snapshot
            elif evt.event_type == "TELEMETRY_SAMPLE":
                if evt.robot_id:
                    robot_telemetry[evt.robot_id] = evt.payload

        if graph is None:
            graph = TaskGraph(graph_id=mission_id)

        return ReplaySnapshot(
            mission_id=mission_id,
            replayed_up_to_seq=target_seq,
            target_time=target_time,
            total_events_in_mission=total_events,
            events_replayed_count=len(events),
            graph_state=graph.to_dict(),
            mermaid=graph.to_mermaid(),
            completed_nodes=completed_nodes,
            failed_nodes=failed_nodes,
            robot_telemetry=robot_telemetry,
            active_corridors=active_corridors,
            replanning_history=replanning_history,
        )
