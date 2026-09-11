"""
tests/test_validator_submodules.py — 约束与规则治理子模块专有测试套件

测试对象：
1. RuleEngine (src/rule_engine.py): 静态字段合法性、时限规则与四级选型可行域
2. SpatialValidator (src/spatial_validator.py): 空间坐标、禁入区与底质安全检查
3. TelemetryGate (src/telemetry_gate.py): 动态遥测快照解析、时钟偏斜与单机健康度门禁
4. TaskValidator (src/validator.py): 门面调度一致性、指纹计算与向后兼容契约
"""

import copy
import math
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

from src.knowledge_retriever import KnowledgeBase
from src.rule_engine import (
    RuleEngine,
    START_TIME_PAST_GRACE_MINUTES,
    get_current_datetime as re_get_now,
)
from src.spatial_validator import SpatialValidator, SPATIAL_CHECKS
from src.telemetry_gate import (
    TelemetryGate,
    matches_numeric_thresholds,
    display_threshold,
    get_current_datetime as tg_get_now,
)
from src.validator import (
    TaskValidator,
    Violation,
    ValidationResult,
    _compute_fingerprint,
    get_current_datetime as val_get_now,
)

NOW = datetime(2026, 7, 28, 12, 0, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
SAFE_POINT = {"lat": 17.60, "lon": 111.00}
FORBIDDEN_POINT = {"lat": 20.40, "lon": 109.85}
DVL_RISK_POINT = {"lat": 20.50, "lon": 113.00}
SOFT_SEABED_POINT = {"lat": 17.52, "lon": 110.15}


class TestRuleEngine(unittest.TestCase):
    """测试 RuleEngine 静态规则与选型约束校验引擎"""

    @classmethod
    def setUpClass(cls):
        cls.kb = KnowledgeBase()

    def setUp(self):
        self.engine = RuleEngine(self.kb)

    def test_validate_water_depth_value(self):
        # 正常浮点与整型
        val, err = self.engine.validate_water_depth_value(300)
        self.assertEqual(300.0, val)
        self.assertIsNone(err)

        val, err = self.engine.validate_water_depth_value("450.5")
        self.assertEqual(450.5, val)
        self.assertIsNone(err)

        # 非法值：布尔值
        val, err = self.engine.validate_water_depth_value(True)
        self.assertIsNone(val)
        self.assertEqual("INVALID_WATER_DEPTH", err["code"])

        # 非法值：0 与 负数
        val, err = self.engine.validate_water_depth_value(0)
        self.assertIsNone(val)
        self.assertEqual("INVALID_WATER_DEPTH", err["code"])

        val, err = self.engine.validate_water_depth_value(-10)
        self.assertIsNone(val)
        self.assertEqual("INVALID_WATER_DEPTH", err["code"])

        # 非法值：NaN 与 Inf
        val, err = self.engine.validate_water_depth_value(float("nan"))
        self.assertIsNone(val)
        self.assertEqual("INVALID_WATER_DEPTH", err["code"])

    def test_validate_time_value(self):
        # 正常字符串与全角冒号转换
        dt, err = self.engine.validate_time_value("2026-07-28 12：00：00", "start_time")
        self.assertIsNone(err)
        self.assertIsNotNone(dt)
        self.assertEqual(2026, dt.year)
        self.assertEqual(12, dt.hour)

        # UTC 结尾 Z 兼容
        dt, err = self.engine.validate_time_value("2026-07-28T12:00:00Z", "start_time")
        self.assertIsNone(err)
        self.assertIsNotNone(dt)

        # 非法布尔值
        dt, err = self.engine.validate_time_value(False, "start_time")
        self.assertIsNone(dt)
        self.assertEqual("MALFORMED_TIME_FORMAT", err["code"])

        # 无法解析的文本
        dt, err = self.engine.validate_time_value("invalid-time-string", "start_time")
        self.assertIsNone(dt)
        self.assertEqual("MALFORMED_TIME_FORMAT", err["code"])

    def test_validate_max_depth_m_value(self):
        depth, err = self.engine.validate_max_depth_m_value(3000, "WROV-250-001")
        self.assertEqual(3000.0, depth)
        self.assertIsNone(err)

        depth, err = self.engine.validate_max_depth_m_value("invalid", "WROV-250-001")
        self.assertIsNone(depth)
        self.assertIsNotNone(err)
        self.assertEqual("INVALID_ROV_MAX_DEPTH", err["code"])

    def test_robot_selection_tuple_and_feasibility(self):
        # 正常完整三元组
        task_state = {
            "task_type_key": "pipeline_inspection",
            "equipment_type": "观察级深海机器人 75HP",
            "equipment_unit_id": "OBSROV-75-001",
        }
        res, err = self.engine.validate_robot_selection_tuple(task_state, require_unit=True)
        self.assertIsNone(err)
        self.assertEqual("OBSROV-75-001", res["unit_id"])
        self.assertEqual("observation_rov", res["robot_class"])

        # 机型与任务不匹配
        mismatch_state = {
            "task_type_key": "tree_valve_operation",
            "equipment_type": "观察级深海机器人 75HP",
            "equipment_unit_id": "OBSROV-75-001",
        }
        res, err = self.engine.validate_robot_selection_tuple(mismatch_state, require_unit=True)
        self.assertIsNotNone(err)
        self.assertIn(err["code"], {"CLASS_NOT_ALLOWED_FOR_TASK", "UNIT_NOT_FOUND"})


class TestSpatialValidator(unittest.TestCase):
    """测试 SpatialValidator 空间坐标与地理环境安全校验器"""

    @classmethod
    def setUpClass(cls):
        cls.kb = KnowledgeBase()

    def setUp(self):
        self.validator = SpatialValidator(self.kb)

    def test_spatial_checks_registry(self):
        self.assertIn("forbidden_area", SPATIAL_CHECKS)
        self.assertIn("dvl_high_risk", SPATIAL_CHECKS)
        self.assertIn("seabed_compatibility", SPATIAL_CHECKS)

    def test_forbidden_area_violation(self):
        c = next(item for item in self.kb.get_constraints() if item["id"] == "C008")
        task_state = {
            "start_point": copy.deepcopy(FORBIDDEN_POINT),
        }
        v = self.validator.check_rule(c, "forbidden_area", task_state, rov=None)
        self.assertIsNotNone(v)
        self.assertEqual("C008", v.constraint_id)
        self.assertEqual("hard", v.severity)

    def test_safe_point_no_violation(self):
        c = next(item for item in self.kb.get_constraints() if item["id"] == "C008")
        task_state = {
            "start_point": copy.deepcopy(SAFE_POINT),
        }
        v = self.validator.check_rule(c, "forbidden_area", task_state, rov=None)
        self.assertIsNone(v)

    def test_dvl_high_risk_violation(self):
        c = next(item for item in self.kb.get_constraints() if item["id"] == "C010")
        task_state = {
            "start_point": copy.deepcopy(DVL_RISK_POINT),
        }
        v = self.validator.check_rule(c, "dvl_high_risk", task_state, rov=None)
        self.assertIsNotNone(v)
        self.assertEqual("C010", v.constraint_id)
        self.assertEqual("soft", v.severity)

    def test_seabed_compatibility_violation(self):
        c = next(item for item in self.kb.get_constraints() if item["id"] == "C009")
        crawler = self.kb.get_rov("履带式海底重载作业机器人 1600HP")
        task_state = {
            "task_type_key": "pipeline_burial",
            "start_point": copy.deepcopy(SOFT_SEABED_POINT),
        }
        v = self.validator.check_rule(c, "seabed_compatibility", task_state, rov=crawler)
        self.assertIsNotNone(v)
        self.assertEqual("C009", v.constraint_id)
        self.assertEqual("hard", v.severity)


class TestTelemetryGate(unittest.TestCase):
    """测试 TelemetryGate 动态遥测状态与单机健康度门禁"""

    @classmethod
    def setUpClass(cls):
        cls.kb = KnowledgeBase()

    def setUp(self):
        self.gate = TelemetryGate(self.kb)

    def test_matches_numeric_thresholds(self):
        thresholds = {"min_exclusive": 0.5, "max_inclusive": 1.2}
        self.assertFalse(matches_numeric_thresholds(0.5, thresholds))
        self.assertTrue(matches_numeric_thresholds(0.50001, thresholds))
        self.assertTrue(matches_numeric_thresholds(1.2, thresholds))
        self.assertFalse(matches_numeric_thresholds(1.20001, thresholds))

        # 非数值或无穷大拦截
        with self.assertRaises(ValueError):
            matches_numeric_thresholds(True, thresholds)
        with self.assertRaises(ValueError):
            matches_numeric_thresholds(float("inf"), thresholds)

    def test_display_threshold(self):
        self.assertEqual(1.2, display_threshold({"max_inclusive": 1.2, "min_exclusive": 0.5}))
        self.assertEqual(0.8, display_threshold({"min_exclusive": 0.8}))
        self.assertIsNone(display_threshold({}))

    def test_validate_state_snapshot_content(self):
        # 正常快照
        normal_snapshot = {
            "status_ref": "ref-001",
            "state_version": 1,
            "state": {
                "update_timestamp": NOW.isoformat(),
                "confidence": 0.9,
                "overall_status": "available",
            },
        }
        with patch("src.validator.get_current_datetime", return_value=NOW):
            err = self.gate.validate_state_snapshot_content("OBSROV-75-001", normal_snapshot)
            self.assertIsNone(err)

        # 缺失 state 字典
        err = self.gate.validate_state_snapshot_content("OBSROV-75-001", {"unit_id": "OBSROV-75-001", "status_ref": "ref-001", "state_version": 1})
        self.assertIsNotNone(err)
        self.assertEqual("INVALID_STATE_DATA", err["code"])

        # 时间戳未来偏斜超标 (超过 120s)
        skewed_snapshot = {
            "status_ref": "ref-001",
            "state_version": 1,
            "state": {
                "update_timestamp": (NOW + timedelta(seconds=121)).isoformat(),
            },
        }
        with patch("src.validator.get_current_datetime", return_value=NOW):
            err = self.gate.validate_state_snapshot_content("OBSROV-75-001", skewed_snapshot)
            self.assertIsNotNone(err)
            self.assertEqual("INVALID_STATE_DATA", err["code"])

    def test_is_task_start_now(self):
        with patch("src.validator.get_current_datetime", return_value=NOW):
            # 未指定 start_time：默认判定为即时任务
            self.assertTrue(self.gate.is_task_start_now({}))

            # 即时任务 (在当前时间后 30 分钟内)
            self.assertTrue(self.gate.is_task_start_now({"start_time": (NOW + timedelta(minutes=30)).isoformat()}))

            # 未来任务 (在当前时间后 2 小时)
            self.assertFalse(self.gate.is_task_start_now({"start_time": (NOW + timedelta(hours=2)).isoformat()}))

            # 进行中任务 (过去开始但尚未结束)
            in_progress = {
                "start_time": (NOW - timedelta(hours=1)).isoformat(),
                "end_time": (NOW + timedelta(hours=1)).isoformat(),
            }
            self.assertTrue(self.gate.is_task_start_now(in_progress))


class TestTaskValidatorFacade(unittest.TestCase):
    """测试 TaskValidator 门面调度、指纹稳定性与向后兼容契约"""

    @classmethod
    def setUpClass(cls):
        cls.kb = KnowledgeBase()

    def setUp(self):
        self.validator = TaskValidator(self.kb)

    def test_fingerprint_stability(self):
        v1 = Violation("C001", "name1", "msg1", "hard")
        v2 = Violation("C002", "name2", "msg2", "soft")
        fp1 = _compute_fingerprint(1, "ref-1", 1, [v1, v2], None)
        fp2 = _compute_fingerprint(1, "ref-1", 1, [v2, v1], None)  # 顺序颠倒
        self.assertEqual(fp1, fp2, "指纹计算必须不受违规列表初始顺序影响")

        fp3 = _compute_fingerprint(2, "ref-1", 1, [v1, v2], None)  # 版本变动
        self.assertNotEqual(fp1, fp3, "任务版本变动必须导致指纹更新")

    def test_backward_compatibility_static_proxies(self):
        # 验证代理在 TaskValidator 实例与类对象上均可正常工作
        val, err = TaskValidator._validate_water_depth_value(500)
        self.assertEqual(500.0, val)
        self.assertIsNone(err)

        val_inst, err_inst = self.validator._validate_water_depth_value(500)
        self.assertEqual(500.0, val_inst)
        self.assertIsNone(err_inst)

        dt, err = TaskValidator._validate_time_value("2026-07-28T12:00:00", "start_time")
        self.assertIsNone(err)
        self.assertIsNotNone(dt)

    def test_delegated_submodule_instantiation(self):
        self.assertIsInstance(self.validator.rule_engine, RuleEngine)
        self.assertIsInstance(self.validator.spatial_validator, SpatialValidator)
        self.assertIsInstance(self.validator.telemetry_gate, TelemetryGate)
        self.assertIs(self.validator.rule_engine.parent_validator, self.validator)
        self.assertIs(self.validator.spatial_validator.parent_validator, self.validator)
        self.assertIs(self.validator.telemetry_gate.parent_validator, self.validator)


if __name__ == "__main__":
    unittest.main()
