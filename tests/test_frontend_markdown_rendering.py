"""Regression contracts for safe Markdown rendering in the dialogue frontend."""

import hashlib
import re
import unittest
from pathlib import Path


class TestFrontendMarkdownRendering(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).resolve().parent.parent
        cls.html = (cls.root / "frontend" / "index.html").read_text(encoding="utf-8")
        cls.index_js = (cls.root / "frontend" / "js" / "index.js").read_text(encoding="utf-8")
        cls.renderer_js = (
            cls.root / "frontend" / "js" / "markdown-renderer.js"
        ).read_text(encoding="utf-8")
        cls.css = (cls.root / "frontend" / "css" / "index.css").read_text(encoding="utf-8")

    def test_local_dependencies_exist_with_pinned_hashes_and_licenses(self):
        expected_hashes = {
            "frontend/vendor/marked/marked.umd.js":
                "ba65f1c8948e6b01321399800843e9048b31e1c197652d4b0fafae840b30e32b",
            "frontend/vendor/dompurify/purify.min.js":
                "9ab3d44d73c3e3947f9ab72e0f0bc15c7f1931d60b365ba261fc85fe59013c56",
        }
        for relative_path, expected_hash in expected_hashes.items():
            content = (self.root / relative_path).read_bytes()
            self.assertEqual(hashlib.sha256(content).hexdigest(), expected_hash)

        self.assertTrue((self.root / "frontend/vendor/marked/LICENSE").is_file())
        self.assertTrue((self.root / "frontend/vendor/dompurify/LICENSE").is_file())
        self.assertTrue((self.root / "frontend/vendor/dompurify/LICENSE-MPL").is_file())

    def test_dependency_and_adapter_load_order_is_deterministic(self):
        script_sources = re.findall(r'<script\s+src="([^"]+)"\s+defer></script>', self.html)
        expected_order = [
            "static/vendor/marked/marked.umd.js",
            "static/vendor/dompurify/purify.min.js",
            "static/js/markdown-renderer.js",
            "static/js/index.js",
        ]
        self.assertEqual(script_sources[-4:], expected_order)

    def test_assistant_pipeline_parses_then_sanitizes(self):
        parse_position = self.renderer_js.index("global.marked.parse")
        sanitize_position = self.renderer_js.index("global.DOMPurify.sanitize")
        self.assertLess(parse_position, sanitize_position)
        self.assertIn("gfm: true", self.renderer_js)
        self.assertIn("breaks: true", self.renderer_js)
        self.assertIn("ASSISTANT_ROLES.has", self.renderer_js)

    def test_security_policy_is_fail_closed(self):
        required_policy = [
            "ALLOWED_TAGS",
            "ALLOWED_ATTR: ALLOWED_ATTRIBUTES",
            "ALLOWED_URI_REGEXP: SAFE_LINK_PATTERN",
            "ALLOW_ARIA_ATTR: false",
            "ALLOW_DATA_ATTR: false",
            "ALLOW_UNKNOWN_PROTOCOLS: false",
            "FORBID_TAGS",
            "FORBID_ATTR",
        ]
        for fragment in required_policy:
            self.assertIn(fragment, self.renderer_js)

        for forbidden in ("script", "iframe", "object", "embed", "img", "svg", "math"):
            self.assertRegex(self.renderer_js, rf"['\"]{forbidden}['\"]")

        self.assertIn("renderer.html", self.renderer_js)
        self.assertIn("renderer.image", self.renderer_js)
        self.assertIn("return renderPlainText(source);", self.renderer_js)

    def test_link_policy_allows_expected_protocols_without_unknown_protocols(self):
        self.assertIn(
            r"SAFE_LINK_PATTERN = /^(?:(?:https?|mailto):[^\s]*|(?:[#/?]|\.\.?\/)[^\s]*)$/i",
            self.renderer_js,
        )
        self.assertNotIn("ALLOW_UNKNOWN_PROTOCOLS: true", self.renderer_js)

    def test_user_content_bypasses_markdown_parser(self):
        role_gate = re.search(
            r"const render = \(value, role\) => \{([\s\S]*?)\n  \};",
            self.renderer_js,
        )
        self.assertIsNotNone(role_gate)
        self.assertIn("return renderPlainText(value);", role_gate.group(1))
        self.assertIn("renderMessageContent(content, role)", self.index_js)
        self.assertIn("div.setAttribute('data-role', role)", self.index_js)

    def test_all_message_repaints_use_role_aware_renderer(self):
        self.assertNotIn("function renderMarkdown", self.index_js)
        self.assertNotIn("renderMarkdown(", self.index_js)
        self.assertEqual(self.index_js.count("renderMessageContent("), 7)
        self.assertIn(
            "renderMessageContent(originalText, msgDiv.getAttribute('data-role'))",
            self.index_js,
        )
        self.assertIn(
            "renderMessageContent(translated, messageDiv.getAttribute('data-role'))",
            self.index_js,
        )

    def test_markdown_elements_have_scoped_chat_styles(self):
        for selector in (" h1", " blockquote", " pre", " code", " a", " table", " th"):
            self.assertIn(f".message.bot .bubble{selector}", self.css)

    def test_html_renderer_allows_br_tags_while_escaping_other_html(self):
        self.assertRegex(self.renderer_js, r"\/\^<br\\s\*\\\/\?>\$\/i\.test\(trimmed\)")
        self.assertIn("return '<br>';", self.renderer_js)
        self.assertIn("return escapeHtml(text);", self.renderer_js)


if __name__ == "__main__":
    unittest.main()
