"""
tests/test_frontend_integrity.py - Unit test to verify index.html integrity, critical DOM IDs, SVG tags, and translations.
"""

import unittest
from pathlib import Path
import re

class TestFrontendIntegrity(unittest.TestCase):
    def setUp(self):
        self.root_dir = Path(__file__).parent.parent
        self.html_path = self.root_dir / "index.html"
        self.assertTrue(self.html_path.exists(), "index.html does not exist in root directory!")
        with open(self.html_path, "r", encoding="utf-8") as f:
            self.html_content = f.read()

    def test_critical_dom_ids(self):
        """Assert that all interactive elements and metrics placeholders have their expected IDs."""
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
            
            # Audio wave indicator wrapper
            "audioWaveformWrapper"
        ]
        
        for element_id in required_ids:
            # Match either id="element_id" or id='element_id'
            pattern = rf'id=["\']{element_id}["\']'
            self.assertTrue(
                re.search(pattern, self.html_content) is not None,
                f"Missing critical DOM element with ID: '{element_id}' in index.html!"
            )

    def test_translation_dictionary_structure(self):
        """Verify that the translation configurations contain all required keys and match between ZH and EN."""
        # Extract I18N variable definition block using simple regex
        i18n_match = re.search(r'const I18N = (\{.*?\n\s*\};)', self.html_content, re.DOTALL)
        self.assertIsNotNone(i18n_match, "Could not locate I18N translation object inside index.html script!")
        
        i18n_block = i18n_match.group(1)
        
        # Verify both ZH and EN keys are present
        self.assertIn("zh:", i18n_block, "ZH translation language is missing in I18N dictionary!")
        self.assertIn("en:", i18n_block, "EN translation language is missing in I18N dictionary!")
        
        # Check presence of a few critical translation keys
        expected_keys = [
            "title", "subtitle", "simtime", "setBtn", "curtask", 
            "collected", "missing", "finaljson", "history", "resetBtn",
            "inputPlaceholder", "voiceBtn", "voiceStop", "sendBtn"
        ]
        
        for k in expected_keys:
            # Look for keys defined in either zh: or en: blocks
            self.assertIn(k, i18n_block, f"Required translation key '{k}' is missing in I18N dictionary!")

    def test_no_legacy_emojis_in_titles(self):
        """Assert that raw legacy emojis are removed from headers, and SVG icons are present instead."""
        forbidden_emojis = ["⏱️", "📋", "✅", "❓", "📄", "📜", "📡", "🌊", "🛠️"]
        
        # Check the translation keys do not contain legacy emojis
        i18n_match = re.search(r'const I18N = (\{.*?\n\s*\};)', self.html_content, re.DOTALL)
        if i18n_match:
            i18n_block = i18n_match.group(1)
            # Find specific headers inside ZH and EN
            header_keys = [
                "simtime", "curtask", "collected", "missing", "finaljson", "history"
            ]
            for key in header_keys:
                # Find occurrences of key: "value" or key: 'value'
                matches = re.findall(rf'{key}:\s*["\'](.*?)["\']', i18n_block)
                for val in matches:
                    for emoji in forbidden_emojis:
                        self.assertNotIn(
                            emoji, val,
                            f"Header translation key '{key}' value '{val}' still contains legacy emoji '{emoji}'!"
                        )

        # Verify SVGs are present in the DOM
        self.assertTrue("<svg" in self.html_content, "index.html is missing high-tech inline SVG tags!")
        self.assertTrue("tech-icon" in self.html_content, "index.html is missing the '.tech-icon' CSS class definition!")

    def test_html_tag_matching(self):
        """Basic check to ensure core structural tags match to prevent rendering issues."""
        self.assertTrue(self.html_content.strip().startswith("<!DOCTYPE html>"), "index.html must start with <!DOCTYPE html>")
        self.assertIn("</html>", self.html_content, "index.html is missing closing </html> tag")
        self.assertIn("</head>", self.html_content, "index.html is missing closing </head> tag")
        self.assertIn("</body>", self.html_content, "index.html is missing closing </body> tag")
        self.assertIn("</script>", self.html_content, "index.html is missing closing </script> tag")

if __name__ == "__main__":
    unittest.main()
