"""
test_task_intent_semantic_mapping.py — TaskIntent 语义映射与 TaskIntentBuilder 权威校验测试
"""
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.exceptions import IntentIdConflict, TaskPersistenceError
from src.knowledge_retriever import KnowledgeBase
from src.task_intent_builder import TaskIntentBuilder, validate_task_intent


class TestTaskIntentSemanticMapping(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.kb = KnowledgeBase()

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.task_dir = Path(self.test_dir) / "task"
        self.task_dir.mkdir(parents=True, exist_ok=True)
        self.patcher = patch("src.task_intent_builder.get_task_dir", return_value=self.task_dir)
        self.patcher.start()
        self.builder = TaskIntentBuilder(self.kb)

    def tearDown(self):
        self.patcher.stop()
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_semantic_matrix_mapping(self):
        """测试 4 类设备与 3 类任务的标准 TaskIntent 语义映射"""
        cases = [
            {
                "name": "管缆巡检 + 观察级ROV",
                "task_type_key": "pipeline_inspection",
                "equipment_type": "观察级ROV",
                "expected_task_type": "pipeline_inspection",
                "expected_robot_type": "observation_rov",
            },
            {
                "name": "管缆巡检 + AUV",
                "task_type_key": "pipeline_inspection",
                "equipment_type": "AUV",
                "expected_task_type": "pipeline_inspection",
                "expected_robot_type": "auv",
            },
            {
                "name": "管缆埋设 + 履带式机器人",
                "task_type_key": "pipeline_burial",
                "equipment_type": "履带式机器人",
                "expected_task_type": "pipeline_burial",
                "expected_robot_type": "work_class_rov",
            },
            {
                "name": "采油树操作 + 工作级ROV",
                "task_type_key": "tree_valve_operation",
                "equipment_type": "工作级ROV",
                "expected_task_type": "valve_operation",
                "expected_robot_type": "work_class_rov",
            },
        ]

        for case in cases:
            with self.subTest(case=case["name"]):
                task_state = {"intent_id": "TI2026073001", "oilfield_name": "流花11-1油田"}
                built_json = {
                    "intent_id": "TI2026073001",
                    "equipment_type": case["equipment_type"],
                    "water_depth": 300.0,
                    "start_time": "2026-08-01T08:00:00+08:00",
                    "end_time": "2026-08-01T18:00:00+08:00",
                }
                intent = self.builder.prepare(
                    task_state=task_state,
                    built_json=built_json,
                    mode="normal",
                    task_type_key=case["task_type_key"],
                )

                self.assertEqual(intent["task_type"], case["expected_task_type"])
                self.assertEqual(intent["task"]["type"], case["expected_task_type"])
                self.assertEqual(intent["equipment"]["robot_type"], case["expected_robot_type"])
                self.assertTrue(validate_task_intent(intent))

    def test_unknown_equipment_raises_persistence_error(self):
        """未知设备不得默认回退成 observation_rov，必须抛出 TaskPersistenceError"""
        task_state = {"intent_id": "TI2026073002"}
        built_json = {
            "intent_id": "TI2026073002",
            "equipment_type": "未知飞碟型机器人",
            "water_depth": 300.0,
        }

        with self.assertRaises(TaskPersistenceError):
            self.builder.prepare(
                task_state=task_state,
                built_json=built_json,
                mode="normal",
                task_type_key="pipeline_inspection",
            )

    def test_pipeline_burial_does_not_output_valve_operation(self):
        """管缆埋设 pipeline_burial 绝对不能被输出为 valve_operation"""
        task_state = {"intent_id": "TI2026073003", "oilfield_name": "流花11-1油田"}
        built_json = {
            "intent_id": "TI2026073003",
            "equipment_type": "履带式机器人",
            "water_depth": 500.0,
        }

        intent = self.builder.prepare(
            task_state=task_state,
            built_json=built_json,
            mode="normal",
            task_type_key="pipeline_burial",
        )

        self.assertNotEqual(intent["task_type"], "valve_operation")
        self.assertEqual(intent["task_type"], "pipeline_burial")
        self.assertEqual(intent["task"]["type"], "pipeline_burial")

    def test_validate_task_intent_semantic_consistency(self):
        """测试 validate_task_intent 对语义一致性的强强校验"""
        valid_intent = {
            "intent_id": "TI2026073004",
            "task_type": "pipeline_inspection",
            "priority": 7,
            "time": {"start": "2026-08-01T08:00:00+08:00", "end": "2026-08-01T18:00:00+08:00"},
            "location": {"oilfield": "流花11-1油田", "water_depth_m": 300.0},
            "task": {
                "type": "pipeline_inspection",
                "details": {"pipeline_type": "subsea_oil_gas", "start_point": None, "end_point": None},
            },
            "equipment": {
                "robot_type": "observation_rov",
                "payload": [],
                "support_vessel": {"name": None, "latitude": None, "longitude": None},
            },
            "conditions": {},
        }
        self.assertTrue(validate_task_intent(valid_intent))

        # 顶层 task_type 与 task.type 不一致 -> validate 失败
        mismatched_intent = json.loads(json.dumps(valid_intent))
        mismatched_intent["task"]["type"] = "valve_operation"
        self.assertFalse(validate_task_intent(mismatched_intent))

        # 非法 robot_type -> validate 失败
        invalid_robot_intent = json.loads(json.dumps(valid_intent))
        invalid_robot_intent["equipment"]["robot_type"] = "unknown_robot"
        self.assertFalse(validate_task_intent(invalid_robot_intent))

    def test_reconfirm_does_not_duplicate_publish(self):
        """重复发布同一 intent_id 时必须抛出 IntentIdConflict 异常且拒绝覆盖"""
        task_state = {"intent_id": "TI2026073005", "oilfield_name": "流花11-1油田"}
        built_json = {
            "intent_id": "TI2026073005",
            "equipment_type": "观察级ROV",
            "water_depth": 200.0,
        }

        intent = self.builder.prepare(
            task_state=task_state,
            built_json=built_json,
            mode="normal",
            task_type_key="pipeline_inspection",
        )

        # 第一次发布成功
        staging_file = self.builder.create_staging(intent)
        published_name = self.builder.publish_staging(staging_file, intent)
        self.assertTrue((self.task_dir / published_name).exists())

        # 第二次重复发布 -> 必须 raise IntentIdConflict
        staging_file2 = self.builder.create_staging(intent)
        with self.assertRaises(IntentIdConflict):
            self.builder.publish_staging(staging_file2, intent)


if __name__ == "__main__":
    unittest.main()
