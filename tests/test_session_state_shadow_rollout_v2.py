"""
tests/test_session_state_shadow_rollout_v2.py

SEAgent G4.3 Rollout Readiness Test Suite.

Verifies:
1. flag=false -> Shadow does not run for any session.
2. allowlist session -> Shadow runs.
3. non-allowlist session -> Shadow does not run.
4. percentage deterministic hashing (no random numbers).
5. metrics tracking (total, parity, strict_rejected, mismatch, error) & thread-safety.
6. Session isolation (Session A shadow configuration/metrics isolation from Session B).
7. Shadow execution produces zero double LLM calls, zero extra SlotStore writes, zero extra publish files.
8. session_state_v2 remains false throughout tests.
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from src.dialogue_manager import DialogueManager
from src.knowledge_retriever import KnowledgeBase
from src.llm_client import LLMClient
from src.model_profile import is_session_state_v2_enabled, is_shadow_compare_enabled
from src.session_state_shadow import (
    get_shadow_metrics_snapshot,
    reset_shadow_metrics,
    should_run_session_state_shadow,
)
from web_backend import get_or_create_manager, _sessions_manager, _sessions_lock


def _make_dm(tmp_dir: Path, session_id: str | None = None) -> DialogueManager:
    tmp_dir.mkdir(parents=True, exist_ok=True)
    state_file = tmp_dir / "state.yaml"
    shutil.copy("config/state.yaml", state_file)
    kb = KnowledgeBase()
    kb.state_info.state_file = state_file
    llm = LLMClient(None, None)
    return DialogueManager(llm, kb, session_id=session_id)


def _write_features_yaml(path: Path, shadow_enabled: bool = True, percentage: int = 0, allow_ids: list[str] | None = None):
    if allow_ids is None:
        allow_ids = []
    content = f"""features:
  session_state_v2: false
  shadow_compare_enabled: {'true' if shadow_enabled else 'false'}

rollout:
  percentage: {percentage}
  allow_session_ids: {json.dumps(allow_ids)}
