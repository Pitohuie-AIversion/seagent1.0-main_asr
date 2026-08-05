"""
tests/test_frontend_welcome_message.py - Targeted regression test for frontend welcome message (Issue #21).

Verifies:
1. Chinese mode titles and examples
2. English mode titles and examples
3. Knowledge Q&A data isolation semantics
4. Emergency mode semantics (rapid collection, key fields)
5. Absence of prohibited misdescriptions (emergency intervention, task stop/cancel)
6. Single source of truth in frontend/js/index.js (I18N.zh and I18N.en)
7. Init, reset, and session recovery reuse of I18N[currentLang].welcomeMsg
"""

import unittest
import re
from pathlib import Path

class TestFrontendWelcomeMessage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root_dir = Path(__file__).resolve().parent.parent
        cls.js_path = cls.root_dir / "frontend" / "js" / "index.js"
        cls.html_path = cls.root_dir / "index.html"

        cls.assertTrue(cls.js_path.exists(), f"{cls.js_path} does not exist!")
        with open(cls.js_path, "r", encoding="utf-8") as f:
            cls.js_content = f.read()

        if cls.html_path.exists():
            with open(cls.html_path, "r", encoding="utf-8") as f:
                cls.html_content = f.read()
        else:
            cls.html_content = ""

        # Extract I18N.zh.welcomeMsg and I18N.en.welcomeMsg from js_content
        cls.zh_welcome = cls._extract_welcome_msg(cls.js_content, lang="zh")
        cls.en_welcome = cls._extract_welcome_msg(cls.js_content, lang="en")

    @classmethod
    def _extract_welcome_msg(cls, content: str, lang: str) -> str:
        """Extract the exact welcomeMsg string for a given language block in index.js."""
        lang_pattern = lang + r':\s*\{([\s\S]*?)\n\s*\},?\n\s*(?:en:|FIELDS|\};)'
        lang_match = re.search(lang_pattern, content)
        if not lang_match:
            lang_match = re.search(lang + r':\s*\{([\s\S]*?)\n\s*\}\s*,\s*\n', content)

        cls.assertTrue(lang_match is not None, f"Could not find '{lang}' dictionary block in index.js")
        lang_block = lang_match.group(1)

        # Match welcomeMsg: `...` or '...' or "..."
        msg_match = re.search(r'welcomeMsg:\s*(`[\s\S]*?`|\x27[\s\S]*?\x27|"[\s\S]*?")', lang_block)
        cls.assertTrue(msg_match is not None, f"Could not find welcomeMsg in {lang} block!")
        raw_val = msg_match.group(1)
        # Strip quotes/backticks
        return raw_val[1:-1]

    def test_9_1_chinese_mode_titles(self):
        """9.1 Chinese mode titles test."""
        self.assertIn("【任务收集】", self.zh_welcome)
        self.assertIn("【知识问答】", self.zh_welcome)
        self.assertIn("【紧急模式】", self.zh_welcome)

    def test_9_2_english_mode_titles(self):
        """9.2 English mode titles test."""
        self.assertIn("[Task Collection]", self.en_welcome)
        self.assertIn("[Knowledge Q&A]", self.en_welcome)
        self.assertIn("[Emergency Mode]", self.en_welcome)

    def test_9_3_chinese_examples(self):
        """9.3 Chinese mode examples test."""
        self.assertIn("在流花11-1油田执行管缆巡检", self.zh_welcome)
        self.assertIn("金牛座一号机的最大作业水深是多少", self.zh_welcome)
        self.assertIn("紧急巡检PL-003管线泄漏", self.zh_welcome)

    def test_9_4_english_examples(self):
        """9.4 English mode examples test."""
        self.assertIn("Inspect the subsea pipeline at Liuhua 11-1 Oilfield", self.en_welcome)
        self.assertIn("What is the maximum operating depth of Taurus Unit 1", self.en_welcome)
        self.assertIn("Urgently inspect the PL-003 pipeline leak", self.en_welcome)

    def test_9_5_knowledge_qa_isolation_semantics(self):
        """9.5 Knowledge Q&A data isolation semantics test."""
        self.assertIn("不会写入或修改任务信息", self.zh_welcome)
        self.assertIn("without creating or modifying task data", self.en_welcome)

    def test_9_6_emergency_mode_semantics(self):
        """9.6 Emergency mode semantics test."""
        # Chinese emergency semantics
        self.assertIn("时间紧迫", self.zh_welcome)
        self.assertIn("关键字段", self.zh_welcome)
        self.assertIn("减少", self.zh_welcome)
        self.assertIn("确认步骤", self.zh_welcome)

        # English emergency semantics
        self.assertIn("time-critical", self.en_welcome)
        self.assertIn("key fields", self.en_welcome)
        self.assertIn("reduces", self.en_welcome)
        self.assertIn("confirmation steps", self.en_welcome)

    def test_9_7_prohibited_misdescriptions(self):
        """9.7 Prohibited misdescriptions test in welcomeMsg content."""
        forbidden_zh = [
            "紧急干预",
            "立即停止当前任务",
            "暂停当前任务",
            "终止当前任务",
            "取消当前任务",
        ]
        forbidden_en = [
            "emergency intervention",
            "stop the current task",
            "pause the current task",
            "abort the current task",
            "cancel the current task",
        ]

        for word in forbidden_zh:
            self.assertNotIn(
                word,
                self.zh_welcome,
                f"Prohibited Chinese phrase '{word}' found in welcomeMsg!"
            )

        for word in forbidden_en:
            self.assertNotIn(
                word,
                self.en_welcome.lower(),
                f"Prohibited English phrase '{word}' found in welcomeMsg!"
            )

    def test_9_8_unique_authoritative_source(self):
        """9.8 Single source of truth test."""
        # Check welcomeMsg is defined exactly twice in JS (once in zh, once in en)
        welcome_msg_defs = re.findall(r'welcomeMsg:\s*[`\x27"]', self.js_content)
        self.assertEqual(
            len(welcome_msg_defs),
            2,
            f"Expected welcomeMsg to be defined exactly twice in I18N, found {len(welcome_msg_defs)}"
        )

        # Confirm no duplicate full welcome text hardcoded elsewhere in JS or HTML
        self.assertNotIn("水下多智能体任务决策助手", self.html_content)
        self.assertNotIn("underwater multi-robot task decision assistant", self.html_content)

        # Check reset and restore consume I18N[currentLang].welcomeMsg
        self.assertIn("I18N[currentLang].welcomeMsg", self.js_content)

    def test_9_9_init_and_reset_reuse(self):
        """9.9 Initialization & restart reuse test."""
        # Verify restoreSessionFromStorage calls reset on failure
        self.assertIn("restoreSessionFromStorage().then(restored => {", self.js_content)
        self.assertIn("if (!restored) reset();", self.js_content)

        # Verify reset clears message container and inserts welcomeMsg
        reset_match = re.search(r'async function reset\(\)\s*\{([\s\S]*?)\n\s*\}\n\s*sendBtn\.addEventListener', self.js_content)
        self.assertIsNotNone(reset_match, "Could not find reset function definition!")
        reset_body = reset_match.group(1)

        self.assertIn("messageContainer.innerHTML = '';", reset_body)
        self.assertIn("addWelcomeMessage();", reset_body)

    def test_language_switch_uses_i18n_welcome_message(self):
        """Verify updateLanguage directly updates welcome message using I18N[currentLang].welcomeMsg."""
        self.assertIn('document.querySelector(\'.message[data-message-kind="welcome"]\')', self.js_content)
        self.assertIn('welcomeContent = I18N[currentLang].welcomeMsg', self.js_content)

    def test_welcome_message_skips_generic_translation(self):
        """Verify generic translation loop skips messages marked with dataset.messageKind === 'welcome'."""
        self.assertIn("if (msgDiv.dataset.messageKind === 'welcome')", self.js_content)
        self.assertIn("if (options.kind !== 'welcome' && currentLang === 'en' && hasChinese(content))", self.js_content)

    def test_welcome_message_has_explicit_kind(self):
        """Verify addWelcomeMessage assigns kind: 'welcome' to welcome messages."""
        self.assertIn("function addWelcomeMessage() {", self.js_content)
        self.assertIn("addMessage('bot', I18N[currentLang].welcomeMsg, { kind: 'welcome' })", self.js_content)

if __name__ == "__main__":
    unittest.main()
