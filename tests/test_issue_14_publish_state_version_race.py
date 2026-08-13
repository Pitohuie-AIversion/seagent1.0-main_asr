"""
test_issue_14_publish_state_version_race.py — 覆盖发布前状态版本发生变更触发重新校验与防 TOCTOU 竞争
"""

import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from src.knowledge_retriever import KnowledgeBase
from src.dialogue_manager import DialogueManager
from src.exceptions import TaskPersistenceError, TaskRollbackError


def _setup_dm(tmp_dir: Path, intent_suffix: str) -> DialogueManager:
    """公用辅助：建立隔离状态文件、初始化 DM 并填充有效 task_state。"""
    from src.simulated_time import get_current_datetime
    state_file = tmp_dir / "state.yaml"
    shutil.copy("config/state.yaml", state_file)
    kb = KnowledgeBase()
    kb.state_info.state_file = state_file

    dm = DialogueManager(kb=kb)
    now_str = get_current_datetime().isoformat(timespec="seconds")
    dm.task_state = {
        "task_type": "海底管道巡检",
        "task_type_key": "pipeline_inspection",
        "cable_type": "海底油气管道",
        "equipment_class": "observation_rov",
        "equipment_family": "观察级深海机器人",
        "equipment_type": "观察级深海机器人 75HP",
        "equipment_name": "观察级深海机器人 75HP",
        "equipment_unit_id": "OBSROV-75-001",
        "start_time": now_str,
        "end_time": "2099-01-01T18:00:00+08:00",
        "water_depth": 300,
        "support_vessel": "深海一号",
        "start_point": {"lat": 20.0, "lon": 110.0},
        "end_point": {"lat": 20.1, "lon": 110.1},
        "intent_id": f"TI20260806{intent_suffix}",
    }
    req_schema = dm.builder.get_schema("pipeline_inspection", dm.mode)
    dm.slot_store.init_task_slots(req_schema)
    for k, v in dm.task_state.items():
        if k in dm.slot_store.slots:
            dm.slot_store.slots[k].value = v
            dm.slot_store.slots[k].status = "valid"
    for slot_name, slot in dm.slot_store.slots.items():
        if slot.status != "valid":
            slot.status = "valid"
            slot.value = {
                "number": 1.0,
                "boolean": False,
                "list": [],
                "coord": {"lat": 0.0, "lon": 0.0},
                "datetime": now_str,
                "object": {},
            }.get(slot.value_type, "auto_populated")
    dm.slot_store.slots["intent_id"].value = f"TI20260806{intent_suffix}"
    dm.slot_store.slots["intent_id"].status = "valid"
    dm.slot_store.slots["task_id"].value = f"PI-20260806-{intent_suffix}"
    dm.slot_store.slots["task_id"].status = "valid"
    dm._last_built_json = dict(dm.task_state)
    dm.phase = "confirming"
    return dm


