"""
task_intent_builder.py — 生成符合 TaskIntent 规范的 JSON 文件
"""
import json
import os
import re
import stat
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from .knowledge_retriever import KnowledgeBase
from .simulated_time import get_current_datetime
from .id_sequence import next_daily_id, validate_intent_id

import threading
import uuid
from .result_paths import get_task_dir
from .exceptions import IntentIdConflict, TaskPersistenceError


class TaskIntentBuilder:
    def __init__(self, kb: KnowledgeBase):
        self.kb = kb

    def prepare(
        self,
        task_state: Dict[str, Any],
        built_json: Dict[str, Any],
        mode: str,
        task_type_key: str,
        intent_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """纯内存构建 TaskIntent 字典，无磁盘副作用"""
        if intent_id is not None:
            if not validate_intent_id(intent_id):
                raise TaskPersistenceError(f"Invalid intent_id parameter: {intent_id}")
            effective_intent_id = intent_id
        else:
            cand_id = built_json.get("intent_id") or task_state.get("intent_id")
            if cand_id:
                if not validate_intent_id(cand_id):
                    raise TaskPersistenceError(f"Invalid intent_id in task_state/built_json: {cand_id}")
                effective_intent_id = cand_id
            else:
                today = get_current_datetime().strftime("%Y%m%d")
                task_dir = get_task_dir(create=False)
                effective_intent_id = next_daily_id("TI", today, 2, [(task_dir, "intent_id")])
        intent_id = effective_intent_id

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

    def create_staging(self, intent: Dict[str, Any]) -> Path:
        """创建临时 staging 任务文件"""
        intent_id = intent.get("intent_id")
        if not validate_intent_id(intent_id):
            raise TaskPersistenceError(f"Invalid intent_id for create_staging: {intent_id}")
        task_dir = get_task_dir(create=True)
        unique_suffix = f"{os.getpid()}_{threading.get_ident()}_{uuid.uuid4().hex[:8]}"
        staging_file = task_dir / f"task_intent_{intent_id}.staging_{unique_suffix}"
        if task_dir.resolve() not in staging_file.resolve().parents:
            raise TaskPersistenceError(f"Path traversal detected for staging file: {staging_file}")
        try:
            with open(staging_file, "w", encoding="utf-8") as f:
                json.dump(intent, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            return staging_file
        except Exception as e:
            if staging_file.exists():
                try:
                    staging_file.unlink()
                except Exception:
                    pass
            raise TaskPersistenceError(f"Failed to create staging file for {intent_id}: {e}") from e

    def publish_staging(self, staging_file: Path | str, intent: Dict[str, Any]) -> str:
        """使用 os.link 原子 no-clobber 发布 staging 临时文件为正式 JSON 文件；后置强验证及防替换安全回滚"""
        if not isinstance(intent, dict):
            raise TaskPersistenceError("intent must be a dictionary")
        intent_id = intent.get("intent_id")
        if not validate_intent_id(intent_id):
            raise TaskPersistenceError(f"Invalid intent_id for publish_staging: {intent_id}")

        try:
            staging_path = Path(staging_file)
        except Exception as e:
            raise TaskPersistenceError(f"Invalid staging_file path: {e}") from e

        # 1. 验证 staging_file 存在且非符号链接
        if not staging_path.exists():
            raise TaskPersistenceError(f"Staging file does not exist: {staging_path}")

        if staging_path.is_symlink():
            raise TaskPersistenceError(f"Staging file cannot be a symlink: {staging_path}")

        if not staging_path.is_file():
            raise TaskPersistenceError(f"Staging file is not a regular file: {staging_path}")

        # 2. 验证严格解析后位于 task_dir 内部且直属 parent 等于 task_dir
        task_dir = get_task_dir(create=True)
        resolved_task_dir = task_dir.resolve()
        try:
            resolved_staging = staging_path.resolve(strict=True)
        except Exception as e:
            raise TaskPersistenceError(f"Failed to resolve staging file path: {e}") from e

        if resolved_staging.is_symlink():
            raise TaskPersistenceError(f"Resolved staging path cannot be a symlink: {resolved_staging}")

        if resolved_staging.parent != resolved_task_dir:
            raise TaskPersistenceError(
                f"Staging file {resolved_staging} is not located directly inside task_dir {resolved_task_dir}"
            )

        # 3. 验证文件名精确符合正则格式
        expected_pattern = rf"^task_intent_{re.escape(intent_id)}\.staging_[0-9]+_[0-9]+_[0-9a-f]{{8}}$"
        if not re.fullmatch(expected_pattern, staging_path.name):
            raise TaskPersistenceError(
                f"Staging filename '{staging_path.name}' does not match controlled format pattern for intent_id '{intent_id}'"
            )

        # 4. 读取 JSON 内容并记录被验证文件的 fd stat (st_dev, st_ino)
        try:
            st_before = staging_path.stat()
        except Exception as e:
            raise TaskPersistenceError(f"Failed to stat staging file: {e}") from e

        try:
            with open(resolved_staging, "r", encoding="utf-8") as f:
                f_fd = f.fileno()
                validated_stat = os.fstat(f_fd)
                if not stat.S_ISREG(validated_stat.st_mode):
                    raise TaskPersistenceError("Staging file descriptor is not a regular file")
                staging_data = json.load(f)
        except TaskPersistenceError:
            raise
        except Exception as e:
            raise TaskPersistenceError(f"Failed to parse staging JSON content: {e}") from e

        try:
            st_after = staging_path.stat()
        except Exception as e:
            raise TaskPersistenceError(f"Failed to re-stat staging file: {e}") from e

        if (st_before.st_dev != st_after.st_dev or
            st_before.st_ino != st_after.st_ino or
            st_before.st_size != st_after.st_size or
            st_before.st_mtime_ns != st_after.st_mtime_ns):
            raise TaskPersistenceError("Staging file was modified during verification")

        if not isinstance(staging_data, dict):
            raise TaskPersistenceError("Staging JSON top-level must be a dictionary")

        st_intent_id = staging_data.get("intent_id")
        if not validate_intent_id(st_intent_id):
            raise TaskPersistenceError(f"Invalid intent_id inside staging JSON: {st_intent_id}")

        if st_intent_id != intent_id:
            raise TaskPersistenceError(
                f"Staging JSON intent_id '{st_intent_id}' does not match expected intent_id '{intent_id}'"
            )

        if staging_data != intent:
            raise TaskPersistenceError("Staging JSON content does not match expected intent data")

        final_file = task_dir / f"task_intent_{intent_id}.json"
        if resolved_task_dir not in final_file.resolve().parents:
            raise TaskPersistenceError(f"Path traversal detected for final file: {final_file}")

        # 5. 原子链接与后置强验证 (若发生 TOCTOU 竞态替换则回滚 final_file 且保护替代文件)
        created_final = False
        try:
            os.link(staging_path, final_file)
            created_final = True

            # 目录 fsync 保证元数据落地
            try:
                dir_fd = os.open(task_dir, os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except Exception:
                pass

            # 6. 后置检查：强校验 final_file 的 inode、格式与内容
            if not final_file.exists() or final_file.is_symlink():
                raise TaskPersistenceError("Post-publish verification failed: final file invalid or symlink")

            final_stat = os.stat(final_file)
            if final_stat.st_dev != validated_stat.st_dev or final_stat.st_ino != validated_stat.st_ino:
                raise TaskPersistenceError("Post-publish verification failed: final file inode does not match verified staging file")

            with open(final_file, "r", encoding="utf-8") as f:
                final_data = json.load(f)

            if not isinstance(final_data, dict) or final_data != intent or final_data.get("intent_id") != intent_id:
                raise TaskPersistenceError("Post-publish verification failed: final file content does not match intent")

            # 7. 安全清理原始 staging 文件（仅当 staging 路径仍指向同一 inode 时才 unlink，绝不删除替换文件）
            if staging_path.exists() and not staging_path.is_symlink():
                try:
                    cur_stat = staging_path.stat()
                    if cur_stat.st_dev == validated_stat.st_dev and cur_stat.st_ino == validated_stat.st_ino:
                        staging_path.unlink()
                except Exception:
                    pass

            return final_file.name

        except FileExistsError:
            # 目标文件预存在处理
            existing_data = None
            try:
                with open(final_file, "r", encoding="utf-8") as f:
                    existing_data = json.load(f)
            except Exception:
                existing_data = None

            # 仅当 staging 依然为原始 inode 时清理
            if staging_path.exists() and not staging_path.is_symlink():
                try:
                    cur_stat = staging_path.stat()
                    if cur_stat.st_dev == validated_stat.st_dev and cur_stat.st_ino == validated_stat.st_ino:
                        staging_path.unlink()
                except Exception:
                    pass

            if existing_data == intent:
                return final_file.name
            else:
                raise IntentIdConflict(f"Intent ID conflict for {intent_id}: target file exists with different content.")
        except IntentIdConflict:
            raise
        except Exception as e:
            # 发生任何后置验证失败或其他异常，回滚删除由本次调用创建的 final_file
            if created_final and final_file.exists():
                try:
                    final_file.unlink()
                except Exception:
                    pass
            raise TaskPersistenceError(f"Failed to publish staging file for {intent_id}: {e}") from e

    def persist(self, intent: Dict[str, Any]) -> str:
        """从 dict 生成 staging 临时文件并原子发布为 TaskIntent 文件"""
        intent_id = intent.get("intent_id")
        if not validate_intent_id(intent_id):
            raise TaskPersistenceError(f"Invalid intent_id for persist: {intent_id}")
        staging_file = self.create_staging(intent)
        return self.publish_staging(staging_file, intent)

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