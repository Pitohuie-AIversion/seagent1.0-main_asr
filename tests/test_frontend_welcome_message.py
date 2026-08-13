"""
tests/test_frontend_welcome_message.py - Targeted regression test for frontend welcome message (Issue #21).

Verifies:
1. Chinese capability titles and examples
2. English capability titles and examples
3. Knowledge Q&A data isolation semantics
4. Task creation and admission semantics
5. Complete absence of emergency-mode content
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
        cls.html_path = cls.root_dir / "frontend" / "index.html"

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
        """9.1 Chinese capability titles test."""
        self.assertIn("当前支持以下两项核心能力", self.zh_welcome)
        self.assertIn("【知识问答】", self.zh_welcome)
        self.assertIn("【任务创建与准入】", self.zh_welcome)
        self.assertNotIn("【任务收集】", self.zh_welcome)

    def test_9_2_english_mode_titles(self):
        """9.2 English capability titles test."""
        self.assertIn("support two core capabilities", self.en_welcome)
        self.assertIn("[Knowledge Q&A]", self.en_welcome)
        self.assertIn("[Task Creation & Admission]", self.en_welcome)
        self.assertNotIn("[Task Collection]", self.en_welcome)

    def test_9_3_chinese_examples(self):
        """9.3 Chinese capability examples test."""
        self.assertIn("金牛座一号机的最大作业水深是多少", self.zh_welcome)
        self.assertIn("在流花11-1油田执行管缆巡检", self.zh_welcome)

    def test_9_4_english_examples(self):
        """9.4 English capability examples test."""
        self.assertIn("What is the maximum operating depth of Taurus Unit 1", self.en_welcome)
        self.assertIn("Inspect the subsea pipeline at Liuhua 11-1 Oilfield", self.en_welcome)

    def test_9_5_knowledge_qa_isolation_semantics(self):
        """9.5 Knowledge Q&A data isolation semantics test."""
        self.assertIn("不会写入或修改任务信息", self.zh_welcome)
        self.assertIn("without creating or modifying task data", self.en_welcome)

    def test_9_6_task_creation_and_admission_semantics(self):
        """9.6 Task creation and admission semantics test."""
        self.assertIn("收集", self.zh_welcome)
        self.assertIn("约束检查", self.zh_welcome)
        self.assertIn("通过准入", self.zh_welcome)
        self.assertIn("确认并发布任务", self.zh_welcome)
        self.assertIn("Collects task information", self.en_welcome)
        self.assertIn("constraint checks", self.en_welcome)
        self.assertIn("admission is approved", self.en_welcome)
        self.assertIn("confirm and publish the task", self.en_welcome)

    def test_9_7_emergency_mode_content_is_absent(self):
        """9.7 Welcome message must not describe an emergency mode."""
        forbidden_zh = [
            "紧急",
            "紧急干预",
            "立即停止当前任务",
            "暂停当前任务",
            "终止当前任务",
            "取消当前任务",
        ]
        forbidden_en = [
            "emergency",
            "time-critical",
            "urgently",
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

    def test_reset_cancels_all_active_request_types(self):
        reset_match = re.search(r'async function reset\(\)\s*\{([\s\S]*?)\n\s*\}\n\s*sendBtn\.addEventListener', self.js_content)
        self.assertIsNotNone(reset_match, "Could not find reset function definition!")
        reset_body = reset_match.group(1)

        self.assertIn("cancelActiveRequest();", reset_body)
        self.assertIn("await cancelVoiceActivity();", reset_body)
        self.assertIn("applyInteractionState(RESET_ACTIONS, false);", reset_body)

    def test_asr_upload_is_abortable_and_generation_guarded(self):
        self.assertIn("let asrAbortController = null;", self.js_content)
        self.assertRegex(
            self.js_content,
            r"fetch\(API_BASE \+ '/api/asr',[\s\S]*?signal:\s*signal",
        )
        self.assertIn("if (asrGeneration !== sessionGeneration) return;", self.js_content)

    def test_voice_cancellation_releases_browser_resources(self):
        cleanup_match = re.search(
            r'async function cancelVoiceActivity\(\)\s*\{([\s\S]*?)\n\s*async function startVoiceRecording',
            self.js_content,
        )
        self.assertIsNotNone(cleanup_match, "Could not find cancelVoiceActivity function!")
        cleanup_body = cleanup_match.group(1)

        self.assertIn("asrAbortController.abort()", cleanup_body)
        self.assertIn("releaseVoiceRecordingResources()", cleanup_body)
        self.assertIn("recordedChunks = [];", cleanup_body)
        self.assertIn("audioWaveformWrapper", cleanup_body)

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


class TestEmergencyModeControl(unittest.TestCase):
    def test_emergency_mode_state_commit_and_reset(self):
        from src.dialogue_manager import DialogueManager
        from src.knowledge_retriever import KnowledgeBase
        from tests.test_p0_security_final_closeout import DummyLLM

        dm = DialogueManager(DummyLLM(), KnowledgeBase())
        self.assertEqual(dm.mode, "normal")

        # Set emergency_mode = True
        dm._apply_updates_in_transaction({"emergency_mode": True}, dm.slot_store.slots, allow_overwrite=True)
        self.assertEqual(dm.mode, "emergency")
        self.assertTrue(dm.slot_store.slots["emergency_mode"].value)

        # Reset emergency_mode = False
        dm._apply_updates_in_transaction({"emergency_mode": False}, dm.slot_store.slots, allow_overwrite=True)
        self.assertEqual(dm.mode, "normal")
        self.assertFalse(dm.slot_store.slots["emergency_mode"].value)
