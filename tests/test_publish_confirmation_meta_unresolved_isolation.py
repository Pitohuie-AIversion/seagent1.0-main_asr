import unittest
from src.dialogue_manager import DialogueManager
from src.extraction.extractor import ParameterExtractor


class TestPublishConfirmationAndMetaUnresolvedIsolation(unittest.TestCase):
    def test_final_publish_confirmation_positive(self):
        """测试确认发布正向用例，支持工程自然语言变体。"""
        valid_cmds = [
            "确认发布",
            "确认并发布",
            "确认发布任务",
            "确认发布当前任务",
            "确认发布该任务",
            "确认最终发布",
            "确认最终发布。",
            "最终确认发布",
            "确认正式发布",
            "正式确认发布",
            "确认立即发布",
            "立即确认发布",
            "最终发布",
            "正式发布",
            "发布任务",
            "发布当前任务",
            "发布",
            "立即发布",
            "现在发布",
            "直接发布",
            "确认提交",
            "确认并提交",
            "确认提交任务",
            "确认最终提交",
            "最终确认提交",
            "最终提交",
            "提交任务",
            "确认下发",
            "确认下发任务",
            "最终下发",
            "下发任务",
        ]
        for cmd in valid_cmds:
            with self.subTest(cmd=cmd):
                self.assertTrue(
                    DialogueManager._is_final_publish_confirmation(cmd),
                    f"'{cmd}' 应该被识别为确认发布指令",
                )

    def test_final_publish_confirmation_negative(self):
        """测试否定词与复合指令不得误识别为确认发布。"""
        invalid_cmds = [
            "不发布",
            "不要发布",
            "别发布",
            "取消发布",
            "暂不发布",
            "拒绝发布",
            "暂缓发布",
            "先不发布",
            "不提交",
            "不要提交",
            "先不提交",
            "好的",
            "可以",
            "确认",
            "确认无误",
            "修改水深为300米然后确认发布",
            "如果可以的话就发布",
        ]
        for cmd in invalid_cmds:
            with self.subTest(cmd=cmd):
                self.assertFalse(
                    DialogueManager._is_final_publish_confirmation(cmd),
                    f"'{cmd}' 不应被识别为纯粹的最终确认发布指令",
                )

    def test_confirmation_only(self):
        """测试泛确认与否定词识别。"""
        pos = ["确认", "确认无误", "确定", "好的", "可以", "没问题", "ok", "继续", "认可"]
        neg = ["不确认", "不要确认", "先不确认", "不确定", "不行", "不对"]
        for p in pos:
            with self.subTest(cmd=p):
                self.assertTrue(DialogueManager._is_confirmation_only(p))
        for n in neg:
            with self.subTest(cmd=n):
                self.assertFalse(DialogueManager._is_confirmation_only(n))

    def test_filter_meta_unresolved_rules(self):
        """测试模型元解释清洗机制：剔除模型反思文本，保留真实业务异常。"""
        mock_raw_unresolved = [
            "用户本轮输入为确认发布指令，未包含任何参数修改、补充或新增字段，根据规则 6，slot_candidates 应为空列表。",
            "根据规则 2，无法识别到任何有效变更",
            "根据第3条，未包含任何槽位更新",
            "slot_candidates 应为空",
            "未包含任何参数修改",
            "未提供任何槽位修改",
            "未提取到任何任务字段",
            "本轮输入为确认发布指令",
            "用户本轮输入为确认",
            "无需更新",
            # 真实业务异常必须保留
            "终点水深超出 4000 米作业极限",
            "未知设备型号：深海蛟龙X",
            "同轮具体任务类型互相冲突，请只指定一种任务操作。",
            "管缆埋设深度 1.5 米与当前模板不兼容，未填入 water_depth",
        ]

        cleaned = ParameterExtractor._filter_meta_unresolved(mock_raw_unresolved)
        expected = [
            "终点水深超出 4000 米作业极限",
            "未知设备型号：深海蛟龙X",
            "同轮具体任务类型互相冲突，请只指定一种任务操作。",
            "管缆埋设深度 1.5 米与当前模板不兼容，未填入 water_depth",
        ]
        self.assertEqual(cleaned, expected)


if __name__ == "__main__":
    unittest.main()
