"""
src/constants.py - 全局业务常量与字段标签定义
"""

from __future__ import annotations

FIELD_LABELS = {
    "task_id":             "任务编号",
    "task_type":           "任务类型",
    "start_time":          "开始时间",
    "end_time":            "结束时间",
    "cable_position":      "管缆位置",
    "cable_type":          "管缆类型",
    "start_point":         "起始点经纬度",
    "end_point":           "结束点经纬度",
    "water_depth":         "水深（米）",
    "equipment_class":     "机器人类别",
    "equipment_family":    "机器人系列",
    "equipment_type":      "设备型号",
    "equipment_name":      "设备全称",
    "equipment_unit_id":   "具体机器人编号",
    "payload":             "携带工具",
    "support_vessel":      "支持船编号",
    "oilfield_name":       "油田名称",
    "oilfield_coordinates":"油田经纬度",
    "wellhead_id":         "井口编号",
}

RECOMMENDATION_FIELD_BY_SUBJECT = {
    "device_class": "equipment_family",
    "device_family": "equipment_family",
    "device": "equipment_type",
}

ROBOT_CASCADE_FIELDS = {
    "equipment_class",
    "equipment_family",
    "equipment_type",
    "equipment_unit_id",
}

OILFIELD_CONTEXT_FIELDS = {
    "oilfield_name",
    "oilfield_coordinates",
    "raw_oilfield_name",
    "oilfield_match_status",
    "oilfield_match_confidence",
    "oilfield_match_evidence",
    "oilfield_match_candidates",
    "oilfield_entity_id",
    "pending_oilfield_name",
    "pending_oilfield_candidates",
}

TASK_TRANSITION_NON_INHERITED_FIELDS = {
    "task_type",
    "payload",
    "equipment_name",
    *ROBOT_CASCADE_FIELDS,
    *OILFIELD_CONTEXT_FIELDS,
}

# 软约束忽略关键词
SOFT_IGNORE_KEYWORDS = {"忽略", "继续", "确认", "无视", "不管", "没关系", "ok", "好的", "是"}

# 连续硬拒绝上限
HARD_REFUSAL_LIMIT = 4
