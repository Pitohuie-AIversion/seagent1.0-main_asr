"""
tests/test_real_asr_environment_contract.py — Real ASR Environment Contract CPU Unit Tests

验证：
1. 缺少 SEAGENT_ASR_AUDIO 环境变量时报 ValueError；
2. 指定不存在的 SEAGENT_ASR_AUDIO 音频路径时抛出 FileNotFoundError；
3. SEAGENT_ASR_AUDIO 存在且预期术语正确时返回合法 Path 和词表列表。
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.run_real_asr_integration import required_environment


class RealAsrEnvironmentContractTest(unittest.TestCase):
    def test_requires_labelled_audio_path(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "SEAGENT_ASR_AUDIO"):
                required_environment()

    def test_rejects_missing_audio_file(self):
        with patch.dict(
            os.environ,
            {
                "SEAGENT_ASR_AUDIO": "/tmp/does-not-exist-seagent.wav",
                "SEAGENT_ASR_EXPECTED_TERMS": "采油树",
            },
            clear=True,
        ):
            with self.assertRaises(FileNotFoundError):
                required_environment()

    def test_parses_existing_fixture_and_expected_terms(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            audio_path = Path(temp_dir) / "domain.wav"
            audio_path.write_bytes(b"RIFF-test-fixture")
            with patch.dict(
                os.environ,
                {
                    "SEAGENT_ASR_AUDIO": str(audio_path),
                    "SEAGENT_ASR_EXPECTED_TERMS": "采油树, 流花",
                },
                clear=True,
            ):
                actual_path, terms = required_environment()
            self.assertEqual(audio_path, actual_path)
            self.assertEqual(["采油树", "流花"], terms)


if __name__ == "__main__":
    unittest.main()
