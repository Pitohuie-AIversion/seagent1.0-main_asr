"""自动化测试产物不得读取或写入用户运行结果目录。"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

from src.id_sequence import next_daily_task_id
from src.result_paths import DEFAULT_RESULT_DIR, get_history_dir, get_result_dir, get_task_dir
from web_backend import TRANSLATION_CACHE_FILE


def _test_root() -> Path:
    configured = os.environ.get("SEAGENT_TEST_RESULT_DIR")
    assert configured, "测试入口必须在收集测试模块前设置 SEAGENT_TEST_RESULT_DIR"
    return Path(configured).resolve()


class TestRuntimeArtifactIsolation(unittest.TestCase):
    def test_test_runner_uses_one_isolated_artifact_tree(self) -> None:
        root = _test_root()

        self.assertEqual(get_result_dir().resolve(), root)
        self.assertEqual(get_task_dir().resolve(), root / "task")
        self.assertEqual(get_history_dir().resolve(), root / "history")
        self.assertEqual(TRANSLATION_CACHE_FILE.resolve(), root / "translation_cache.json")
        self.assertNotEqual(root, DEFAULT_RESULT_DIR.resolve())
        self.assertNotIn("SEAGENT_TASK_DIR", os.environ)
        self.assertNotIn("SEAGENT_HISTORY_DIR", os.environ)

    def test_child_process_inherits_same_isolated_artifact_tree(self) -> None:
        root = _test_root()
        command = (
            "import json; "
            "from src.result_paths import get_result_dir, get_task_dir, get_history_dir; "
            "print(json.dumps([str(get_result_dir()), str(get_task_dir()), str(get_history_dir())]))"
        )

        completed = subprocess.run(
            [sys.executable, "-c", command],
            check=True,
            capture_output=True,
            text=True,
            cwd=Path(__file__).resolve().parents[1],
        )

        self.assertEqual(
            json.loads(completed.stdout),
            [str(root), str(root / "task"), str(root / "history")],
        )

    def test_task_id_reservation_persists_only_under_test_root(self) -> None:
        root = _test_root()
        task_dir = get_task_dir(create=True)
        history_dir = get_history_dir(create=True)

        task_id = next_daily_task_id(
            "PI",
            "20991231",
            3,
            [(task_dir, "task_id"), (history_dir, "task_id")],
            allowed_prefixes=["PI", "PB", "CT"],
        )

        counter_file = root / ".id_sequences.json"
        sequence = int(task_id.rsplit("-", 1)[-1])
        self.assertTrue(counter_file.exists())
        self.assertEqual(
            json.loads(counter_file.read_text(encoding="utf-8"))["TASK:20991231"],
            sequence,
        )
