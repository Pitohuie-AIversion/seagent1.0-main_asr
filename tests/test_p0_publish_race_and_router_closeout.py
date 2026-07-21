"""
tests/test_p0_publish_race_and_router_closeout.py - P0 发布竞态与 P1 路由收口测试套件

A. Staging Race Condition Tests (1-12)
B. Routing & Intent Classification Tests (13-24)
"""

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from src.dialogue_manager import DialogueManager
from src.intent_router import IntentRouter
from src.knowledge_retriever import KnowledgeBase
from src.llm_client import LLMClient
from src.task_intent_builder import TaskIntentBuilder
from src.exceptions import TaskPersistenceError
from tests.test_slot_consistency import seed_complete_valid_pipeline_task


class DummyLLM(LLMClient):
    def __init__(self, default_reply="默认LLM测试回复"):
        self.llm = None
        self.default_reply = default_reply

    def chat(self, messages, temperature=0.7, max_tokens=800):
        return self.default_reply

    def generate(self, messages, temperature=0.7, max_tokens=800):
        return self.chat(messages, temperature, max_tokens)

    def filter_reply(self, text):
        return text


# ─────────────────────────────────────────────────────────────────────────────
# 测试 A: staging 竞态与安全发布测试 (1-12)
# ─────────────────────────────────────────────────────────────────────────────

