"""
task_intent_builder.py — 生成符合 TaskIntent 规范的 JSON 文件
"""
import fcntl
import json
import os
import re
import stat
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from .exceptions import IntentIdConflict, TaskPersistenceError
from .id_sequence import next_daily_id, validate_intent_id
from .knowledge_retriever import KnowledgeBase
from .result_paths import get_task_dir
from .simulated_time import get_current_datetime


TASK_TIMEZONE = timezone(timedelta(hours=8))

TASK_TYPE_OUTPUT_MAP = {
    "pipeline_inspection": "pipeline_inspection",
    "tree_valve_operation": "valve_operation",
}

VALID_ROBOT_TYPES = {
    "observation_rov",
    "work_class_rov",
    "auv",
}


class TaskPublishLock:
    """进程间与线程间任务发布排他锁"""
    def __init__(self, task_dir: Path):
        self.lock_path = task_dir / ".task_intent_publish.lock"
        self._fd = None

    def __enter__(self):
        try:
            self._fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(self._fd, fcntl.LOCK_EX)
        except Exception as e:
            if self._fd is not None:
                try:
                    os.close(self._fd)
                except Exception:
                    pass
            raise TaskPersistenceError(f"Failed to acquire publish lock: {e}") from e
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
                os.close(self._fd)
            except Exception:
                pass
            self._fd = None


