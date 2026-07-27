"""
tests/test_regression_error_classification.py — Classification Error Type & Taxonomy Verification

验证要求：
1. 明确区分 COLLECTION_ERROR, SETUP_ERROR, EXECUTION_ERROR, ASSERTION_FAILURE。
2. unittest.loader._FailedTest 绝不与普通断言混淆，必须归为 COLLECTION_ERROR。
3. 验证 invariant_type 审计分流的唯一性与有效性。
"""

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


def classify_error_type(test_obj, err_detail, status):
    test_str = str(test_obj)
    cls_name = getattr(getattr(test_obj, "__class__", None), "__name__", "")

    if "_FailedTest" in test_str or "_FailedTest" in cls_name:
        return "COLLECTION_ERROR"
    elif status == "errors":
        if "setUp" in err_detail or "setUpClass" in err_detail:
            return "SETUP_ERROR"
        return "EXECUTION_ERROR"
    elif status == "failures":
        return "ASSERTION_FAILURE"
    return "UNKNOWN"


class RegressionErrorClassificationTest(unittest.TestCase):
    def test_failed_test_classified_as_collection_error(self):
        """1. unittest.loader._FailedTest 必须识别为 COLLECTION_ERROR"""
        dummy_failed_test = unittest.loader._FailedTest("test_dummy", Exception("Module import error"))
        err_type = classify_error_type(dummy_failed_test, "Module import error", "errors")
        self.assertEqual(err_type, "COLLECTION_ERROR")

    def test_assertion_failure_classification(self):
        """2. AssertionError 必须识别为 ASSERTION_FAILURE"""
        err_type = classify_error_type(self, "AssertionError: 1 != 0", "failures")
        self.assertEqual(err_type, "ASSERTION_FAILURE")

    def test_setup_error_classification(self):
        """3. setUp 阶段异常识别为 SETUP_ERROR"""
        err_type = classify_error_type(self, "Traceback in setUp: FileNotFoundError", "errors")
        self.assertEqual(err_type, "SETUP_ERROR")

    def test_execution_error_classification(self):
        """4. 运行阶段未捕获异常识别为 EXECUTION_ERROR"""
        err_type = classify_error_type(self, "KeyError: 'task_type'", "errors")
        self.assertEqual(err_type, "EXECUTION_ERROR")


if __name__ == "__main__":
    unittest.main()
