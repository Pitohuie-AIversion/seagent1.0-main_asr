"""Robot telemetry persistence, versioning, and concurrency contract tests."""

from __future__ import annotations

import multiprocessing
import os
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from src import exceptions as state_exceptions
from src.state_info import RobotStateInfo


StatePersistenceError = getattr(
    state_exceptions,
    "StatePersistenceError",
    type("MissingStatePersistenceError", (RuntimeError,), {}),
)
StateSnapshotValidationError = getattr(
    state_exceptions,
    "StateSnapshotValidationError",
    type("MissingStateSnapshotValidationError", (RuntimeError,), {}),
)
StateVersionConflict = getattr(
    state_exceptions,
    "StateVersionConflict",
    type("MissingStateVersionConflict", (RuntimeError,), {}),
)


def _state_update_worker(
    state_path: str,
    selector: str,
    params: dict,
    expected_version: int | None,
    start_barrier,
    result_queue,
) -> None:
    store = RobotStateInfo()
    store.state_file = Path(state_path)
    try:
        start_barrier.wait(timeout=10)
        result = store.set_status(
            selector,
            params,
            expected_version=expected_version,
        )
        result_queue.put(("ok", result))
    except BaseException as exc:  # pragma: no cover - asserted in parent process
        result_queue.put(
            (
                "error",
                type(exc).__name__,
                getattr(exc, "expected_version", None),
                getattr(exc, "current_version", None),
            )
        )


