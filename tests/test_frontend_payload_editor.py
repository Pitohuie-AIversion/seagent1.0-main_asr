from pathlib import Path
import shutil
import subprocess
import pytest

@pytest.mark.skipif(not shutil.which('node'), reason='Node.js is required for frontend behavior tests')
def test_payload_editor_dom_lifecycle():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(['node', str(root / 'tests/frontend_payload_editor.cjs')], cwd=root, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
