"""
tests/test_issue_12_runtime_availability_gate.py — 运行时设备可用性门禁测试

覆盖场景：
1. Test 1：在线、空闲、状态新鲜 → 允许发布
2. Test 2：设备离线 → 阻止发布
3. Test 3：设备忙碌 → 阻止发布
4. Test 4：状态记录不存在 → 阻止发布
5. Test 5：状态快照过期 → 边界判断 (<=300s有效, >300s过期)
6. Test 6：确认发布时重新检查 → 不使用预览缓存，重读最新状态
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from src.dialogue_manager import DialogueManager
from src.knowledge_retriever import KnowledgeBase
from src.result_paths import get_task_dir
from src.simulated_time import get_current_datetime
from tests.test_slot_consistency import seed_complete_valid_pipeline_task


class FakeLLMForRuntimeGate:
    def route_mock(self, user_message: str):
        return {
            "interaction_type": "QUERY",
            "query_intent": "GENERAL_CHAT",
            "confidence": 0.90,
            "reason": "测试",
        }

    def classify_interaction(self, messages, max_tokens=260):
        return self.route_mock("测试")

    def extract_json(self, messages, max_tokens=800):
        return {"slot_candidates": [], "unresolved": []}

    def chat(self, messages, **kwargs):
        return "测试回复"

    def filter_reply(self, reply):
        return reply


class Issue12RuntimeAvailabilityGateTest(unittest.TestCase):
    _intent_counter = 1000

    def setUp(self):
        self.kb = KnowledgeBase()
        self._temp_dir = tempfile.TemporaryDirectory()
        self.kb.state_info.state_file = Path(self._temp_dir.name) / "state.yaml"
        self.llm = FakeLLMForRuntimeGate()
        self.dm = DialogueManager(self.llm, self.kb)
        self.unit_id = "AUV-324cc-001"
        self.created_final_files: list[Path] = []

    def tearDown(self):
        for f in self.created_final_files:
            if f.exists():
                try:
                    f.unlink()
                except OSError:
                    pass
        self._temp_dir.cleanup()

    def _setup_confirming_task(self, unit_id: str = "AUV-324cc-001") -> str:
        seed_complete_valid_pipeline_task(self.dm, self.kb)
        Issue12RuntimeAvailabilityGateTest._intent_counter += 1
        intent_id = f"TI20260805{Issue12RuntimeAvailabilityGateTest._intent_counter:06d}"
        self.dm.slot_store.slots["intent_id"].value = intent_id
        self.dm.slot_store.slots["intent_id"].status = "valid"
        self.dm.task_state["intent_id"] = intent_id
        if isinstance(self.dm._last_built_json, dict):
            self.dm._last_built_json["intent_id"] = intent_id

        if unit_id:
            self.dm.slot_store.slots["equipment_unit_id"].value = unit_id
            self.dm.slot_store.slots["equipment_unit_id"].status = "valid"
            self.dm.task_state["equipment_unit_id"] = unit_id
            if isinstance(self.dm._last_built_json, dict):
                self.dm._last_built_json["equipment_unit_id"] = unit_id

        self.dm.phase = "confirming"
        return intent_id

    def _set_raw_state(self, unit_id: str, state_dict: dict):
        with self.kb.state_info._snapshot_lock(exclusive=True):
            snap = self.kb.state_info._load_state_unlocked()
            snap["robots"][unit_id] = state_dict
            self.kb.state_info._save_state_unlocked(snap)

    def test_1_online_idle_fresh_state_allows_publish(self):
        """Test 1：设备在线、空闲且状态更新时间在 300s 以内 → 允许发布。"""
        intent_id = self._setup_confirming_task(self.unit_id)
        now_dt = get_current_datetime()
        ts_fresh = (now_dt - timedelta(seconds=10)).isoformat(timespec="microseconds")

        self._set_raw_state(
            self.unit_id,
            {
                "overall_status": "available",
                "updated_at": ts_fresh,
                "update_timestamp": ts_fresh,
                "version": 1,
            },
        )

        reply = self.dm.process("确认发布")

        self.assertEqual(self.dm.phase, "done")
        self.assertIn("✅", reply)
        # 确认 final 文件存在
        task_dir = get_task_dir("final")
        final_file = task_dir / f"task_intent_{intent_id}.json"
        self.created_final_files.append(final_file)
        self.assertTrue(final_file.exists(), f"Final task intent file {final_file} should exist")

    def test_2_offline_device_blocks_publish(self):
        """Test 2：设备离线 → 阻止发布，phase != done，final 文件不存在，回复包含“离线”，SlotStore 不变。"""
        intent_id = self._setup_confirming_task(self.unit_id)
        now_dt = get_current_datetime()
        ts_fresh = now_dt.isoformat(timespec="microseconds")

        self._set_raw_state(
            self.unit_id,
            {
                "overall_status": "offline",
                "updated_at": ts_fresh,
                "update_timestamp": ts_fresh,
                "version": 1,
            },
        )

        ver_before = self.dm.slot_store.version
        reply = self.dm.process("确认发布")

        self.assertNotEqual(self.dm.phase, "done")
        self.assertIn("离线", reply)
        self.assertEqual(self.dm.slot_store.slots["equipment_unit_id"].value, self.unit_id)
        self.assertEqual(self.dm.slot_store.version, ver_before)

        task_dir = get_task_dir("final")
        final_file = task_dir / f"task_intent_{intent_id}.json"
        self.assertFalse(final_file.exists(), "Final file should not be created")

    def test_3_busy_device_blocks_publish(self):
        """Test 3：设备忙碌 → 阻止发布，phase != done，final 文件不存在，回复包含“忙碌”或“正在执行其他任务”。"""
        intent_id = self._setup_confirming_task(self.unit_id)
        now_dt = get_current_datetime()
        ts_fresh = now_dt.isoformat(timespec="microseconds")

        self._set_raw_state(
            self.unit_id,
            {
                "overall_status": "busy",
                "updated_at": ts_fresh,
                "update_timestamp": ts_fresh,
                "version": 1,
            },
        )

        reply = self.dm.process("确认发布")

        self.assertNotEqual(self.dm.phase, "done")
        self.assertTrue(
            "忙碌" in reply or "正在执行其他任务" in reply,
            f"Reply should contain busy reason, got: {reply}",
        )

        task_dir = get_task_dir("final")
        final_file = task_dir / f"task_intent_{intent_id}.json"
        self.assertFalse(final_file.exists(), "Final file should not be created")

    def test_4_missing_state_record_blocks_publish(self):
        """Test 4：状态记录不存在 → 阻止发布，phase != done，final 文件不存在，回复说明状态不可用。"""
        intent_id = self._setup_confirming_task(self.unit_id)
        # 状态文件保持无记录

        reply = self.dm.process("确认发布")

        self.assertNotEqual(self.dm.phase, "done")
        self.assertTrue(
            "不存在" in reply or "无法确认" in reply or "不可用" in reply,
            f"Reply should state status missing, got: {reply}",
        )

        task_dir = get_task_dir("final")
        final_file = task_dir / f"task_intent_{intent_id}.json"
        self.assertFalse(final_file.exists(), "Final file should not be created")

    def test_5_expired_state_blocks_publish_with_boundary_check(self):
        """Test 5：状态快照过期判定，300 秒以内 (如 290s) 有效，超过 300 秒 (如 350s) 过期阻止发布。"""
        # 边界 1：290 秒 -> 有效
        self._setup_confirming_task(self.unit_id)
        now_dt = get_current_datetime()
        ts_290 = (now_dt - timedelta(seconds=290)).isoformat(timespec="microseconds")
        self._set_raw_state(
            self.unit_id,
            {
                "overall_status": "available",
                "updated_at": ts_290,
                "update_timestamp": ts_290,
                "version": 1,
            },
        )
        res_290 = self.kb.state_info.check_runtime_availability(self.unit_id)
        self.assertTrue(res_290["available"], "290s should be fresh/valid (<= 300s)")

        # 边界 2：超过 300 秒 (350 秒) -> 过期阻止发布
        intent_id = self._setup_confirming_task(self.unit_id)
        ts_350 = (now_dt - timedelta(seconds=350)).isoformat(timespec="microseconds")
        self._set_raw_state(
            self.unit_id,
            {
                "overall_status": "available",
                "updated_at": ts_350,
                "update_timestamp": ts_350,
                "version": 1,
            },
        )

        reply = self.dm.process("确认发布")

        self.assertNotEqual(self.dm.phase, "done")
        self.assertIn("状态信息已过期", reply)

        task_dir = get_task_dir("final")
        final_file = task_dir / f"task_intent_{intent_id}.json"
        self.assertFalse(final_file.exists(), "Final file should not be created for expired state")

    def test_6_recheck_state_at_final_confirmation(self):
        """Test 6：确认发布时重新检查设备状态，不得使用预览阶段缓存。"""
        intent_id = self._setup_confirming_task(self.unit_id)
        now_dt = get_current_datetime()
        ts_fresh = now_dt.isoformat(timespec="microseconds")

        # 1. 任务预览阶段：设备在线且空闲
        self._set_raw_state(
            self.unit_id,
            {
                "overall_status": "available",
                "updated_at": ts_fresh,
                "update_timestamp": ts_fresh,
                "version": 1,
            },
        )
        res_preview = self.kb.state_info.check_runtime_availability(self.unit_id)
        self.assertTrue(res_preview["available"])

        # 2. 确认发布前：设备状态被更新为 busy
        self._set_raw_state(
            self.unit_id,
            {
                "overall_status": "busy",
                "updated_at": ts_fresh,
                "update_timestamp": ts_fresh,
                "version": 2,
            },
        )

        # 3. 用户输入“确认发布”
        reply = self.dm.process("确认发布")

        # 4. 断言：确认阶段重新读取最新状态，阻止发布
        self.assertNotEqual(self.dm.phase, "done")
        self.assertTrue("busy" not in res_preview["reason_code"])
        self.assertTrue(
            "忙碌" in reply or "正在执行其他任务" in reply,
            f"Reply should reflect rechecked busy state, got: {reply}",
        )

        task_dir = get_task_dir("final")
        final_file = task_dir / f"task_intent_{intent_id}.json"
        self.assertFalse(final_file.exists(), "Final file should not be created")

    def test_7_version_0_expired_state_blocks_publish(self):
        """Test 7: version=0 且时间戳过期 → 阻止发布 (STATE_EXPIRED)。"""
        intent_id = self._setup_confirming_task(self.unit_id)
        now_dt = get_current_datetime()
        ts_expired = (now_dt - timedelta(seconds=400)).isoformat(timespec="microseconds")

        self._set_raw_state(
            self.unit_id,
            {
                "overall_status": "available",
                "updated_at": ts_expired,
                "update_timestamp": ts_expired,
                "version": 0,
            },
        )

        reply = self.dm.process("确认发布")

        self.assertNotEqual(self.dm.phase, "done")
        self.assertIn("状态信息已过期", reply)
        task_dir = get_task_dir("final")
        final_file = task_dir / f"task_intent_{intent_id}.json"
        self.assertFalse(final_file.exists(), "Final file should not be created for version=0 expired state")

    def test_8_missing_overall_status_blocks_publish(self):
        """Test 8: 缺少 overall_status 字段 → 阻止发布 (INVALID_STATE_DATA)。"""
        intent_id = self._setup_confirming_task(self.unit_id)
        now_dt = get_current_datetime()
        ts_fresh = now_dt.isoformat(timespec="microseconds")

        self._set_raw_state(
            self.unit_id,
            {
                "updated_at": ts_fresh,
                "update_timestamp": ts_fresh,
                "version": 1,
            },
        )

        reply = self.dm.process("确认发布")

        self.assertNotEqual(self.dm.phase, "done")
        self.assertTrue("缺少状态指标" in reply or "无法确认" in reply or "无法识别" in reply)
        task_dir = get_task_dir("final")
        final_file = task_dir / f"task_intent_{intent_id}.json"
        self.assertFalse(final_file.exists(), "Final file should not be created for missing status field")

    def test_9_unknown_overall_status_value_blocks_publish(self):
        """Test 9: overall_status=unknown → 阻止发布 (INVALID_STATE_DATA)。"""
        intent_id = self._setup_confirming_task(self.unit_id)
        now_dt = get_current_datetime()
        ts_fresh = now_dt.isoformat(timespec="microseconds")

        self._set_raw_state(
            self.unit_id,
            {
                "overall_status": "unknown",
                "updated_at": ts_fresh,
                "update_timestamp": ts_fresh,
                "version": 1,
            },
        )

        reply = self.dm.process("确认发布")

        self.assertNotEqual(self.dm.phase, "done")
        self.assertTrue("无法识别" in reply or "无法确认" in reply)
        task_dir = get_task_dir("final")
        final_file = task_dir / f"task_intent_{intent_id}.json"
        self.assertFalse(final_file.exists(), "Final file should not be created for unknown status value")

    def test_10_non_exact_unit_id_blocks_publish(self):
        """Test 10: family/variant/alias 不是精确 unit_id → 阻止发布 (UNIT_NOT_FOUND)。"""
        variant_name = "light_work_class_rov_hp"
        intent_id = self._setup_confirming_task(variant_name)

        reply = self.dm.process("确认发布")

        self.assertNotEqual(self.dm.phase, "done")
        self.assertIn("未在系统中注册", reply)
        task_dir = get_task_dir("final")
        final_file = task_dir / f"task_intent_{intent_id}.json"
        self.assertFalse(final_file.exists(), "Final file should not be created for non-exact unit_id")


if __name__ == "__main__":
    unittest.main()
