import unittest
import sys
import json

class SummaryResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.test_records = []

    def addSuccess(self, test):
        super().addSuccess(test)
        self.test_records.append({
            "test": str(test),
            "status": "pass",
            "category": "N/A",
            "action": "none",
            "reason": ""
        })

    def addFailure(self, test, err):
        super().addFailure(test, err)
        reason = str(err[1])
        cat, action = self._classify(test, reason, "fail")
        self.test_records.append({
            "test": str(test),
            "status": "fail",
            "category": cat,
            "action": action,
            "reason": reason.splitlines()[0] if reason else ""
        })

    def addError(self, test, err):
        super().addError(test, err)
        reason = str(err[1])
        cat, action = self._classify(test, reason, "error")
        self.test_records.append({
            "test": str(test),
            "status": "error",
            "category": cat,
            "action": action,
            "reason": reason.splitlines()[0] if reason else ""
        })

    def _classify(self, test, reason, status):
        t_str = str(test)
        # Category A: Legacy (router interaction_type changes, TASK_CREATE/TASK_UPDATE, old prompt expectations)
        if any(kw in t_str or kw in reason for kw in [
            "TASK_CREATE", "TASK_UPDATE", "TASK_CANCEL", "TASK_STATUS", "intent",
            "test_p0", "test_phase1_", "test_intent_routing.py"
        ]):
            return "legacy", "update"
        # Category C: Fixture / Mock / Environment issues
        if any(kw in reason or kw in t_str for kw in [
            "FileExistsError", "FileNotFoundError", "multiprocess", "exitcode",
            "AssertionError: 1 != 0", "exitcode", "permission", "lock"
        ]):
            return "fixture", "repair"
        # Category B: Regression
        return "regression", "fix"

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = loader.discover("tests")
    runner = unittest.TextTestRunner(resultclass=SummaryResult, verbosity=0)
    res = runner.run(suite)
    
    with open("/tmp/test_records.json", "w") as f:
        json.dump({
            "total": res.testsRun,
            "failures": len(res.failures),
            "errors": len(res.errors),
            "records": res.test_records
        }, f, ensure_ascii=False, indent=2)
    print(f"Recorded {res.testsRun} tests: {len(res.failures)} failures, {len(res.errors)} errors")
