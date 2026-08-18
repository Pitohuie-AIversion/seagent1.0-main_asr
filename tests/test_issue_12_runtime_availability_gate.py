"""
tests/test_issue_12_runtime_availability_gate.py — 运行时设备可用性门禁测试

覆盖场景：
1. Test 1：在线、空闲、状态新鲜 → 允许发布
2. Test 2：设备离线 → 阻止发布
3. Test 3：设备忙碌 → 阻止发布
4. Test 4：状态记录不存在 → 阻止发布
5. Test 5：状态快照过期 → 边界判断（一天内有效，超过一天过期）
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
from tests.interaction_plan_support import ScriptedLLM
from tests.test_slot_consistency import seed_complete_valid_pipeline_task


STATE_MAX_AGE_SECONDS = 10 * 60
ONE_DAY_SECONDS = STATE_MAX_AGE_SECONDS


class Issue12RuntimeAvailabilityGateTest(unittest.TestCase):
    _intent_counter = 1000

    def setUp(self):
        self.kb = KnowledgeBase()
        self._temp_dir = tempfile.TemporaryDirectory()
        self.kb.state_info.state_file = Path(self._temp_dir.name) / "state.yaml"
        self.llm = ScriptedLLM()
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

        task_dir = get_task_dir("final")
        final_file = task_dir / f"task_intent_{intent_id}.json"
        if final_file.exists():
            try:
                final_file.unlink()
            except OSError:
                pass
        self.created_final_files.append(final_file)

        self.dm.slot_store.slots["intent_id"].value = intent_id
        self.dm.slot_store.slots["intent_id"].status = "valid"
        self.dm.task_state["intent_id"] = intent_id
        if isinstance(self.dm._last_built_json, dict):
            self.dm._last_built_json["intent_id"] = intent_id

        if unit_id:
            resolved = self.kb.resolve_robot_unit(
                unit_id,
                task_type_key="pipeline_inspection",
            )
            if resolved:
                robot = resolved["robot"]
                equipment_values = {
                    "equipment_class": robot["robot_class"],
                    "equipment_family": robot["family_full_name"],
                    "equipment_type": robot["full_name"],
                    "equipment_unit_id": unit_id,
                }
                supported = robot.get("supported_payloads", [])
                task_commons = self.kb.assets.get("payload_options", {}).get("pipeline_inspection", {}).get("common", [])
                valid_p = [p for p in task_commons if p in supported]
                if not valid_p:
                    valid_p = supported[:1] if supported else ["侧扫声呐"]
                equipment_values["payload"] = valid_p[:1]
            else:
                # Some negative tests intentionally place a family/variant/alias
                # in the concrete-unit slot to verify fail-closed rejection.
                equipment_values = {"equipment_unit_id": unit_id}
            for key, value in equipment_values.items():
                self.dm.slot_store.slots[key].value = value
                self.dm.slot_store.slots[key].status = "valid"
                self.dm.task_state[key] = value
                if isinstance(self.dm._last_built_json, dict):
                    self.dm._last_built_json[key] = value

        self.dm.phase = "confirming"
        return intent_id

    def _set_raw_state(self, unit_id: str, state_dict: dict):
        with self.kb.state_info._snapshot_lock(exclusive=True):
            snap = self.kb.state_info._load_state_unlocked()
            snap["robots"][unit_id] = state_dict
            self.kb.state_info._save_state_unlocked(snap)

    def test_1_online_idle_fresh_state_allows_publish(self):
        """Test 1：设备在线、空闲且状态更新时间在一天以内 → 允许发布。"""
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

        self.assertEqual(self.dm.phase, "blocked_hard")
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
        """Test 5：状态快照在一天内有效，超过一天则阻止发布。"""
        # 边界 1：距离一天还差 10 秒 -> 有效
        self._setup_confirming_task(self.unit_id)
        now_dt = get_current_datetime()
        ts_within_one_day = (
            now_dt - timedelta(seconds=ONE_DAY_SECONDS - 10)
        ).isoformat(timespec="microseconds")
        self._set_raw_state(
            self.unit_id,
            {
                "overall_status": "available",
                "updated_at": ts_within_one_day,
                "update_timestamp": ts_within_one_day,
                "version": 1,
            },
        )
        within_one_day = self.kb.state_info.check_runtime_availability(self.unit_id)
        self.assertTrue(
            within_one_day["available"],
            "a state snapshot younger than one day should remain valid",
        )

        # 边界 2：超过一天 10 秒 -> 过期阻止发布
        intent_id = self._setup_confirming_task(self.unit_id)
        ts_over_one_day = (
            now_dt - timedelta(seconds=ONE_DAY_SECONDS + 10)
        ).isoformat(timespec="microseconds")
        self._set_raw_state(
            self.unit_id,
            {
                "overall_status": "available",
                "updated_at": ts_over_one_day,
                "update_timestamp": ts_over_one_day,
                "version": 1,
            },
        )

        reply = self.dm.process("确认发布")

        self.assertEqual(
            self.dm.phase,
            "confirming",
            "遥测过期是发布时就绪条件，不应把任务升级为 blocked_hard",
        )
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
        """Test 7: version=0 且时间戳超过一天 → 阻止发布 (STATE_EXPIRED)。"""
        intent_id = self._setup_confirming_task(self.unit_id)
        now_dt = get_current_datetime()
        ts_expired = (
            now_dt - timedelta(seconds=ONE_DAY_SECONDS + 100)
        ).isoformat(timespec="microseconds")

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

        self.assertEqual(
            self.dm.phase,
            "confirming",
            "version=0 不改变遥测过期的非硬约束语义",
        )
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
        variant_name = "light_work_class_rov_150hp"
        intent_id = self._setup_confirming_task(variant_name)

        reply = self.dm.process("确认发布")

        self.assertNotEqual(self.dm.phase, "done")
        self.assertIn("未在系统中注册", reply)
        task_dir = get_task_dir("final")
        final_file = task_dir / f"task_intent_{intent_id}.json"
        self.assertFalse(final_file.exists(), "Final file should not be created for non-exact unit_id")

    def test_11_contradictory_task_status_busy_blocks_publish(self):
        """Test 11: overall_status=available 但 task_status=busy 矛盾状态 → 阻止发布 (BUSY)。"""
        intent_id = self._setup_confirming_task(self.unit_id)
        now_dt = get_current_datetime()
        ts_fresh = now_dt.isoformat(timespec="microseconds")

        self._set_raw_state(
            self.unit_id,
            {
                "overall_status": "available",
                "task_status": "busy",
                "updated_at": ts_fresh,
                "update_timestamp": ts_fresh,
                "version": 1,
            },
        )

        reply = self.dm.process("确认发布")

        self.assertNotEqual(self.dm.phase, "done")
        self.assertTrue("忙碌" in reply or "正在执行其他任务" in reply, f"Got reply: {reply}")
        task_dir = get_task_dir("final")
        final_file = task_dir / f"task_intent_{intent_id}.json"
        self.assertFalse(final_file.exists(), "Final file should not be created for contradictory busy status")

    def test_12_contradictory_connection_status_offline_blocks_publish(self):
        """Test 12: overall_status=available 但 connection_status=offline 矛盾状态 → 阻止发布 (OFFLINE)。"""
        intent_id = self._setup_confirming_task(self.unit_id)
        now_dt = get_current_datetime()
        ts_fresh = now_dt.isoformat(timespec="microseconds")

        self._set_raw_state(
            self.unit_id,
            {
                "overall_status": "available",
                "connection_status": "offline",
                "updated_at": ts_fresh,
                "update_timestamp": ts_fresh,
                "version": 1,
            },
        )

        reply = self.dm.process("确认发布")

        self.assertNotEqual(self.dm.phase, "done")
        self.assertIn("离线", reply)
        task_dir = get_task_dir("final")
        final_file = task_dir / f"task_intent_{intent_id}.json"
        self.assertFalse(final_file.exists(), "Final file should not be created for contradictory offline status")

    def test_13_contradictory_is_online_string_false_blocks_publish(self):
        """Test 13: overall_status=available 但 is_online='false' (字符串布尔) → 阻止发布 (OFFLINE)。"""
        intent_id = self._setup_confirming_task(self.unit_id)
        now_dt = get_current_datetime()
        ts_fresh = now_dt.isoformat(timespec="microseconds")

        self._set_raw_state(
            self.unit_id,
            {
                "overall_status": "available",
                "is_online": "false",
                "updated_at": ts_fresh,
                "update_timestamp": ts_fresh,
                "version": 1,
            },
        )

        reply = self.dm.process("确认发布")

        self.assertNotEqual(self.dm.phase, "done")
        self.assertIn("离线", reply)
        task_dir = get_task_dir("final")
        final_file = task_dir / f"task_intent_{intent_id}.json"
        self.assertFalse(final_file.exists(), "Final file should not be created when is_online is 'false'")


    def test_14_auto_bound_unit_does_not_bypass_runtime_gate(self):
        """Test 14: 四级自动收敛绑定的 equipment_unit_id (WROV-250-001) 若运行时离线，仍必须被 Runtime Availability Gate 阻断发布。"""
        intent_id = self._setup_confirming_task("WROV-250-001")
        slots = self.dm.slot_store.slots
        slots["equipment_unit_id"].source = "auto"
        self.assertEqual(slots["equipment_unit_id"].value, "WROV-250-001")
        self.assertEqual(slots["equipment_unit_id"].status, "valid")
        self.assertEqual(slots["equipment_unit_id"].source, "auto")

        # 模拟 WROV-250-001 离线
        now_dt = get_current_datetime()
        ts_fresh = now_dt.isoformat(timespec="microseconds")
        self._set_raw_state(
            "WROV-250-001",
            {
                "overall_status": "offline",
                "updated_at": ts_fresh,
                "update_timestamp": ts_fresh,
                "version": 1,
            },
        )

        self.dm.phase = "confirming"
        reply = self.dm.process("确认发布")

        self.assertNotEqual(self.dm.phase, "done")
        self.assertIn("离线", reply)


if __name__ == "__main__":
    unittest.main()