class StagingRaceConditionTest(unittest.TestCase):
    def setUp(self):
        self.kb = KnowledgeBase()
        self.builder = TaskIntentBuilder(self.kb)

    def _make_valid_intent(self, intent_id="TI2026072101"):
        return {
            "intent_id": intent_id,
            "task_type": "pipeline_inspection",
            "priority": 7,
            "time": {"start": None, "end": None},
            "location": {"oilfield": None, "water_depth_m": 300.0},
            "task": {"type": "pipeline_inspection", "details": {}},
            "equipment": {"robot_type": "observation_rov", "payload": [], "support_vessel": {"name": None}},
            "conditions": {}
        }

    def test_b1_race_replace_staging_before_link_raises_error(self):
        """1. 在认领/发布前将 staging 路径替换为伪造文件 → publish_staging 抛出 TaskPersistenceError"""
        intent = self._make_valid_intent("TI2026072101")
        forged_intent = copy.deepcopy(intent)
        forged_intent["priority"] = 99

        with tempfile.TemporaryDirectory() as tmp_task_dir_str:
            task_dir = Path(tmp_task_dir_str)
            staging_file = task_dir / "task_intent_TI2026072101.staging_1234_5678_abcd1234"
            with open(staging_file, "w", encoding="utf-8") as f:
                json.dump(intent, f)

            original_stat = os.stat

            def fake_stat_on_claim(path, *args, **kwargs):
                if ".claimed_" in str(path):
                    st = original_stat(path, *args, **kwargs)
                    return os.stat_result((
                        st.st_mode, st.st_ino + 9999, st.st_dev, st.st_nlink,
                        st.st_uid, st.st_gid, st.st_size, st.st_atime, st.st_mtime, st.st_ctime
                    ))
                return original_stat(path, *args, **kwargs)

            with patch("src.task_intent_builder.get_task_dir", return_value=task_dir), \
                 patch("os.stat", side_effect=fake_stat_on_claim):
                with self.assertRaises(TaskPersistenceError):
                    self.builder.publish_staging(staging_file, intent)

    def test_b2_race_replace_staging_final_file_rolled_back(self):
        """2. 发生竞态替换时，final_file 不存在（或已被安全回滚删除）"""
        intent = self._make_valid_intent("TI2026072101")
        forged_intent = copy.deepcopy(intent)
        forged_intent["priority"] = 99

        with tempfile.TemporaryDirectory() as tmp_task_dir_str:
            task_dir = Path(tmp_task_dir_str)
            staging_file = task_dir / "task_intent_TI2026072101.staging_1234_5678_abcd1234"
            with open(staging_file, "w", encoding="utf-8") as f:
                json.dump(intent, f)

            original_stat = os.stat

            def fake_stat_on_claim(path, *args, **kwargs):
                if ".claimed_" in str(path):
                    st = original_stat(path, *args, **kwargs)
                    return os.stat_result((
                        st.st_mode, st.st_ino + 9999, st.st_dev, st.st_nlink,
                        st.st_uid, st.st_gid, st.st_size, st.st_atime, st.st_mtime, st.st_ctime
                    ))
                return original_stat(path, *args, **kwargs)

            with patch("src.task_intent_builder.get_task_dir", return_value=task_dir), \
                 patch("os.stat", side_effect=fake_stat_on_claim):
                try:
                    self.builder.publish_staging(staging_file, intent)
                except TaskPersistenceError:
                    pass

            final_file = task_dir / "task_intent_TI2026072101.json"
            self.assertFalse(final_file.exists())

    def test_b3_race_replace_staging_file_preserved(self):
        """3. 发生竞态替换时，替换后的 staging 文件必须仍然存在于磁盘"""
        intent = self._make_valid_intent("TI2026072101")
        forged_intent = copy.deepcopy(intent)
        forged_intent["priority"] = 99

        with tempfile.TemporaryDirectory() as tmp_task_dir_str:
            task_dir = Path(tmp_task_dir_str)
            staging_file = task_dir / "task_intent_TI2026072101.staging_1234_5678_abcd1234"
            with open(staging_file, "w", encoding="utf-8") as f:
                json.dump(intent, f)

            real_rename = os.rename

            def race_replace_rename(src, dst):
                real_rename(src, dst)
                with open(staging_file, "w", encoding="utf-8") as f:
                    json.dump(forged_intent, f)

            with patch("src.task_intent_builder.get_task_dir", return_value=task_dir), \
                 patch("os.rename", side_effect=race_replace_rename):
                try:
                    self.builder.publish_staging(staging_file, intent)
                except TaskPersistenceError:
                    pass

            self.assertTrue(staging_file.exists())

    def test_b4_race_replace_staging_file_content_unchanged(self):
        """4. 发生竞态替换时，替换后的 staging 文件内容保持不变"""
        intent = self._make_valid_intent("TI2026072101")
        forged_intent = copy.deepcopy(intent)
        forged_intent["priority"] = 99

        with tempfile.TemporaryDirectory() as tmp_task_dir_str:
            task_dir = Path(tmp_task_dir_str)
            staging_file = task_dir / "task_intent_TI2026072101.staging_1234_5678_abcd1234"
            with open(staging_file, "w", encoding="utf-8") as f:
                json.dump(intent, f)

            real_rename = os.rename

            def race_replace_rename(src, dst):
                real_rename(src, dst)
                with open(staging_file, "w", encoding="utf-8") as f:
                    json.dump(forged_intent, f)

            with patch("src.task_intent_builder.get_task_dir", return_value=task_dir), \
                 patch("os.rename", side_effect=race_replace_rename):
                try:
                    self.builder.publish_staging(staging_file, intent)
                except TaskPersistenceError:
                    pass

            with open(staging_file, "r", encoding="utf-8") as f:
                content = json.load(f)
            self.assertEqual(content, forged_intent)

    def test_b5_os_link_wrap_intercept_forged_inode(self):
        """5. os.link 之前替换新 inode 文件 → 拦截且伪造 inode 绝对不会成为有效正式文件"""
        intent = self._make_valid_intent("TI2026072101")
        forged_intent = copy.deepcopy(intent)
        forged_intent["payload"] = "FORGED_INODE_PAYLOAD"

        with tempfile.TemporaryDirectory() as tmp_task_dir_str:
            task_dir = Path(tmp_task_dir_str)
            staging_file = task_dir / "task_intent_TI2026072101.staging_1234_5678_abcd1234"
            with open(staging_file, "w", encoding="utf-8") as f:
                json.dump(intent, f)

            original_stat = os.stat

            def fake_stat_on_claim(path, *args, **kwargs):
                if ".claimed_" in str(path):
                    st = original_stat(path, *args, **kwargs)
                    return os.stat_result((
                        st.st_mode, st.st_ino + 9999, st.st_dev, st.st_nlink,
                        st.st_uid, st.st_gid, st.st_size, st.st_atime, st.st_mtime, st.st_ctime
                    ))
                return original_stat(path, *args, **kwargs)

            with patch("src.task_intent_builder.get_task_dir", return_value=task_dir), \
                 patch("os.stat", side_effect=fake_stat_on_claim):
                with self.assertRaises(TaskPersistenceError):
                    self.builder.publish_staging(staging_file, intent)

            final_file = task_dir / "task_intent_TI2026072101.json"
            self.assertFalse(final_file.exists())

    def test_b6_post_link_inode_mismatch_rejected_and_rolled_back(self):
        """6. 认领后 inode 不一致 → 拒绝并回滚删除 final_file"""
        intent = self._make_valid_intent("TI2026072101")
        with tempfile.TemporaryDirectory() as tmp_task_dir_str:
            task_dir = Path(tmp_task_dir_str)
            staging_file = task_dir / "task_intent_TI2026072101.staging_1234_5678_abcd1234"
            with open(staging_file, "w", encoding="utf-8") as f:
                json.dump(intent, f)

            original_stat = os.stat

            def fake_stat_on_claim(path, *args, **kwargs):
                if ".claimed_" in str(path):
                    st = original_stat(path, *args, **kwargs)
                    return os.stat_result((
                        st.st_mode, st.st_ino + 9999, st.st_dev, st.st_nlink,
                        st.st_uid, st.st_gid, st.st_size, st.st_atime, st.st_mtime, st.st_ctime
                    ))
                return original_stat(path, *args, **kwargs)

            with patch("src.task_intent_builder.get_task_dir", return_value=task_dir), \
                 patch("os.stat", side_effect=fake_stat_on_claim):
                with self.assertRaises(TaskPersistenceError):
                    self.builder.publish_staging(staging_file, intent)

            final_file = task_dir / "task_intent_TI2026072101.json"
            self.assertFalse(final_file.exists())

    def test_b7_post_link_content_mismatch_rejected_and_rolled_back(self):
        """7. 提交后 content 不一致 → 拒绝并回滚删除 final_file"""
        intent = self._make_valid_intent("TI2026072101")
        with tempfile.TemporaryDirectory() as tmp_task_dir_str:
            task_dir = Path(tmp_task_dir_str)
            staging_file = task_dir / "task_intent_TI2026072101.staging_1234_5678_abcd1234"
            with open(staging_file, "w", encoding="utf-8") as f:
                json.dump(intent, f)

            with patch("src.task_intent_builder.get_task_dir", return_value=task_dir), \
                 patch("src.task_intent_builder._atomic_commit_noreplace", side_effect=TaskPersistenceError("Mock commit failed")):
                with self.assertRaises(TaskPersistenceError):
                    self.builder.publish_staging(staging_file, intent)

            final_file = task_dir / "task_intent_TI2026072101.json"
            self.assertFalse(final_file.exists())

    def test_b8_preexisting_final_file_not_overwritten(self):
        """8. final_file 预先存在 → 不得覆盖，原内容与预存在 final_file 保持不变"""
        intent = self._make_valid_intent("TI2026072101")
        preexisting_intent = copy.deepcopy(intent)
        preexisting_intent["priority"] = 5

        with tempfile.TemporaryDirectory() as tmp_task_dir_str:
            task_dir = Path(tmp_task_dir_str)
            final_file = task_dir / "task_intent_TI2026072101.json"
            with open(final_file, "w", encoding="utf-8") as f:
                json.dump(preexisting_intent, f)

            staging_file = task_dir / "task_intent_TI2026072101.staging_1234_5678_abcd1234"
            with open(staging_file, "w", encoding="utf-8") as f:
                json.dump(intent, f)

            with patch("src.task_intent_builder.get_task_dir", return_value=task_dir):
                from src.exceptions import IntentIdConflict
                with self.assertRaises((TaskPersistenceError, IntentIdConflict)):
                    self.builder.publish_staging(staging_file, intent)

            with open(final_file, "r", encoding="utf-8") as f:
                final_content = json.load(f)
            self.assertEqual(final_content, preexisting_intent)

    def test_b9_normal_staging_publish_success(self):
        """9. staging 未被替换的正常流程 → 发布成功，final JSON == intent，staging 被删除"""
        intent = self._make_valid_intent("TI2026072101")
        with tempfile.TemporaryDirectory() as tmp_task_dir_str:
            task_dir = Path(tmp_task_dir_str)
            with patch("src.task_intent_builder.get_task_dir", return_value=task_dir):
                staging_file = self.builder.create_staging(intent)
                pub_name = self.builder.publish_staging(staging_file, intent)
                final_file = task_dir / pub_name

                self.assertTrue(final_file.exists())
                self.assertFalse(staging_file.exists())

                with open(final_file, "r", encoding="utf-8") as f:
                    final_content = json.load(f)
                self.assertEqual(final_content, intent)

    def test_b10_memory_trusted_publishing_safeguards(self):
        """10. 模拟在 publish 过程中 staging 路径被替换 → final JSON 仍只能等于已验证 intent"""
        intent = self._make_valid_intent("TI2026072101")
        forged_intent = copy.deepcopy(intent)
        forged_intent["priority"] = 888

        with tempfile.TemporaryDirectory() as tmp_task_dir_str:
            task_dir = Path(tmp_task_dir_str)
            staging_file = task_dir / "task_intent_TI2026072101.staging_1234_5678_abcd1234"
            with open(staging_file, "w", encoding="utf-8") as f:
                json.dump(intent, f)

            real_link = os.link

            def race_link(src, dst):
                with open(staging_file, "w", encoding="utf-8") as f:
                    json.dump(forged_intent, f)
                real_link(src, dst)

            with patch("src.task_intent_builder.get_task_dir", return_value=task_dir), \
                 patch("os.link", side_effect=race_link):
                try:
                    self.builder.publish_staging(staging_file, intent)
                except TaskPersistenceError:
                    pass

            final_file = task_dir / "task_intent_TI2026072101.json"
            if final_file.exists():
                with open(final_file, "r", encoding="utf-8") as f:
                    fc = json.load(f)
                self.assertEqual(fc, intent)

    def test_b11_replacement_staging_not_deleted_on_cleanup(self):
        """11. 清理阶段前 staging 路径被替换 → 绝不得删除替代文件"""
        intent = self._make_valid_intent("TI2026072101")
        forged_intent = copy.deepcopy(intent)
        forged_intent["priority"] = 99

        with tempfile.TemporaryDirectory() as tmp_task_dir_str:
            task_dir = Path(tmp_task_dir_str)
            staging_file = task_dir / "task_intent_TI2026072101.staging_1234_5678_abcd1234"
            with open(staging_file, "w", encoding="utf-8") as f:
                json.dump(intent, f)

            real_rename = os.rename

            def race_replace_rename(src, dst):
                real_rename(src, dst)
                with open(staging_file, "w", encoding="utf-8") as f:
                    json.dump(forged_intent, f)

            with patch("src.task_intent_builder.get_task_dir", return_value=task_dir), \
                 patch("os.rename", side_effect=race_replace_rename):
                try:
                    self.builder.publish_staging(staging_file, intent)
                except TaskPersistenceError:
                    pass

            self.assertTrue(staging_file.exists())

    def test_b12_fail_closed_error_wrapping(self):
        """12. 所有异常与失败路径统一包装抛出 TaskPersistenceError，不泄漏不受控底层异常"""
        intent = self._make_valid_intent("TI2026072101")
        with tempfile.TemporaryDirectory() as tmp_task_dir_str:
            task_dir = Path(tmp_task_dir_str)
            staging_file = task_dir / "task_intent_TI2026072101.staging_1234_5678_abcd1234"
            with open(staging_file, "w", encoding="utf-8") as f:
                json.dump(intent, f)

            with patch("src.task_intent_builder.get_task_dir", return_value=task_dir), \
                 patch("os.link", side_effect=PermissionError("Mock disk error")):
                with self.assertRaises(TaskPersistenceError):
                    self.builder.publish_staging(staging_file, intent)


