import ast
from pathlib import Path
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class WebBackendSerializationGuardTest(unittest.TestCase):
    def test_json_module_is_imported_for_serialization_guard(self):
        source = (PROJECT_ROOT / "web_backend.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported_names = set()
        for node in tree.body:
            if isinstance(node, ast.Import):
                imported_names.update(alias.asname or alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported_names.update(alias.asname or alias.name for alias in node.names)

        self.assertIn("json", imported_names)
        self.assertIn("json.dumps", source)


if __name__ == "__main__":
    unittest.main()
