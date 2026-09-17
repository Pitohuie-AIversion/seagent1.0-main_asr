from pathlib import Path
import shutil
import subprocess
import pytest


@pytest.mark.skipif(not shutil.which('node'), reason='Node.js is required for DOM behavior tests')
def test_dashboard_distinguishes_active_history_and_pending_delete():
    root=Path(__file__).resolve().parents[1]
    result=subprocess.run(['node',str(root/'tests/frontend_dashboard_lifecycle.cjs')],cwd=root,capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stdout+result.stderr