def _atomic_commit_noreplace(temp_file: Path, final_file: Path) -> None:
    """原子提交临时文件为正式文件，已存在时拒绝覆盖"""
    if final_file.exists():
        raise FileExistsError(f"Final file already exists: {final_file}")

    try:
        os.link(temp_file, final_file)
        try:
            temp_file.unlink()
        except Exception:
            pass
    except FileExistsError:
        raise
    except Exception as e:
        raise TaskPersistenceError(f"Atomic commit failed: {e}") from e


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

        priority = 1 if mode == "emergency" else 7

        start_time = self._normalize_task_time(built_json.get("start_time"))
        end_time = self._normalize_task_time(built_json.get("end_time"))

        oilfield_name = None
        water_depth = built_json.get("water_depth")
        coords = (
            built_json.get("start_point")
            or built_json.get("oilfield_coordinates")
            or built_json.get("cable_position")
        )
        if coords and isinstance(coords, dict):
            lat = coords.get("lat")
            lon = coords.get("lon")
            if lat is not None and lon is not None:
                area = self.kb.get_environment_for_coords({"lat": lat, "lon": lon})
                if area:
                    oilfield_name = area.get("name")
        if not oilfield_name:
            oilfield_name = task_state.get("oilfield_name")

        top_task_type = self._resolve_output_task_type(task_type_key)
        details = self._build_details(task_type_key, task_state, built_json)
        robot_type = self._resolve_robot_type(task_state, built_json, task_type_key)

        payload = built_json.get("payload", [])
        if not isinstance(payload, list):
            payload = [payload] if payload else []
        support_vessel_name = built_json.get("support_vessel")
        support_vessel = {
            "name": support_vessel_name,
            "latitude": None,
            "longitude": None,
        }

        intent = {
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
            "conditions": {},
        }
        self._validate_intent(intent)
        return intent

    def create_staging(self, intent: Dict[str, Any]) -> Path:
        """创建临时 staging 任务文件"""
        self._validate_intent(intent)
        intent_id = intent.get("intent_id")
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
        """使用跨进程排他锁、认领隔离与内存可信原子提交发布 staging 为正式 JSON"""
        self._validate_intent(intent)
        intent_id = intent.get("intent_id")

        try:
            staging_path = Path(staging_file)
        except Exception as e:
            raise TaskPersistenceError(f"Invalid staging_file path: {e}") from e

        task_dir = get_task_dir(create=True)
        resolved_task_dir = task_dir.resolve()

        if not staging_path.exists():
            raise TaskPersistenceError(f"Staging file does not exist: {staging_path}")
        if staging_path.is_symlink():
            raise TaskPersistenceError(f"Staging file cannot be a symlink: {staging_path}")
        if not staging_path.is_file():
            raise TaskPersistenceError(f"Staging file is not a regular file: {staging_path}")

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

        expected_pattern = rf"^task_intent_{re.escape(intent_id)}\.staging_[0-9]+_[0-9]+_[0-9a-f]{{8}}$"
        if not re.fullmatch(expected_pattern, staging_path.name):
            raise TaskPersistenceError(
                f"Staging filename '{staging_path.name}' does not match controlled format pattern for intent_id '{intent_id}'"
            )

        with TaskPublishLock(task_dir):
            final_file = task_dir / f"task_intent_{intent_id}.json"
            if resolved_task_dir not in final_file.resolve().parents:
                raise TaskPersistenceError(f"Path traversal detected for final file: {final_file}")

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

            if (
                st_before.st_dev != st_after.st_dev
                or st_before.st_ino != st_after.st_ino
                or st_before.st_size != st_after.st_size
                or st_before.st_mtime_ns != st_after.st_mtime_ns
            ):
                raise TaskPersistenceError("Staging file was modified during verification")

            if not isinstance(staging_data, dict):
                raise TaskPersistenceError("Staging JSON top-level must be a dictionary")

            self._validate_intent(staging_data)
            if staging_data != intent:
                raise TaskPersistenceError("Staging JSON content does not match expected intent data")

            if final_file.exists():
                try:
                    with open(final_file, "r", encoding="utf-8") as f:
                        existing_data = json.load(f)
                    if existing_data == intent:
                        if staging_path.exists() and not staging_path.is_symlink():
                            try:
                                cur_stat = staging_path.stat()
                                if cur_stat.st_dev == validated_stat.st_dev and cur_stat.st_ino == validated_stat.st_ino:
                                    staging_path.unlink()
                            except Exception:
                                pass
                        return final_file.name
                    if staging_path.exists() and not staging_path.is_symlink():
                        try:
                            cur_stat = staging_path.stat()
                            if cur_stat.st_dev == validated_stat.st_dev and cur_stat.st_ino == validated_stat.st_ino:
                                staging_path.unlink()
                        except Exception:
                            pass
                    raise IntentIdConflict(f"Intent ID conflict for {intent_id}: target file exists with different content.")
                except IntentIdConflict:
                    raise
                except Exception:
                    raise IntentIdConflict(f"Intent ID conflict for {intent_id}: target file exists.")

            claim_file = task_dir / f".claimed_{intent_id}_{uuid.uuid4().hex}"
            claimed_owned = False
            try:
                os.rename(staging_path, claim_file)
                claimed_owned = True
                claimed_stat = os.stat(claim_file)
                if claimed_stat.st_dev != validated_stat.st_dev or claimed_stat.st_ino != validated_stat.st_ino:
                    raise TaskPersistenceError("Staging file inode mismatch upon claim")
            except Exception as e:
                if claimed_owned and claim_file.exists():
                    try:
                        claim_file.unlink()
                    except Exception:
                        pass
                raise TaskPersistenceError(f"Failed to claim staging file for {intent_id}: {e}") from e

            tmp_file = task_dir / f".tmp_publish_{intent_id}_{uuid.uuid4().hex}"
            tmp_owned = False
            try:
                tmp_fd = os.open(tmp_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                tmp_owned = True
                try:
                    content_bytes = json.dumps(intent, ensure_ascii=False, indent=2).encode("utf-8")
                    os.write(tmp_fd, content_bytes)
                    os.fsync(tmp_fd)
                finally:
                    os.close(tmp_fd)

                with open(tmp_file, "r", encoding="utf-8") as f:
                    written_data = json.load(f)
                if written_data != intent:
                    raise TaskPersistenceError("Temp file written content mismatch")

                _atomic_commit_noreplace(tmp_file, final_file)
                tmp_owned = False

                if claimed_owned and claim_file.exists():
                    try:
                        claim_file.unlink()
                    except Exception:
                        pass
                    claimed_owned = False

                return final_file.name

            except FileExistsError:
                if claimed_owned and claim_file.exists():
                    try:
                        claim_file.unlink()
                    except Exception:
                        pass
                raise IntentIdConflict(f"Intent ID conflict for {intent_id}: target file exists.")
            except IntentIdConflict:
                if claimed_owned and claim_file.exists():
                    try:
                        claim_file.unlink()
                    except Exception:
                        pass
                raise
            except Exception as e:
                if tmp_owned and tmp_file.exists():
                    try:
                        tmp_file.unlink()
                    except Exception:
                        pass
                if claimed_owned and claim_file.exists():
                    try:
                        claim_file.unlink()
                    except Exception:
                        pass
                raise TaskPersistenceError(f"Failed to publish staging file for {intent_id}: {e}") from e

    def persist(self, intent: Dict[str, Any]) -> str:
        """从 dict 生成 staging 临时文件并原子发布为 TaskIntent 文件"""
        self._validate_intent(intent)
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

    def _normalize_task_time(self, value: Optional[str]) -> Optional[str]:
        if not value:
            return None

        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise TaskPersistenceError(f"非法任务时间格式: {value}") from exc

        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=TASK_TIMEZONE)

        return parsed.isoformat(timespec="seconds")

    def _resolve_output_task_type(self, task_type_key: str) -> str:
        output_type = TASK_TYPE_OUTPUT_MAP.get(task_type_key)
        if output_type is None:
            raise TaskPersistenceError(f"不支持的 task_type_key: {task_type_key}")
        return output_type

    def _resolve_robot_type(
        self,
        task_state: Dict[str, Any],
        built_json: Dict[str, Any],
        task_type_key: str,
    ) -> str:
        """由已选型号或单机的知识库 robot_class 生成 TaskIntent robot_type。"""
        unit_selector = built_json.get("equipment_unit_id") or task_state.get("equipment_unit_id")
        variant_selector = built_json.get("equipment_type") or task_state.get("equipment_type")

        rov = None
        if unit_selector:
            resolved_unit = self.kb.resolve_robot_unit(
                str(unit_selector),
                task_type_key,
                str(variant_selector) if variant_selector else None,
            )
            if not resolved_unit:
                raise TaskPersistenceError(f"无法解析具体机器人编号: {unit_selector}")
            rov = resolved_unit.get("robot")
        elif variant_selector:
            rov = self.kb.get_rov_for_task(str(variant_selector), task_type_key)

        if rov is None:
            legacy_map = {
                "观察级ROV": "observation_rov",
                "工作级ROV": "work_class_rov",
                "管缆埋设机器人": "work_class_rov",
                "海底拖拉机": "work_class_rov",
                "AUV": "auv",
                "调查型AUV": "auv",
            }
            legacy_type = legacy_map.get(str(variant_selector))
            if legacy_type:
                return legacy_type
            raise TaskPersistenceError(f"无法根据设备信息确定 robot_type: {variant_selector}")

        class_map = {
            "observation_rov": "observation_rov",
            "work_class_rov": "work_class_rov",
            "cable_burial_robot": "work_class_rov",
            "auv": "auv",
        }
        robot_class = rov.get("robot_class")
        robot_type = class_map.get(robot_class)
        if not robot_type:
            raise TaskPersistenceError(f"未知 robot_class: {robot_class}")
        return robot_type

    def _build_details(
        self,
        task_type_key: str,
        task_state: Dict[str, Any],
        built_json: Dict[str, Any],
    ) -> Dict[str, Any]:
        if task_type_key == "pipeline_inspection":
            return self._build_pipeline_inspection_details(task_state, built_json)
        if task_type_key == "tree_valve_operation":
            return self._build_tree_valve_operation_details(task_state, built_json)
        raise TaskPersistenceError(f"没有为任务类型 {task_type_key} 配置 details 构建器")

    def _build_pipeline_inspection_details(
        self,
        task_state: Dict[str, Any],
        built_json: Dict[str, Any],
    ) -> Dict[str, Any]:
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

    def _build_tree_valve_operation_details(
        self,
        task_state: Dict[str, Any],
        built_json: Dict[str, Any],
    ) -> Dict[str, Any]:
        wellhead_id = built_json.get("wellhead_id") or task_state.get("wellhead_id")
        oilfield_coords = built_json.get("oilfield_coordinates") or task_state.get("oilfield_coordinates")
        target = None
        if oilfield_coords and isinstance(oilfield_coords, dict):
            target = {
                "latitude": oilfield_coords.get("lat"),
                "longitude": oilfield_coords.get("lon"),
            }

        return {
            "wellhead_id": wellhead_id,
            "target": target,
            "hole_positions": [],
        }

    def _validate_intent(self, intent: Dict[str, Any]) -> None:
        if not isinstance(intent, dict):
            raise TaskPersistenceError("TaskIntent must be a dictionary")

        required_keys = {
            "intent_id",
            "task_type",
            "priority",
            "time",
            "location",
            "task",
            "equipment",
            "conditions",
        }
        missing = required_keys - intent.keys()
        if missing:
            raise TaskPersistenceError(f"TaskIntent 缺少字段: {sorted(missing)}")

        if not validate_intent_id(intent.get("intent_id")):
            raise TaskPersistenceError(f"intent_id 非法: {intent.get('intent_id')}")

        if intent.get("task_type") not in set(TASK_TYPE_OUTPUT_MAP.values()):
            raise TaskPersistenceError(f"非法输出任务类型: {intent.get('task_type')}")

        if intent.get("task", {}).get("type") != intent.get("task_type"):
            raise TaskPersistenceError("task.type 与顶层 task_type 不一致")

        priority = intent.get("priority")
        if isinstance(priority, bool) or not isinstance(priority, int) or priority not in range(1, 11):
            raise TaskPersistenceError("priority 超出范围")

        for section in ("time", "location", "task", "equipment", "conditions"):
            if not isinstance(intent.get(section), dict):
                raise TaskPersistenceError(f"TaskIntent section must be dict: {section}")

        robot_type = intent["equipment"].get("robot_type")
        if robot_type not in VALID_ROBOT_TYPES:
            raise TaskPersistenceError(f"非法 robot_type: {robot_type}")