class RobotStateAtomicPersistenceTest(unittest.TestCase):
    def setUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        self.state_file = Path(self._temp_dir.name) / "state.yaml"
        self.store = RobotStateInfo()
        self.store.state_file = self.state_file

    def tearDown(self):
        self._temp_dir.cleanup()

    def _write_snapshot(self, snapshot: dict) -> bytes:
        content = yaml.safe_dump(
            snapshot,
            allow_unicode=True,
            sort_keys=False,
        ).encode("utf-8")
        self.state_file.write_bytes(content)
        return content

    def _read_snapshot(self) -> dict:
        return yaml.safe_load(self.state_file.read_text(encoding="utf-8"))

    def _run_workers(self, updates: list[tuple[str, dict, int | None]]) -> list:
        context = multiprocessing.get_context("spawn")
        barrier = context.Barrier(len(updates))
        result_queue = context.Queue()
        processes = [
            context.Process(
                target=_state_update_worker,
                args=(
                    str(self.state_file),
                    selector,
                    params,
                    expected_version,
                    barrier,
                    result_queue,
                ),
            )
            for selector, params, expected_version in updates
        ]
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=15)
        try:
            self.assertTrue(
                all(not process.is_alive() for process in processes),
                "concurrent state writers did not finish",
            )
            return [result_queue.get(timeout=2) for _ in processes]
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=2)
            result_queue.close()

    def test_legacy_snapshot_versions_upgrade_from_zero(self):
        self._write_snapshot(
            {
                "robots": {
                    "CRAWLER-1600-001": {
                        "depth": 340,
                        "update_timestamp": "2026-07-01T10:00:00+08:00",
                    }
                }
            }
        )

        legacy_state = self.store.get_robot_state("CRAWLER-1600-001")
        self.assertEqual(legacy_state["version"], 0)

        result = self.store.set_status(
            "CRAWLER-1600-001",
            {"depth": 350},
            expected_version=0,
        )

        self.assertEqual(result["version"], 1)
        self.assertEqual(result["store_version"], 1)
        snapshot = self._read_snapshot()
        self.assertEqual(snapshot["store_version"], 1)
        self.assertEqual(snapshot["robots"]["CRAWLER-1600-001"]["version"], 1)
        self.assertEqual(snapshot["robots"]["CRAWLER-1600-001"]["depth"], 350)

    def test_every_update_refreshes_system_owned_timestamps(self):
        old_timestamp = "2026-07-01T10:00:00+08:00"
        self._write_snapshot(
            {
                "store_version": 3,
                "robots": {
                    "CRAWLER-1600-001": {
                        "version": 2,
                        "depth": 340,
                        "updated_at": old_timestamp,
                        "update_timestamp": old_timestamp,
                    }
                },
            }
        )

        result = self.store.set_status(
            "CRAWLER-1600-001",
            {
                "depth": 350,
                "updated_at": "1999-01-01T00:00:00+08:00",
                "update_timestamp": "1999-01-01T00:00:00+08:00",
                "version": 99,
                "store_version": 99,
            },
            expected_version=2,
        )

        self.assertNotEqual(result["updated_at"], old_timestamp)
        self.assertNotEqual(result["updated_at"], "1999-01-01T00:00:00+08:00")
        self.assertEqual(result["state"]["updated_at"], result["updated_at"])
        self.assertEqual(
            result["state"]["update_timestamp"],
            result["updated_at"],
        )
        self.assertEqual(result["version"], 3)
        self.assertEqual(result["store_version"], 4)

    def test_alias_unit_and_status_ref_share_one_state_node(self):
        result = self.store.set_status(
            "金牛座一号机",
            {"depth": 350},
            expected_version=0,
        )

        self.assertEqual(result["status_ref"], "CRAWLER-1600-001")
        by_alias = self.store.get_robot_state("金牛座一号机")
        by_unit = self.store.get_robot_state("CRAWLER-1600-001")
        by_display_name = self.store.get_robot_state(
            "履带式海底重载作业机器人1600HP-001"
        )
        self.assertEqual(by_alias, by_unit)
        self.assertEqual(by_display_name, by_unit)
        self.assertEqual(by_unit["depth"], 350)
        self.assertEqual(by_unit["version"], 1)
        self.assertEqual(
            set(self._read_snapshot()["robots"]),
            {"CRAWLER-1600-001"},
        )

    def test_model_and_family_alias_resolve_to_unique_status_ref(self):
        self.assertEqual(
            self.store.resolve_status_ref("crawler_heavy_seabed_robot_1600hp"),
            "CRAWLER-1600-001",
        )
        self.assertEqual(
            self.store.resolve_status_ref("履带式海底重载作业机器人 1600HP"),
            "CRAWLER-1600-001",
        )
        self.assertEqual(
            self.store.resolve_status_ref("金牛座"),
            "CRAWLER-1600-001",
        )

    def test_stale_expected_version_preserves_file_byte_for_byte(self):
        original = self._write_snapshot(
            {
                "store_version": 7,
                "robots": {
                    "CRAWLER-1600-001": {
                        "version": 3,
                        "depth": 340,
                        "updated_at": "2026-07-01T10:00:00+08:00",
                        "update_timestamp": "2026-07-01T10:00:00+08:00",
                    }
                },
            }
        )

        with self.assertRaises(StateVersionConflict) as raised:
            self.store.set_status(
                "CRAWLER-1600-001",
                {"depth": 350},
                expected_version=2,
            )

        self.assertEqual(self.state_file.read_bytes(), original)
        self.assertEqual(raised.exception.expected_version, 2)
        self.assertEqual(raised.exception.current_version, 3)

    def test_unknown_selector_fails_closed_without_creating_ghost_node(self):
        original = self._write_snapshot(
            {
                "store_version": 1,
                "robots": {
                    "CRAWLER-1600-001": {
                        "version": 1,
                        "depth": 340,
                    }
                },
            }
        )

        with self.assertRaises(ValueError):
            self.store.set_status("CRAWLER-1600-OO1", {"depth": 350})

        self.assertEqual(self.state_file.read_bytes(), original)

    def test_malformed_yaml_fails_closed_without_overwrite(self):
        malformed = b"robots:\n  CRAWLER-1600-001: [unterminated\n"
        self.state_file.write_bytes(malformed)

        with self.assertRaises(StateSnapshotValidationError):
            self.store.get_robot_state("CRAWLER-1600-001")
        with self.assertRaises(StateSnapshotValidationError):
            self.store.set_status("CRAWLER-1600-001", {"depth": 350})

        self.assertEqual(self.state_file.read_bytes(), malformed)

    def test_replace_failure_preserves_original_and_cleans_owned_temp(self):
        original = self._write_snapshot(
            {
                "store_version": 1,
                "robots": {
                    "CRAWLER-1600-001": {
                        "version": 1,
                        "depth": 340,
                    }
                },
            }
        )

        with patch("src.state_info.os.replace", side_effect=OSError("replace failed")):
            with self.assertRaises(StatePersistenceError):
                self.store.set_status(
                    "CRAWLER-1600-001",
                    {"depth": 350},
                    expected_version=1,
                )

        self.assertEqual(self.state_file.read_bytes(), original)
        self.assertEqual(
            [path for path in self.state_file.parent.iterdir() if path.suffix == ".tmp"],
            [],
        )

    def test_file_fsync_failure_preserves_original_and_reports_failure(self):
        original = self._write_snapshot(
            {
                "store_version": 1,
                "robots": {
                    "CRAWLER-1600-001": {
                        "version": 1,
                        "depth": 340,
                    }
                },
            }
        )

        with patch("src.state_info.os.fsync", side_effect=OSError("file fsync failed")):
            with self.assertRaises(StatePersistenceError):
                self.store.set_status(
                    "CRAWLER-1600-001",
                    {"depth": 350},
                    expected_version=1,
                )

        self.assertEqual(self.state_file.read_bytes(), original)

    def test_directory_fsync_failure_is_not_reported_as_success(self):
        self._write_snapshot(
            {
                "store_version": 1,
                "robots": {
                    "CRAWLER-1600-001": {
                        "version": 1,
                        "depth": 340,
                    }
                },
            }
        )
        real_fsync = os.fsync

        def fail_for_directory(file_descriptor: int) -> None:
            if stat.S_ISDIR(os.fstat(file_descriptor).st_mode):
                raise OSError("directory fsync failed")
            real_fsync(file_descriptor)

        with patch("src.state_info.os.fsync", side_effect=fail_for_directory):
            with self.assertRaises(StatePersistenceError):
                self.store.set_status(
                    "CRAWLER-1600-001",
                    {"depth": 350},
                    expected_version=1,
                )

    def test_target_is_old_snapshot_until_atomic_replace(self):
        original = self._write_snapshot(
            {
                "store_version": 1,
                "robots": {
                    "CRAWLER-1600-001": {
                        "version": 1,
                        "depth": 340,
                    }
                },
            }
        )
        before_replace = threading.Event()
        allow_replace = threading.Event()
        real_replace = os.replace
        errors: list[BaseException] = []

        def paused_replace(source, destination):
            before_replace.set()
            if not allow_replace.wait(timeout=5):
                raise TimeoutError("test did not release atomic replace")
            return real_replace(source, destination)

        def update_state():
            try:
                self.store.set_status(
                    "CRAWLER-1600-001",
                    {"depth": 350},
                    expected_version=1,
                )
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        with patch("src.state_info.os.replace", side_effect=paused_replace):
            writer = threading.Thread(target=update_state)
            writer.start()
            self.assertTrue(
                before_replace.wait(timeout=3),
                "writer never reached atomic replace",
            )
            self.assertEqual(self.state_file.read_bytes(), original)
            allow_replace.set()
            writer.join(timeout=5)

        self.assertFalse(writer.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self._read_snapshot()["robots"]["CRAWLER-1600-001"]["depth"], 350)

    def test_lock_file_follows_reassigned_state_file(self):
        self.store.set_status("CRAWLER-1600-001", {"depth": 350})

        self.assertTrue(
            self.state_file.with_name(f".{self.state_file.name}.lock").exists()
        )

    def test_concurrent_updates_do_not_lose_data(self):
        self._write_snapshot(
            {
                "store_version": 0,
                "robots": {
                    "CRAWLER-1600-001": {"version": 0, "depth": 300},
                    "WROV-250-001": {"version": 0, "depth": 300},
                },
            }
        )

        results = self._run_workers(
            [
                ("CRAWLER-1600-001", {"depth": 350}, 0),
                ("WROV-250-001", {"depth": 360}, 0),
            ]
        )

        self.assertEqual([result[0] for result in results].count("ok"), 2, results)
        snapshot = self._read_snapshot()
        self.assertEqual(snapshot["store_version"], 2)
        self.assertEqual(snapshot["robots"]["CRAWLER-1600-001"]["depth"], 350)
        self.assertEqual(snapshot["robots"]["WROV-250-001"]["depth"], 360)
        self.assertEqual(snapshot["robots"]["CRAWLER-1600-001"]["version"], 1)
        self.assertEqual(snapshot["robots"]["WROV-250-001"]["version"], 1)

    def test_concurrent_same_robot_rejects_stale_version(self):
        self._write_snapshot(
            {
                "store_version": 0,
                "robots": {
                    "CRAWLER-1600-001": {"version": 0, "depth": 300},
                },
            }
        )

        results = self._run_workers(
            [
                ("CRAWLER-1600-001", {"depth": 350}, 0),
                ("CRAWLER-1600-001", {"depth": 360}, 0),
            ]
        )

        self.assertEqual([result[0] for result in results].count("ok"), 1, results)
        errors = [result for result in results if result[0] == "error"]
        self.assertEqual(len(errors), 1, results)
        self.assertEqual(errors[0][1], "StateVersionConflict")
        snapshot = self._read_snapshot()
        self.assertEqual(snapshot["store_version"], 1)
        self.assertEqual(snapshot["robots"]["CRAWLER-1600-001"]["version"], 1)
        self.assertIn(
            snapshot["robots"]["CRAWLER-1600-001"]["depth"],
            {350, 360},
        )


if __name__ == "__main__":
    unittest.main()
