"""
task_intent_builder.py — 生成符合 TaskIntent 规范的 JSON 文件
"""
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from .knowledge_retriever import KnowledgeBase
from .simulated_time import get_current_datetime
from .id_sequence import next_daily_id

import threading
from .result_paths import get_task_dir


class TaskIntentBuilder:
    def __init__(self, kb: KnowledgeBase):
        self.kb = kb

    def prepare(
        self,
        task_state: Dict[str, Any],
        built_json: Dict[str, Any],
        mode: str,
        task_type_key: str,
    ) -> Dict[str, Any]:
        """纯内存构建 TaskIntent 字典，无磁盘副作用"""
        today = get_current_datetime().strftime("%Y%m%d")
        task_dir = get_task_dir(create=False)
        intent_id = next_daily_id("TI", today, 2, [(task_dir, "intent_id")])

        if mode == "emergency":
            priority = 1
        else:
            priority = 7

        start_time = built_json.get("start_time")
        end_time = built_json.get("end_time")
        def ensure_tz(ts: Optional[str]) -> Optional[str]:
            if not ts:
                return None
            if "+" not in ts and ts.endswith("Z") is False:
                ts += "+08:00"
            return ts
        start_time = ensure_tz(start_time)
        end_time = ensure_tz(end_time)

        oilfield_name = None
        water_depth = built_json.get("water_depth")
        coords = (built_json.get("start_point") or
                  built_json.get("oilfield_coordinates") or
                  built_json.get("cable_position"))
        if coords and isinstance(coords, dict):
            lat = coords.get("lat")
            lon = coords.get("lon")
            if lat is not None and lon is not None:
                area = self.kb.get_environment_for_coords({"lat": lat, "lon": lon})
                if area:
                    oilfield_name = area.get("name")
        if not oilfield_name:
            oilfield_name = task_state.get("oilfield_name")

        details = self._build_details(task_type_key, task_state, built_json)

        equipment_type = built_json.get("equipment_type")
        robot_type_map = {
            "观察级ROV": "observation_rov",
            "工作级ROV": "work_class_rov",
            "海底拖拉机": "work_class_rov",
            "调查型AUV": "auv",
        }
        robot_type = robot_type_map.get(equipment_type, "observation_rov")
        payload = built_json.get("payload", [])
        if not isinstance(payload, list):
            payload = [payload] if payload else []
        support_vessel_name = built_json.get("support_vessel")
        support_vessel = {
            "name": support_vessel_name,
            "latitude": None,
            "longitude": None,
        }

        conditions = {}

        if task_type_key == "pipeline_inspection":
            top_task_type = "pipeline_inspection"
        else:
            top_task_type = "valve_operation"

        return {
            "intent_id": intent_id,
            "task_type": top_task_type,
            "priority": priority,
            "time": {
                "start": start_time,
                "end": end_time,
            },
            "location": {
                "oilfield": oilfield_name,
                "water_depth_m": float(water_depth) if water_depth is not None else None,
            },
            "task": {
                "type": top_task_type,
                "details": details,
            },
            "equipment": {
                "robot_type": robot_type,
                "payload": payload,
                "support_vessel": support_vessel,
            },
            "conditions": conditions,
        }

    def persist(self, intent: Dict[str, Any]) -> str:
        """持久化已构建的 intent 字典，使用同目录临时文件原子替换，幂等写入"""
        intent_id = intent.get("intent_id", "unknown")
        task_dir = get_task_dir(create=True)
        final_file = task_dir / f"task_intent_{intent_id}.json"

        # 幂等检查：目标文件已存在直接返回
        if final_file.exists():
            return final_file.name

        temp_file = task_dir / f"task_intent_{intent_id}.tmp_{os.getpid()}_{threading.get_ident()}"
        try:
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(intent, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_file, final_file)
        except Exception as e:
            if temp_file.exists():
                try:
                    temp_file.unlink()
                except Exception:
                    pass
            raise RuntimeError(f"TaskIntent persist failed for {intent_id}: {e}") from e

        return final_file.name

    def build(
        self,
        task_state: Dict[str, Any],
        built_json: Dict[str, Any],
        mode: str,
        task_type_key: str,
    ) -> Dict[str, Any]:
        """兼容接口：先 prepare 构建，再 persist 持久化"""
        intent = self.prepare(task_state, built_json, mode, task_type_key)
        self.persist(intent)
        return intent

    def _build_details(
        self,
        task_type_key: str,
        task_state: Dict[str, Any],
        built_json: Dict[str, Any],
    ) -> Dict[str, Any]:
        if task_type_key == "pipeline_inspection":
            cable_type_raw = built_json.get("cable_type")
            pipeline_type_map = {
                "海底油气管道": "subsea_oil_gas",
                "电力电缆": "power_cable",
                "光纤通信缆": "fiber_optic",
            }
            pipeline_type = pipeline_type_map.get(cable_type_raw, "unknown")
            start_point = built_json.get("start_point")
            end_point = built_json.get("end_point")

            return {
                "pipeline_type": pipeline_type,
                "start_point": {
                    "latitude": start_point.get("lat") if start_point else None,
                    "longitude": start_point.get("lon") if start_point else None,
                } if start_point else None,
                "end_point": {
                    "latitude": end_point.get("lat") if end_point else None,
                    "longitude": end_point.get("lon") if end_point else None,
                } if end_point else None,
            }

        elif task_type_key == "tree_valve_operation":
            # 采油树不再区分立式/卧式，停用类型映射与默认 vertical 输出。
            # tree_type_raw = built_json.get("tree_type")
            # ct_map = {"立式": "vertical", "卧式": "horizontal"}
            # christmas_tree_type = ct_map.get(tree_type_raw, "vertical")

            wellhead_id = built_json.get("wellhead_id") or task_state.get("wellhead_id")
            oilfield_coords = built_json.get("oilfield_coordinates") or task_state.get("oilfield_coordinates")
            target = None
            if oilfield_coords and isinstance(oilfield_coords, dict):
                target = {
                    "latitude": oilfield_coords.get("lat"),
                    "longitude": oilfield_coords.get("lon"),
                }

            hole_positions = []   # 预留接口，暂不收集

            return {
                "wellhead_id": wellhead_id,
                "target": target,
                # "christmas_tree_type": christmas_tree_type,
                "hole_positions": hole_positions,
            }
        else:
            return {}