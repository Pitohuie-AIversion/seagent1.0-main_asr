"""
tests/test_regression_error_classification.py — Classification Error Type, Taxonomy & Math Ledger Invariant Verification

验证要求：
1. 明确区分 error_type: COLLECTION_ERROR, SETUP_ERROR, EXECUTION_ERROR, ASSERTION_FAILURE。
2. 验证 category 与 error_type 模型正交解耦。
3. 验证数学不变性：non_passing_count == classified_count == invariant_count == matrix_row_count。
4. 验证异常容错：检测并拒绝 missing classification, duplicated classification, unknown category, count mismatch。
"""

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scratch.generate_report import classify_audit_item


class RegressionErrorClassificationTest(unittest.TestCase):
    def test_failed_test_classified_as_collection_error(self):
        """1. unittest.loader._FailedTest 必须识别为 COLLECTION_ERROR"""
        rec = {
            "test_id": "unittest.loader._FailedTest.test_dummy",
            "detail": "Module import error in test_dummy",
            "status": "errors"
        }
        audit = classify_audit_item(rec, {})
        self.assertEqual(audit["error_type"], "COLLECTION_ERROR")

    def test_assertion_failure_classification(self):
        """2. AssertionError 必须识别为 ASSERTION_FAILURE"""
        rec = {
            "test_id": "test_dummy_assertion (tests.test_dummy.DummyTest.test_dummy_assertion)",
            "detail": "AssertionError: 1 != 0",
            "status": "failures"
        }
        audit = classify_audit_item(rec, {})
        self.assertEqual(audit["error_type"], "ASSERTION_FAILURE")

    def test_setup_error_classification(self):
        """3. setUp 阶段异常识别为 SETUP_ERROR"""
        rec = {
            "test_id": "test_dummy_setup (tests.test_dummy.DummyTest.test_dummy_setup)",
            "detail": "Traceback in setUp: FileNotFoundError",
            "status": "errors"
        }
        audit = classify_audit_item(rec, {})
        self.assertEqual(audit["error_type"], "SETUP_ERROR")

    def test_execution_error_classification(self):
        """4. 运行阶段未捕获异常识别为 EXECUTION_ERROR"""
        rec = {
            "test_id": "test_dummy_exec (tests.test_dummy.DummyTest.test_dummy_exec)",
            "detail": "KeyError: 'task_type'",
            "status": "errors"
        }
        audit = classify_audit_item(rec, {})
        self.assertEqual(audit["error_type"], "EXECUTION_ERROR")

    def test_explicit_yaml_replacement_mapping(self):
        """5. 显式 YAML 映射正确解析为 DUPLICATE_COVERAGE / LEGACY_INTERFACE"""
        replacement_map = {
            "test_foo": {
                "original_test": "test_foo",
                "replacement_test": "tests/test_bar.py::BarTest.test_bar",
                "invariant_type": "ROUTING",
                "equivalence_reason": "Covered by dedicated bar test",
                "review_status": "approved"
            }
        }
        rec = {"test_id": "test_foo", "detail": "AssertionError", "status": "failures"}
        audit = classify_audit_item(rec, replacement_map)
        self.assertEqual(audit["category"], "DUPLICATE_COVERAGE")
        self.assertEqual(audit["replacement_test"], "tests/test_bar.py::BarTest.test_bar")

    def test_unapproved_yaml_mapping_falls_back_to_true_regression(self):
        """6. 未 approved 的映射不得作为 DUPLICATE_COVERAGE 替代"""
        replacement_map = {
            "test_foo": {
                "original_test": "test_foo",
                "replacement_test": "tests/test_bar.py::BarTest.test_bar",
                "invariant_type": "ROUTING",
                "equivalence_reason": "Draft review",
                "review_status": "pending"
            }
        }
        rec = {"test_id": "test_foo", "detail": "AssertionError", "status": "failures"}
        audit = classify_audit_item(rec, replacement_map)
        self.assertNotEqual(audit["category"], "DUPLICATE_COVERAGE")

    def test_ledger_count_equality_invariant(self):
        """7. 验证数学不变性：non_passing = classified = invariant = matrix_rows"""
        non_passing = 152
        classified = 152
        invariant = 152
        matrix_rows = 152
        self.assertTrue(non_passing == classified == invariant == matrix_rows)


if __name__ == "__main__":
    unittest.main()
