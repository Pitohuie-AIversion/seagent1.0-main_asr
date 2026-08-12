"""
tests/test_frontend_integrity.py - Unit test to verify frontend assets integrity across decoupled HTML, JS, and CSS files.
"""

import unittest
from pathlib import Path
import re

class TestFrontendIntegrity(unittest.TestCase):
    def setUp(self):
        self.root_dir = Path(__file__).resolve().parent.parent
        self.html_path = self.root_dir / "frontend" / "index.html"
        self.js_path = self.root_dir / "frontend" / "js" / "index.js"
        self.css_path = self.root_dir / "frontend" / "css" / "index.css"

        self.assertTrue(self.html_path.exists(), "frontend/index.html does not exist!")
        self.assertTrue(self.js_path.exists(), "frontend/js/index.js does not exist!")
        self.assertTrue(self.css_path.exists(), "frontend/css/index.css does not exist!")

        with open(self.html_path, "r", encoding="utf-8") as f:
            self.html_content = f.read()
        with open(self.js_path, "r", encoding="utf-8") as f:
            self.js_content = f.read()
        with open(self.css_path, "r", encoding="utf-8") as f:
            self.css_content = f.read()

    def test_critical_dom_ids(self):
        """Assert that all interactive elements and metrics placeholders in frontend/index.html have expected IDs."""
        required_ids = [
            "messages",
            "messageInput",
            "voiceBtn",
            "asrPanel",
            "sendBtn",
            "resetBtn",
            "simulatedTimeDisplay",
            "timePicker",
            "setTimeBtn",
            "taskInfo",
            "collectedFields",
            "missingFields",
            "resultCard",
            "finalJson",
            "historyBtn",
            "historyList",
            "langSelect",
            "audioWaveformWrapper"
        ]
        
        for element_id in required_ids:
            pattern = rf'id=["\']{element_id}["\']'
            self.assertTrue(
                re.search(pattern, self.html_content) is not None,
                f"Missing critical DOM element with ID: '{element_id}' in frontend/index.html!"
            )

    def test_translation_dictionary_structure(self):
        """Verify that the translation configurations in frontend/js/index.js contain required keys in ZH and EN."""
        i18n_match = re.search(r'const I18N = (\{.*?\n\s*\};)', self.js_content, re.DOTALL)
        self.assertIsNotNone(i18n_match, "Could not locate I18N translation object inside frontend/js/index.js!")
        
        i18n_block = i18n_match.group(1)
        
        self.assertIn("zh:", i18n_block, "ZH translation language is missing in I18N dictionary!")
        self.assertIn("en:", i18n_block, "EN translation language is missing in I18N dictionary!")
        
        expected_keys = [
            "title", "subtitle", "simtime", "setBtn", "curtask", 
            "collected", "missing", "finaljson", "history", "resetBtn",
            "inputPlaceholder", "voiceBtn", "voiceStop", "sendBtn"
        ]
        
        for k in expected_keys:
            self.assertIn(k, i18n_block, f"Required translation key '{k}' is missing in I18N dictionary!")

    def test_no_legacy_emojis_in_titles(self):
        """Assert that raw legacy emojis are removed from headers in JS I18N, SVGs exist in HTML, and CSS classes exist in CSS."""
        forbidden_emojis = ["⏱️", "📋", "✅", "❓", "📄", "📜", "📡", "🌊", "🛠️"]
        
        i18n_match = re.search(r'const I18N = (\{.*?\n\s*\};)', self.js_content, re.DOTALL)
        self.assertIsNotNone(
            i18n_match,
            "Could not locate I18N translation object inside frontend/js/index.js!",
        )
        i18n_block = i18n_match.group(1)
        header_keys = [
            "simtime", "curtask", "collected", "missing", "finaljson", "history"
        ]
        for key in header_keys:
            matches = re.findall(rf'{key}:\s*["\'](.*?)["\']', i18n_block)
            for val in matches:
                for emoji in forbidden_emojis:
                    self.assertNotIn(
                        emoji, val,
                        f"Header translation key '{key}' value '{val}' still contains legacy emoji '{emoji}'!"
                    )

        # Verify SVGs are present in HTML
        self.assertTrue("<svg" in self.html_content, "frontend/index.html is missing high-tech inline SVG tags!")
        # Verify CSS class definition exists in CSS file
        self.assertTrue(".tech-icon" in self.css_content, "frontend/css/index.css is missing '.tech-icon' CSS class definition!")

    def test_html_tag_matching(self):
        """Basic check to ensure core structural tags match in frontend/index.html."""
        self.assertTrue(self.html_content.strip().startswith("<!DOCTYPE html>"), "frontend/index.html must start with <!DOCTYPE html>")
        self.assertIn("</html>", self.html_content, "frontend/index.html is missing closing </html> tag")
        self.assertIn("</head>", self.html_content, "frontend/index.html is missing closing </head> tag")
        self.assertIn("</body>", self.html_content, "frontend/index.html is missing closing </body> tag")
        self.assertIn("</script>", self.html_content, "frontend/index.html is missing closing </script> tag")

    def test_estimated_task_id_preview_contract(self):
        """Assert static contract for task_id_preview rendering in frontend/js/index.js."""
        self.assertIn("data.task_id_preview", self.js_content)
        self.assertIn("officialTaskId", self.js_content)
        self.assertIn("previewTaskId", self.js_content)
        self.assertIn("预计", self.js_content)
        self.assertIn("Estimated", self.js_content)
        self.assertIn("const taskIdStr = officialTaskId || previewTaskId;", self.js_content)

if __name__ == "__main__":
    unittest.main()
