import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.run_real_asr_integration import required_environment


class RealAsrIntegrationRunnerTest(unittest.TestCase):
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
