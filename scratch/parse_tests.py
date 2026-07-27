"""
scratch/parse_tests.py — SEAgent Phase 1.9.4 Audit Data Collector

分别记录 runner_counters 与 record_counters，绝不用 records 数量反向覆盖 runner 统计。
强校验 runner_counters == record_counters，如果不一致，记录差异清单并退出。
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
        self.test_records = []
        self._seen_ids = {}

    def _get_unique_test_id(self, test):
        raw = str(test)
        count = self._seen_ids.get(raw, 0) + 1
        self._seen_ids[raw] = count
        return raw if count == 1 else f"{raw} [instance {count}]"

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
        unique_id = self._get_unique_test_id(test)
        first_line = detail.splitlines()[-1] if detail else ""
        self.test_records.append({
            "test_id": unique_id,
            "status": status,
            "detail": first_line
        })


def run_and_collect_tests(test_dir="tests"):
    loader = unittest.TestLoader()
    suite = loader.discover(test_dir)
    runner = unittest.TextTestRunner(resultclass=AuditTestResult, verbosity=0)
    result = runner.run(suite)

    # 1. Direct Runner Counters
    runner_counters = {
        "tests_run": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "skipped": len(result.skipped)
    }

    records = result.test_records

    # 2. Record Counters
    rec_failures = sum(1 for r in records if r["status"] == "failures")
    rec_errors = sum(1 for r in records if r["status"] == "errors")
    rec_skipped = sum(1 for r in records if r["status"] == "skipped")
    rec_passed = sum(1 for r in records if r["status"] == "passed")

    record_counters = {
        "total": len(records),
        "passed": rec_passed,
        "failures": rec_failures,
        "errors": rec_errors,
        "skipped": rec_skipped
    }

    # 3. Independent Audit Comparison
    mismatches = []
    if runner_counters["tests_run"] != record_counters["total"]:
        mismatches.append(f"tests_run ({runner_counters['tests_run']}) != total ({record_counters['total']})")
    if runner_counters["failures"] != record_counters["failures"]:
        mismatches.append(f"runner failures ({runner_counters['failures']}) != record failures ({record_counters['failures']})")
    if runner_counters["errors"] != record_counters["errors"]:
        mismatches.append(f"runner errors ({runner_counters['errors']}) != record errors ({record_counters['errors']})")
    if runner_counters["skipped"] != record_counters["skipped"]:
        mismatches.append(f"runner skipped ({runner_counters['skipped']}) != record skipped ({record_counters['skipped']})")

    if mismatches:
        print("FATAL: Runner vs Record Counters Audit Discrepancy Detected!")
        for m in mismatches:
            print(f"  - {m}")
        sys.exit(1)

    metadata = {
        "command": f"{sys.executable} -m unittest discover {test_dir}",
        "python_version": sys.version,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "commit_sha": get_git_commit(),
        "runner_counters": runner_counters,
        "record_counters": record_counters,
        "records": records
    }

    out_path = Path("/tmp/test_records.json")
    out_path.write_text(json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Collected {runner_counters['tests_run']} test records successfully (runner_counters == record_counters). Saved to {out_path}.")


if __name__ == "__main__":
    run_and_collect_tests()
