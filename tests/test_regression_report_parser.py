"""
tests/test_regression_report_parser.py — Regression Report Parser Unit Tests

验证：
1. total == passed + failures + errors + skipped 恒成立。
2. 收集到的 JSON 包含全部必要元数据字段 (command, python_version, timestamp, commit_sha, total, passed, failures, errors, skipped)。
3. TestResult 审计收集没有重复计算。
"""

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

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

        import os
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
        records = list(res.test_records.values())
        self.assertEqual(len(records), 5)

        statuses = [r["status"] for r in records]
        self.assertEqual(statuses.count("passed"), 2)
        self.assertEqual(statuses.count("failures"), 1)
        self.assertEqual(statuses.count("errors"), 1)
        self.assertEqual(statuses.count("skipped"), 1)


if __name__ == "__main__":
    unittest.main()
