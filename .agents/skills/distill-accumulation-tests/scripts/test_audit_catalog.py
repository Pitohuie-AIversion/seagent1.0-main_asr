import tempfile
from pathlib import Path
import unittest

import audit_catalog


def valid_case(case_id="TS-01", **overrides):
    case = {
        "id": case_id,
        "title": "场景",
        "portable_invariant": "保持一个可迁移行为不变量",
        "layers": ["routing"],
        "outcome": "safe_response",
        "oracle": "structured",
        "next_gen_status": "planned",
    }
    case.update(overrides)
    return case


def valid_catalog(cases):
    return {"schema_version": 1, "suite": "test-suite", "cases": cases}


class CatalogValidationTest(unittest.TestCase):
    def test_accepts_valid_catalog(self):
        cases, errors = audit_catalog.validate_catalog(
            valid_catalog([valid_case()]), required_status=None
        )
        self.assertEqual(len(cases), 1)
        self.assertEqual(errors, [])

    def test_rejects_duplicate_ids(self):
        _, errors = audit_catalog.validate_catalog(
            valid_catalog([valid_case(), valid_case()]), required_status=None
        )
        self.assertIn("duplicate case id: TS-01", errors)

    def test_requires_reason_for_deferred_case(self):
        _, errors = audit_catalog.validate_catalog(
            valid_catalog([valid_case(next_gen_status="deferred")]),
            required_status=None,
        )
        self.assertTrue(any("defer_reason" in error for error in errors))

    def test_required_status_is_enforced(self):
        _, errors = audit_catalog.validate_catalog(
            valid_catalog([valid_case(next_gen_status="adapted")]),
            required_status="verified",
        )
        self.assertTrue(any("expected 'verified'" in error for error in errors))

    def test_runner_ast_extraction_ignores_non_case_calls(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            runner = Path(temp_dir) / "runner.py"
            runner.write_text(
                'IntegrationTestCase("TS-01", "one")\n'
                'other("TS-99")\n'
                'suite.IntegrationTestCase("TS-02", "two")\n',
                encoding="utf-8",
            )
            self.assertEqual(
                audit_catalog.runner_case_ids(runner, "IntegrationTestCase"),
                ["TS-01", "TS-02"],
            )

    def test_manual_heading_extraction_and_parity_errors(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manual = Path(temp_dir) / "manual.md"
            manual.write_text(
                "### TS-01｜first\n### TS-03｜extra\n",
                encoding="utf-8",
            )
            observed = audit_catalog.manual_case_ids(manual)
            self.assertEqual(observed, ["TS-01", "TS-03"])
            errors = audit_catalog.compare_ids(
                "manual document", observed, ["TS-01", "TS-02"]
            )
            self.assertTrue(any("TS-02" in error for error in errors))
            self.assertTrue(any("TS-03" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