class TestPublishStateVersionRace(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_publish_revalidates_when_state_version_changes(self):
        """发布前遥测 state_version 改变且引入严重超流速时，应被检测到并阻断。"""
        tmp_dir = Path(self._tmp) / "test1"
        tmp_dir.mkdir()
        dm = _setup_dm(tmp_dir, "0001")
        kb = dm.kb

        kb.state_info.set_status("OBSROV-75-001", {"current_velocity": 0.2, "turbidity": 3})

        val_res = dm._refresh_validation(purpose="publish")
        self.assertIn(val_res.overall_status, ("valid", "warning", "pending_runtime_validation"))

        original_get_snapshot = kb.state_info.get_unit_state_snapshot
        call_count = [0]

        def mock_changed_snapshot(unit_id):
            call_count[0] += 1
            snap = dict(original_get_snapshot(unit_id))
            if call_count[0] >= 2:
                snap["state_version"] = snap["state_version"] + 10
                snap["state"] = dict(snap["state"])
                snap["state"]["current_velocity"] = 5.0
            return snap

        kb.state_info.get_unit_state_snapshot = mock_changed_snapshot
        kb.get_unit_state_snapshot = mock_changed_snapshot
        try:
            with self.assertRaises((TaskPersistenceError, TaskRollbackError)) as ctx:
                dm._handle_final_publish_confirmation("确认发布", request_id="req-race-1")
            self.assertIn("状态遥测在确认发布过程中发生变更", str(ctx.exception))
        finally:
            kb.state_info.get_unit_state_snapshot = original_get_snapshot
            kb.get_unit_state_snapshot = original_get_snapshot

    def test_publish_revalidate_exception_fails_closed(self):
        """发布前状态复核抛出异常时必须 fail closed (事务回滚，final 文件不存在，phase != done)。"""
        tmp_dir = Path(self._tmp) / "test2"
        tmp_dir.mkdir()
        task_dir = tmp_dir / "task_intents"
        task_dir.mkdir(parents=True, exist_ok=True)
        dm = _setup_dm(tmp_dir, "0002")
        kb = dm.kb

        kb.state_info.set_status("OBSROV-75-001", {"current_velocity": 0.2, "turbidity": 3})

        original_get_snapshot = kb.state_info.get_unit_state_snapshot
        call_count = [0]

        def mock_broken_snapshot(unit_id):
            call_count[0] += 1
            if call_count[0] >= 2:
                raise RuntimeError("IO Error reading robot state snapshot")
            return original_get_snapshot(unit_id)

        kb.state_info.get_unit_state_snapshot = mock_broken_snapshot
        try:
            with patch("src.task_intent_builder.get_task_dir", return_value=task_dir):
                with self.assertRaises(TaskPersistenceError) as ctx:
                    dm._handle_final_publish_confirmation("确认发布", request_id="req-race-failclosed")
            self.assertTrue(
                "fail closed" in str(ctx.exception) or "发布前单机状态复核失败" in str(ctx.exception)
            )
            self.assertNotEqual(dm.phase, "done")
            final_file = task_dir / "task_intent_TI202608060002.json"
            self.assertFalse(final_file.exists())
        finally:
            kb.state_info.get_unit_state_snapshot = original_get_snapshot

    def test_publish_new_soft_warning_blocks_without_acknowledgement(self):
        """状态变化产生新的 blocked_soft 且缺乏有效 acknowledgement 时必须阻断并更新 phase=blocked_soft。"""
        tmp_dir = Path(self._tmp) / "test3"
        tmp_dir.mkdir()
        task_dir = tmp_dir / "task_intents"
        task_dir.mkdir(parents=True, exist_ok=True)
        dm = _setup_dm(tmp_dir, "0003")
        kb = dm.kb

        kb.state_info.set_status("OBSROV-75-001", {"current_velocity": 0.2, "turbidity": 3})

        original_get_snapshot = kb.state_info.get_unit_state_snapshot
        call_count = [0]

        def mock_soft_changed_snapshot(unit_id):
            call_count[0] += 1
            snap = dict(original_get_snapshot(unit_id))
            if call_count[0] >= 2:
                snap["state_version"] = snap["state_version"] + 1
                snap["state"] = dict(snap["state"])
                snap["state"]["turbidity"] = 15  # 触发 C014 软警告
            return snap

        kb.state_info.get_unit_state_snapshot = mock_soft_changed_snapshot
        try:
            with patch("src.task_intent_builder.get_task_dir", return_value=task_dir):
                with self.assertRaises(TaskPersistenceError):
                    dm._handle_final_publish_confirmation("确认发布", request_id="req-race-soft")
            self.assertEqual(dm.phase, "blocked_soft")
            final_file = task_dir / "task_intent_TI202608060003.json"
            self.assertFalse(final_file.exists())
        finally:
            kb.state_info.get_unit_state_snapshot = original_get_snapshot

    def test_real_state_lock_race_with_barrier(self):
        """测试真实状态锁 guard_unit_state_version 与 set_status 并发互斥逻辑（使用 threading.Barrier，不使用 sleep）。"""
        tmp_dir = Path(self._tmp) / "test4"
        tmp_dir.mkdir()
        state_file = tmp_dir / "state.yaml"
        shutil.copy("config/state.yaml", state_file)
        kb = KnowledgeBase()
        kb.state_info.state_file = state_file

        current_snap = kb.state_info.get_unit_state_snapshot("OBSROV-75-001")
        current_ver = current_snap["state_version"]

        barrier = threading.Barrier(2)
        step = []

        def worker_guard():
            with kb.state_info.guard_unit_state_version("OBSROV-75-001", current_ver):
                step.append("guard_entered")
                barrier.wait(timeout=5)  # 通知并等待 worker_update 试图获取排他锁
                step.append("guard_holding")
            step.append("guard_exited")

        def worker_update():
            barrier.wait(timeout=5)  # 确保 worker_guard 已进入 guard
            kb.state_info.set_status("OBSROV-75-001", {"current_velocity": 0.5})
            step.append("update_finished")

        t1 = threading.Thread(target=worker_guard)
        t2 = threading.Thread(target=worker_update)

        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        self.assertFalse(t1.is_alive())
        self.assertFalse(t2.is_alive())
        # 状态更新必须在 guard 退出后才完成
        self.assertEqual(step, ["guard_entered", "guard_holding", "guard_exited", "update_finished"])


if __name__ == "__main__":
    unittest.main()