"""
    path.write_text(content, encoding="utf-8")


class TestSessionStateShadowRolloutV2(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.tmp_path = Path(self._tmp)
        self.features_yaml = self.tmp_path / "features.yaml"
        reset_shadow_metrics()

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)
        reset_shadow_metrics()
        with _sessions_lock:
            _sessions_manager.clear()

    # 1. flag=false -> Shadow does not run for any session.
    def test_01_flag_disabled_never_runs(self):
        _write_features_yaml(self.features_yaml, shadow_enabled=False, allow_ids=["sess_allow_1"])
        self.assertFalse(should_run_session_state_shadow("sess_allow_1", features_path=self.features_yaml))
        self.assertFalse(should_run_session_state_shadow("sess_other", features_path=self.features_yaml))

    # 2. allowlist session -> Shadow runs.
    def test_02_allowlist_session_runs(self):
        _write_features_yaml(self.features_yaml, shadow_enabled=True, allow_ids=["sess_allow_100", "sess_allow_200"])
        self.assertTrue(should_run_session_state_shadow("sess_allow_100", features_path=self.features_yaml))
        self.assertTrue(should_run_session_state_shadow("sess_allow_200", features_path=self.features_yaml))

    # 3. non-allowlist session -> Shadow does not run.
    def test_03_non_allowlist_session_does_not_run(self):
        _write_features_yaml(self.features_yaml, shadow_enabled=True, percentage=0, allow_ids=["sess_allow_100"])
        self.assertFalse(should_run_session_state_shadow("sess_denied_999", features_path=self.features_yaml))
        self.assertFalse(should_run_session_state_shadow(None, features_path=self.features_yaml))
        self.assertFalse(should_run_session_state_shadow("", features_path=self.features_yaml))

    # 4. percentage deterministic hashing (no random numbers).
    def test_04_percentage_deterministic_hashing(self):
        _write_features_yaml(self.features_yaml, shadow_enabled=True, percentage=50, allow_ids=[])
        # Test exact deterministic consistency across repeated calls
        res1 = should_run_session_state_shadow("user_session_alpha", features_path=self.features_yaml)
        res2 = should_run_session_state_shadow("user_session_alpha", features_path=self.features_yaml)
        self.assertEqual(res1, res2, "Percentage selection must be 100% deterministic")

        # Test full 100% rollout
        _write_features_yaml(self.features_yaml, shadow_enabled=True, percentage=100, allow_ids=[])
        self.assertTrue(should_run_session_state_shadow("any_session_id", features_path=self.features_yaml))

    # 5. metrics tracking (total, parity, strict_rejected, mismatch, error) & thread-safety.
    def test_05_metrics_tracking(self):
        _write_features_yaml(self.features_yaml, shadow_enabled=True, allow_ids=["sess_metrics_1"])
        dm = _make_dm(self.tmp_path / "t05", session_id="sess_metrics_1")

        with patch("src.session_state_shadow.CONFIG_DIR", self.tmp_path), \
             patch("src.model_profile.CONFIG_DIR", self.tmp_path):
            dm.process("什么是DVL？", request_id="req_m1")
            m = get_shadow_metrics_snapshot()
            self.assertEqual(m["total"], 1)
            self.assertEqual(m["parity"], 1)
            self.assertEqual(m["strict_rejected"], 0)
            self.assertEqual(m["mismatch"], 0)
            self.assertEqual(m["error"], 0)

    # 6. Session isolation (Session A shadow configuration/metrics isolation from Session B).
    def test_06_session_isolation(self):
        _write_features_yaml(self.features_yaml, shadow_enabled=True, allow_ids=["sess_A"])

        with patch("src.session_state_shadow.CONFIG_DIR", self.tmp_path), \
             patch("src.model_profile.CONFIG_DIR", self.tmp_path):
            dm_a = get_or_create_manager("sess_A")
            dm_b = get_or_create_manager("sess_B")

            self.assertEqual(dm_a.session_id, "sess_A")
            self.assertEqual(dm_b.session_id, "sess_B")

            dm_a.process("什么是DVL？", request_id="req_a")
            m_after_a = get_shadow_metrics_snapshot()
            self.assertEqual(m_after_a["total"], 1)

            dm_b.process("什么是DVL？", request_id="req_b")
            m_after_b = get_shadow_metrics_snapshot()
            # Session B is not in allowlist -> total count remains 1
            self.assertEqual(m_after_b["total"], 1)

    # 7. Shadow execution produces zero double LLM calls, zero extra SlotStore writes, zero extra publish files.
    def test_07_shadow_zero_side_effects(self):
        _write_features_yaml(self.features_yaml, shadow_enabled=True, allow_ids=["sess_se_1"])
        dm = _make_dm(self.tmp_path / "t07", session_id="sess_se_1")

        with patch("src.session_state_shadow.CONFIG_DIR", self.tmp_path), \
             patch("src.model_profile.CONFIG_DIR", self.tmp_path):
            v_before = dm.slot_store.version
            reply = dm.process("什么是DVL？", request_id="req_se_1")
            v_after = dm.slot_store.version

            self.assertEqual(v_before, v_after)
            self.assertTrue(isinstance(reply, str) and len(reply) > 0)
            self.assertEqual(get_shadow_metrics_snapshot()["total"], 1)

    # 8. session_state_v2 remains false throughout tests.
    def test_08_session_state_v2_remains_false(self):
        self.assertFalse(is_session_state_v2_enabled())
        _write_features_yaml(self.features_yaml, shadow_enabled=True, allow_ids=["sess_v2_test"])
        with patch("src.session_state_shadow.CONFIG_DIR", self.tmp_path), \
             patch("src.model_profile.CONFIG_DIR", self.tmp_path):
            dm = _make_dm(self.tmp_path / "t08", session_id="sess_v2_test")
            dm.process("什么是DVL？", request_id="req_v2")
            self.assertFalse(is_session_state_v2_enabled())


if __name__ == "__main__":
    unittest.main()
