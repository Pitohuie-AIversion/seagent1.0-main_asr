import unittest
from unittest.mock import MagicMock
from src.llm_client import LLMClient


class TestFilterReplyRootCauseFix(unittest.TestCase):
    """验证根除脱敏过滤误杀机器人型号与‘无法透露模型底座’的逻辑。"""

    def setUp(self):
        self.client = LLMClient(None, None)
        self.client.llm = MagicMock()  # 使 is_mock 为 False，从而能够测试 filter_reply 内部逻辑

    def test_robot_equipment_update_confirmation_not_corrupted(self):
        """验证机器人更新确认回复（包含“作业机器人更新为无法透露...”误杀场景）不会被替换为脱敏词。"""
        raw_reply = (
            "收到，已为您将作业机器人更新为轻型工作级深海机器人（系统内部型号对应您提到的“天鹰座”）。"
            "该设备专为高频次、快响应的水下勘察与轻量化干预任务设计。"
        )
        # 模拟 LLM 误杀输出了拒绝文案
        self.client.generate_text = MagicMock(
            return_value="收到，已为您将作业机器人更新为无法透露底座模型或实现细节（系统内部型号对应您提到的“天鹰座”）。"
        )
        filtered = self.client.filter_reply(raw_reply)
        # 必须触发预检熔断，还原为 raw_reply
        self.assertEqual(filtered, raw_reply)
        self.assertNotIn("无法透露底座模型", filtered)
        self.assertIn("轻型工作级深海机器人", filtered)

    def test_domain_terms_restoration_for_robot_models(self):
        """验证若回复中出现水下机器人模型（如天鹰座、系统内部型号），不会被二次过滤篡改。"""
        raw_reply = "系统内部型号为【天鹰座001】，作业机器人为轻型工作级深海机器人。"
        self.client.generate_text = MagicMock(
            return_value="系统内部型号为【无法透露底座模型或实现细节】，作业机器人为无法透露底座模型或实现细节。"
        )
        filtered = self.client.filter_reply(raw_reply)
        self.assertEqual(filtered, raw_reply)

    def test_real_sensitive_ai_leak_is_filtered(self):
        """验证包含真实 AI 底座模型（如 Qwen-72B、System Prompt）的泄露依然能够被正确拦截并脱敏。"""
        raw_reply = "我们的底层 LLM 底座大模型是 Qwen-72B，它的 System Prompt 设置了系统路由逻辑。"
        self.client.generate_text = MagicMock(
            return_value="我们的底层我无法透露底座模型或实现细节，它的我无法透露底座模型或实现细节。"
        )
        filtered = self.client.filter_reply(raw_reply)
        self.assertIn("无法透露底座模型", filtered)
        self.assertNotIn("Qwen-72B", filtered)

    def test_normal_dialogue_without_ai_leak_remains_intact(self):
        """验证普通水下业务对话在 LLM 生成正常文本时，脱敏过滤保持原样输出。"""
        raw_reply = "好的，已将作业水深设置为 1500 米，起始坐标为 (18.5, 110.2)。"
        self.client.generate_text = MagicMock(return_value=raw_reply)
        filtered = self.client.filter_reply(raw_reply)
        self.assertEqual(filtered, raw_reply)


if __name__ == "__main__":
    unittest.main()
