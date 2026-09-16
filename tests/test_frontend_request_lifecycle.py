"""Execute frontend request lifecycle regressions with Node's built-in APIs."""

from pathlib import Path
import shutil
import subprocess
import unittest


class TestFrontendRequestLifecycle(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is required for frontend behavior tests")
    def test_request_cleanup_and_history_restore(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            ["node", str(root / "tests/frontend_request_lifecycle.cjs")],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
