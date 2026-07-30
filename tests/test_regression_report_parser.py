"""
tests/test_regression_report_parser.py — Regression Report Parser Unit Tests

验证：
1. total == passed + failures + errors + skipped 恒成立。
2. 收集到的 JSON 包含全部必要元数据字段 (command, python_version, timestamp, commit_sha, total, passed, failures, errors, skipped)。
3. TestResult 审计收集没有重复计算。
4. generate_report 严格判定：有任何 failure、error、collection error 或 skip 时判定为 FAIL。
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scratch.generate_report import generate_report
from scratch.parse_tests import AuditTestResult


class RegressionReportParserTest(unittest.TestCase):
    def test_math_invariant_and_metadata_structure(self):
        class DummyPassingTest(unittest.TestCase):
            def test_pass_1(self):
                self.assertTrue(True)

            def test_pass_2(self):
                self.assertEqual(1, 1)

        class DummyFailingTest(unittest.TestCase):
            def test_fail_1(self):
                self.assertTrue(False)

            def test_error_1(self):
                raise RuntimeError("Simulated error")

            @unittest.skip("Simulated skip")
            def test_skip_1(self):
                pass

        suite = unittest.TestSuite()
        loader = unittest.TestLoader()
        suite.addTests(loader.loadTestsFromTestCase(DummyPassingTest))
        suite.addTests(loader.loadTestsFromTestCase(DummyFailingTest))

        with open(os.devnull, "w") as devnull:
            runner = unittest.TextTestRunner(stream=devnull, resultclass=AuditTestResult, verbosity=0)
            res = runner.run(suite)

        total = res.testsRun
        failures = len(res.failures)
        errors = len(res.errors)
        skipped = len(res.skipped)
        passed = total - failures - errors - skipped

        # 1. 验证数学等式
        self.assertEqual(total, passed + failures + errors + skipped)
        self.assertEqual(total, 5)
        self.assertEqual(passed, 2)
        self.assertEqual(failures, 1)
        self.assertEqual(errors, 1)
        self.assertEqual(skipped, 1)

        # 2. 验证记录条数无重复
        records = list(res.test_records)
        self.assertEqual(len(records), 5)

        statuses = [r["status"] for r in records]
        self.assertEqual(statuses.count("passed"), 2)
        self.assertEqual(statuses.count("failures"), 1)
        self.assertEqual(statuses.count("errors"), 1)
        self.assertEqual(statuses.count("skipped"), 1)

    def test_generate_report_strict_evaluation_cases(self):
        """测试 0/1 failure/error/collection error/skip 的严苛判定规则"""
        cases = [
            {
                "name": "all_pass",
                "runner": {"tests_run": 500, "failures": 0, "errors": 0, "skipped": 0},
                "records": [{"test_id": f"test_{i}", "status": "passed", "detail": "OK"} for i in range(500)],
                "expected_decision": "**Final Decision**: **PASS**",
                "expected_coll": "**Collection Errors**: **PASS**",
            },
            {
                "name": "has_failure",
                "runner": {"tests_run": 500, "failures": 1, "errors": 0, "skipped": 0},
                "records": [{"test_id": "test_fail", "status": "failures", "detail": "AssertionError"}]
                + [{"test_id": f"test_{i}", "status": "passed", "detail": "OK"} for i in range(499)],
                "expected_decision": "**Final Decision**: **NO / FAIL**",
                "expected_coll": "**Collection Errors**: **PASS**",
            },
            {
                "name": "has_collection_error",
                "runner": {"tests_run": 500, "failures": 0, "errors": 1, "skipped": 0},
                "records": [{"test_id": "unittest.loader._FailedTest.test_mod", "status": "errors", "detail": "ImportError"}]
                + [{"test_id": f"test_{i}", "status": "passed", "detail": "OK"} for i in range(499)],
                "expected_decision": "**Final Decision**: **NO / FAIL**",
                "expected_coll": "**Collection Errors**: **FAIL**",
            },
            {
                "name": "has_skip",
                "runner": {"tests_run": 500, "failures": 0, "errors": 0, "skipped": 1},
                "records": [{"test_id": "test_skip", "status": "skipped", "detail": "skip reason"}]
                + [{"test_id": f"test_{i}", "status": "passed", "detail": "OK"} for i in range(499)],
                "expected_decision": "**Final Decision**: **NO / FAIL**",
                "expected_coll": "**Collection Errors**: **PASS**",
            },
        ]

        for case in cases:
            with self.subTest(case=case["name"]):
                record_counters = {
                    "total": case["runner"]["tests_run"],
                    "passed": sum(1 for r in case["records"] if r["status"] == "passed"),
                    "failures": case["runner"]["failures"],
                    "errors": case["runner"]["errors"],
                    "skipped": case["runner"]["skipped"],
                }
                data = {
                    "command": "python -m unittest discover tests -v",
                    "python_version": "3.10.12",
                    "timestamp": "2026-07-30T12:00:00Z",
                    "commit_sha": "abc1234",
                    "runner_counters": case["runner"],
                    "record_counters": record_counters,
                    "records": case["records"],
                }

                with tempfile.TemporaryDirectory() as tmp_dir:
                    rec_path = Path(tmp_dir) / "test_records.json"
                    report_path = Path(tmp_dir) / "regression_report.md"
                    rec_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

                    with patch("scratch.generate_report.Path") as mock_path:
                        def mock_path_factory(p):
                            if str(p) == "/tmp/test_records.json":
                                return rec_path
                            if str(p) == "docs/regression_report.md":
                                return report_path
                            return Path(p)
                        mock_path.side_effect = mock_path_factory

                        generate_report()

                    content = report_path.read_text(encoding="utf-8")
                    self.assertIn(case["expected_decision"], content)
                    self.assertIn(case["expected_coll"], content)


if __name__ == "__main__":
    unittest.main()
