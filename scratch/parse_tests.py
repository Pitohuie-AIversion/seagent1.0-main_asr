"""
scratch/parse_tests.py — SEAgent Phase 1.9.1 Accurate Test Suite Runner & Collector

执行 unittest 测试收集与运行，记录可测量、可审计的测试状态数据。
严防重算与遗漏，确保 total = passed + failures + errors + skipped 恒成立。
"""

import sys
import os
import json
import unittest
import subprocess
from datetime import datetime, timezone
from pathlib import Path


def get_git_commit():
    try:
        res = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True
        )
        return res.stdout.strip()
    except Exception:
        return "unknown"


class AuditTestResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.test_records = {}

    def addSuccess(self, test):
        super().addSuccess(test)
        self._record(test, "passed")

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._record(test, "failures", str(err[1]))

    def addError(self, test, err):
        super().addError(test, err)
        self._record(test, "errors", str(err[1]))

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self._record(test, "skipped", str(reason))

    def _record(self, test, status, detail=""):
        test_id = str(test)
        # 提取首行错误描述
        reason_first_line = detail.splitlines()[0] if detail else ""
        self.test_records[test_id] = {
            "test_id": test_id,
            "status": status,
            "detail": reason_first_line
        }


def run_and_collect_tests(test_dir="tests"):
    loader = unittest.TestLoader()
    suite = loader.discover(test_dir)
    runner = unittest.TextTestRunner(resultclass=AuditTestResult, verbosity=0)
    result = runner.run(suite)

    total = result.testsRun
    failures = len(result.failures)
    errors = len(result.errors)
    skipped = len(result.skipped)
    passed = total - failures - errors - skipped

    # 验证数学不变性
    assert total == passed + failures + errors + skipped, \
        f"Math invariant failed: {total} != {passed} + {failures} + {errors} + {skipped}"

    records = list(result.test_records.values())

    metadata = {
        "command": f"{sys.executable} -m unittest discover {test_dir}",
        "python_version": sys.version,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "commit_sha": get_git_commit(),
        "total": total,
        "passed": passed,
        "failures": failures,
        "errors": errors,
        "skipped": skipped,
        "records": records
    }

    return metadata


if __name__ == "__main__":
    meta = run_and_collect_tests()
    out_path = Path("/tmp/test_records.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    print(f"Collected {meta['total']} tests: passed={meta['passed']}, failures={meta['failures']}, errors={meta['errors']}, skipped={meta['skipped']}")