# ─────────────────────────────────────────────────────────────────────────────
# 测试 B: 路由与意图分类收口测试 (13-24)
# ─────────────────────────────────────────────────────────────────────────────

class IntentRouterCloseoutTest(unittest.TestCase):
    def setUp(self):
        self.kb = KnowledgeBase()
        self.llm = DummyLLM()
        self.dm = DialogueManager(self.llm, self.kb)

    def test_r13_jinniuzuo_why_cannot_work_at_500m(self):
        """13. '金牛座一号机为什么不能在500米作业？' → DEVICE_CAPABILITY"""
        res = self.dm.intent_router.route("金牛座一号机为什么不能在500米作业？", [], {})
        self.assertEqual(res.intent, "DEVICE_CAPABILITY")
        self.assertFalse(res.should_update_slots)

    def test_r14_jinniuzuo_why_depth_is_500m(self):
        """14. '金牛座一号机的作业水深为什么是500米？' → DEVICE_CAPABILITY"""
        res = self.dm.intent_router.route("金牛座一号机的作业水深为什么是500米？", [], {})
        self.assertEqual(res.intent, "DEVICE_CAPABILITY")
        self.assertFalse(res.should_update_slots)

    def test_r15_do_you_think_jinniuzuo_can_work_at_500m(self):
        """15. '你认为金牛座一号机能在500米作业吗？' → DEVICE_CAPABILITY"""
        res = self.dm.intent_router.route("你认为金牛座一号机能在500米作业吗？", [], {})
        self.assertEqual(res.intent, "DEVICE_CAPABILITY")
        self.assertFalse(res.should_update_slots)

    def test_r16_change_depth_to_500m(self):
        """16. '把作业水深改为500米' → TASK_UPDATE"""
        seed_complete_valid_pipeline_task(self.dm, self.kb)
        res = self.dm.intent_router.route("把作业水深改为500米", [], self.dm.task_state, phase=self.dm.phase)
        self.assertEqual(res.intent, "TASK_UPDATE")
        self.assertTrue(res.should_update_slots)

    def test_r17_adjust_max_depth_to_500m(self):
        """17. '将最大深度调整到500米' → TASK_UPDATE"""
        seed_complete_valid_pipeline_task(self.dm, self.kb)
        res = self.dm.intent_router.route("将最大深度调整到500米", [], self.dm.task_state, phase=self.dm.phase)
        self.assertEqual(res.intent, "TASK_UPDATE")
        self.assertTrue(res.should_update_slots)

    def test_r18_change_from_500m_to_800m(self):
        """18. '从500米改到800米' → TASK_UPDATE"""
        seed_complete_valid_pipeline_task(self.dm, self.kb)
        res = self.dm.intent_router.route("从500米改到800米", [], self.dm.task_state, phase=self.dm.phase)
        self.assertEqual(res.intent, "TASK_UPDATE")
        self.assertTrue(res.should_update_slots)

    def test_r19_set_operation_depth_to_500m(self):
        """19. '设置作业深度为500米' → TASK_UPDATE"""
        seed_complete_valid_pipeline_task(self.dm, self.kb)
        res = self.dm.intent_router.route("设置作业深度为500米", [], self.dm.task_state, phase=self.dm.phase)
        self.assertEqual(res.intent, "TASK_UPDATE")
        self.assertTrue(res.should_update_slots)

    def test_r20_device_capability_query_slot_store_unchanged(self):
        """20. 能力查询前后 SlotStore 完整快照完全相等"""
        seed_complete_valid_pipeline_task(self.dm, self.kb)
        snap_before = copy.deepcopy(self.dm.slot_store.export_snapshot())

        reply = self.dm.process("金牛座一号机为什么不能在500米作业？")

        snap_after = copy.deepcopy(self.dm.slot_store.export_snapshot())
        self.assertEqual(snap_before, snap_after)

    def test_r21_device_capability_query_does_not_call_extractor(self):
        """21. 能力查询不得调用 extractor"""
        seed_complete_valid_pipeline_task(self.dm, self.kb)
        with patch.object(self.dm.extractor, "extract_updates") as mock_ext:
            reply = self.dm.process("金牛座一号机的作业水深为什么是500米？")
            mock_ext.assert_not_called()

    def test_r22_update_request_calls_extractor_and_commits(self):
        """22. 修改请求必须调用 extractor，并到达统一事务提交"""
        seed_complete_valid_pipeline_task(self.dm, self.kb)
        with patch.object(self.dm.extractor, "extract_updates", return_value={
            "intent": "TASK_UPDATE",
            "slot_candidates": [
                {"canonical_key": "water_depth", "normalized_value": 500.0, "raw_value": "500米", "confidence": 1.0}
            ]
        }) as mock_ext:
            reply = self.dm.process("把作业水深改为500米")
            mock_ext.assert_called_once()
            slot = self.dm.slot_store.slots.get("water_depth")
            self.assertIsNotNone(slot)
            val = slot.candidate_value or slot.value
            self.assertEqual(val, 500.0)

    def test_r23_do_i_have_001_apples(self):
        """23. '我有001个苹果吗？' - 不得产生设备相关 reason，不得修改 SlotStore"""
        seed_complete_valid_pipeline_task(self.dm, self.kb)
        snap_before = copy.deepcopy(self.dm.slot_store.export_snapshot())

        res = self.dm.intent_router.route("我有001个苹果吗？", [], self.dm.task_state, phase=self.dm.phase)
        self.assertNotIn("设备", res.reason)
        self.assertNotIn("歧义", res.reason)

        reply = self.dm.process("我有001个苹果吗？")
        snap_after = copy.deepcopy(self.dm.slot_store.export_snapshot())
        self.assertEqual(snap_before, snap_after)

    def test_r24_when_will_order_001_arrive(self):
        """24. '订单001什么时候到？' - 不得因'到'识别成 TASK_UPDATE，不得触发设备别名规则"""
        seed_complete_valid_pipeline_task(self.dm, self.kb)
        res = self.dm.intent_router.route("订单001什么时候到？", [], self.dm.task_state, phase=self.dm.phase)
        self.assertNotEqual(res.intent, "TASK_UPDATE")
        self.assertNotIn("设备", res.reason)


if __name__ == "__main__":
    unittest.main()
