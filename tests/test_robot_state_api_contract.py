"""HTTP contract tests for robot telemetry updates."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import web_backend
from src import exceptions as state_exceptions
from src.state_info import RobotStateInfo


StatePersistenceError = getattr(
    state_exceptions,
    "StatePersistenceError",
    type("MissingStatePersistenceError", (RuntimeError,), {}),
)


class RobotStateAPIContractTest(unittest.TestCase):
    def setUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        self.store = RobotStateInfo()
        self.store.state_file = Path(self._temp_dir.name) / "state.yaml"
        self.old_kb = web_backend._shared_kb
        web_backend._shared_kb = SimpleNamespace(state_info=self.store)
        web_backend.app.testing = True
        self.client = web_backend.app.test_client()

    def tearDown(self):
        web_backend._shared_kb = self.old_kb
        self._temp_dir.cleanup()

    def _post(self, payload: dict):
        return self.client.post(
            "/api/robot/set-state-info",
            data=json.dumps(payload),
            content_type="application/json",
            headers={"X-Request-ID": "req_robot_state_test"},
        )

    def test_success_contract_supports_legacy_call_without_expected_version(self):
        response = self._post(
            {
                "robot_name": "金牛座一号机",
                "params": {"depth": 350},
            }
        )

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["ok"], True)
        self.assertEqual(body["code"], 200)
        self.assertEqual(body["robot"], "金牛座一号机")
        self.assertEqual(body["status_ref"], "CRAWLER-1600-001")
        self.assertEqual(body["version"], 1)
        self.assertEqual(body["store_version"], 1)
        self.assertEqual(body["state"]["depth"], 350)
        self.assertEqual(body["updated_at"], body["state"]["update_timestamp"])
        self.assertEqual(body["request_id"], "req_robot_state_test")
        self.assertNotIn("final_timestamp", body)

    def test_stale_expected_version_returns_retryable_409(self):
        first = self._post(
            {
                "robot_name": "CRAWLER-1600-001",
                "params": {"depth": 350},
                "expected_version": 0,
            }
        )
        self.assertEqual(first.status_code, 200)

        response = self._post(
            {
                "robot_name": "CRAWLER-1600-001",
                "params": {"depth": 360},
                "expected_version": 0,
            }
        )

        self.assertEqual(response.status_code, 409)
        body = response.get_json()
        self.assertEqual(body["ok"], False)
        self.assertEqual(body["code"], 409)
        self.assertEqual(body["error"], "StateVersionConflict")
        self.assertEqual(body["retryable"], True)
        self.assertEqual(body["expected_version"], 0)
        self.assertEqual(body["current_version"], 1)
        self.assertNotIn("state", body)

    def test_persistence_failure_returns_retryable_500_without_path_leak(self):
        failing_store = MagicMock()
        failing_store.resolve_status_ref.return_value = "CRAWLER-1600-001"
        failing_store.set_status.side_effect = StatePersistenceError(
            "disk failed at /root/private/state.yaml"
        )
        web_backend._shared_kb = SimpleNamespace(state_info=failing_store)

        response = self._post(
            {
                "robot_name": "CRAWLER-1600-001",
                "params": {"depth": 350},
            }
        )

        self.assertEqual(response.status_code, 500)
        body = response.get_json()
        self.assertEqual(body["ok"], False)
        self.assertEqual(body["code"], 500)
        self.assertEqual(body["error"], "StatePersistenceError")
        self.assertEqual(body["retryable"], True)
        self.assertNotIn("/root/private", response.get_data(as_text=True))
        self.assertNotIn("state", body)

    def test_unknown_robot_and_invalid_expected_version_return_400(self):
        unknown_response = self._post(
            {
                "robot_name": "CRAWLER-1600-OO1",
                "params": {"depth": 350},
            }
        )
        invalid_version_response = self._post(
            {
                "robot_name": "CRAWLER-1600-001",
                "params": {"depth": 350},
                "expected_version": "zero",
            }
        )

        for response in (unknown_response, invalid_version_response):
            self.assertEqual(response.status_code, 400)
            body = response.get_json()
            self.assertEqual(body["ok"], False)
            self.assertEqual(body["code"], 400)
            self.assertEqual(body["retryable"], False)

    def test_missing_or_wrong_json_shape_returns_400(self):
        responses = [
            self.client.post(
                "/api/robot/set-state-info",
                data="not json",
                content_type="application/json",
            ),
            self._post({"robot_name": "CRAWLER-1600-001", "params": []}),
            self._post({"robot_name": "", "params": {"depth": 350}}),
        ]

        for response in responses:
            self.assertEqual(response.status_code, 400)
            body = response.get_json()
            self.assertEqual(body["ok"], False)
            self.assertEqual(body["retryable"], False)


if __name__ == "__main__":
    unittest.main()
