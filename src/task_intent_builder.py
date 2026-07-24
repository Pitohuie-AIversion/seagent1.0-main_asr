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
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from .exceptions import IntentIdConflict, TaskPersistenceError
from .id_sequence import next_daily_id, validate_intent_id
from .knowledge_retriever import KnowledgeBase
from .result_paths import get_task_dir
from .simulated_time import get_current_datetime


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

        with TaskPublishLock(task_dir):
            try:
                flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, 'O_NOFOLLOW', 0)
                fd = os.open(staging_file, flags, 0o600)
                try:
                    content_bytes = json.dumps(intent, ensure_ascii=False, indent=2).encode("utf-8")
                    os.write(fd, content_bytes)
                    os.fsync(fd)
                finally:
                    os.close(fd)
                return staging_file
            except Exception as e:
                raise TaskPersistenceError(f"Failed to create staging file for {intent_id}: {e}") from e

def validate_task_intent(intent: Any) -> bool:
    """权威完整 TaskIntent 结构校验器"""
    if not isinstance(intent, dict):
        return False
    intent_id = intent.get("intent_id")
    if not validate_intent_id(intent_id):
        return False
    top_task_type = intent.get("task_type")
    if top_task_type not in ("pipeline_inspection", "tree_valve_operation", "valve_operation"):
        return False
    if not isinstance(intent.get("priority"), int):
        return False
    time_info = intent.get("time")
    if not isinstance(time_info, dict) or "start" not in time_info or "end" not in time_info:
        return False
    loc_info = intent.get("location")
    if not isinstance(loc_info, dict) or "oilfield" not in loc_info or "water_depth_m" not in loc_info:
        return False
    task_info = intent.get("task")
    if not isinstance(task_info, dict) or "type" not in task_info or "details" not in task_info:
        return False
    eq_info = intent.get("equipment")
    if not isinstance(eq_info, dict) or "robot_type" not in eq_info or "payload" not in eq_info or "support_vessel" not in eq_info:
        return False
    cond_info = intent.get("conditions")
    if not isinstance(cond_info, dict):
        return False
    return True


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

        with TaskPublishLock(task_dir):
            try:
                flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, 'O_NOFOLLOW', 0)
                fd = os.open(staging_file, flags, 0o600)
                try:
                    content_bytes = json.dumps(intent, ensure_ascii=False, indent=2).encode("utf-8")
                    os.write(fd, content_bytes)
                    os.fsync(fd)
                finally:
                    os.close(fd)
                return staging_file
            except Exception as e:
                raise TaskPersistenceError(f"Failed to create staging file for {intent_id}: {e}") from e

    def publish_staging(self, staging_file: Path | str, intent: Dict[str, Any]) -> str:
        """使用跨进程排他锁、认领隔离与内存可信原子提交发布 staging 为正式 JSON"""
        if not isinstance(intent, dict):
            raise TaskPersistenceError("intent must be a dictionary")
        intent_id = intent.get("intent_id")
        if not validate_intent_id(intent_id):
            raise TaskPersistenceError(f"Invalid intent_id for publish_staging: {intent_id}")

        try:
            staging_path = Path(staging_file)
        except Exception as e:
            raise TaskPersistenceError(f"Invalid staging_file path: {e}") from e

        task_dir = get_task_dir(create=True)
        resolved_task_dir = task_dir.resolve()

        # 1. 优先校验 staging 路径合法性与文件名格式
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

        m = re.match(r"^task_intent_[^.]+\.staging_([0-9]+)_", staging_path.name)
        if m:
            owner_pid = int(m.group(1))
            if owner_pid != os.getpid():
                raise TaskPersistenceError(f"Staging file owner PID {owner_pid} does not match current process PID {os.getpid()}")

        txid = uuid.uuid4().hex

        with TaskPublishLock(task_dir):
            final_file = task_dir / f"task_intent_{intent_id}.json"
            if resolved_task_dir not in final_file.resolve().parents:
                raise TaskPersistenceError(f"Path traversal detected for final file: {final_file}")

            # 2. 如果 final_file 已存在：无条件拒绝发布！不得尝试按路径强删 staging
            if final_file.exists() or final_file.is_symlink():
                raise IntentIdConflict(f"Target official file already exists: {final_file.name}")

            # 3. 打开 staging 文件描述符 (O_NOFOLLOW + O_RDONLY)，用 fstat 强绑定 inode
            try:
                open_flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
                st_fd = os.open(resolved_staging, open_flags)
            except Exception as e:
                raise TaskPersistenceError(f"Failed to open staging file descriptor safely: {e}") from e

            try:
                validated_stat = os.fstat(st_fd)
                if not stat.S_ISREG(validated_stat.st_mode):
                    raise TaskPersistenceError("Staging file descriptor is not a regular file")

                with os.fdopen(st_fd, "r", encoding="utf-8", closefd=True) as f:
                    staging_data = json.load(f)
            except TaskPersistenceError:
                raise
            except Exception as e:
                raise TaskPersistenceError(f"Failed to parse staging JSON content: {e}") from e

            if not isinstance(staging_data, dict):
                raise TaskPersistenceError("Staging JSON top-level must be a dictionary")

            st_intent_id = staging_data.get("intent_id")
            if not validate_intent_id(st_intent_id):
                raise TaskPersistenceError(f"Invalid intent_id inside staging JSON: {st_intent_id}")

            if st_intent_id != intent_id or staging_data != intent:
                raise TaskPersistenceError("Staging JSON content does not match expected intent data")

            # 4. 安全认领 Staging (Claiming) 到专用隔离路径
            claim_file = task_dir / f".claimed_{intent_id}_{txid}"
            try:
                os.rename(staging_path, claim_file)
            except Exception as e:
                raise TaskPersistenceError(f"Failed to claim staging file for {intent_id}: {e}") from e

            # 5. 从受信任内存 intent 原子创建私有 0600 临时文件并写入
            tmp_file = task_dir / f".tmp_publish_{intent_id}_{txid}"
            tmp_stat = None
            try:
                tmp_flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, 'O_NOFOLLOW', 0)
                tmp_fd = os.open(tmp_file, tmp_flags, 0o600)
                try:
                    content_bytes = json.dumps(intent, ensure_ascii=False, indent=2).encode("utf-8")
                    os.write(tmp_fd, content_bytes)
                    os.fsync(tmp_fd)
                finally:
                    os.close(tmp_fd)

                read_flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
                t_fd = os.open(tmp_file, read_flags)
                try:
                    tmp_stat = os.fstat(t_fd)
                    with os.fdopen(t_fd, "r", encoding="utf-8", closefd=True) as f:
                        written_data = json.load(f)
                except Exception as e:
                    raise TaskPersistenceError(f"Failed to read back written temp file: {e}") from e

                if written_data != intent:
                    raise TaskPersistenceError("Temp file written content mismatch")

                # 6. 原子 no-overwrite 提交正式文件
                _atomic_commit_noreplace(tmp_file, final_file)

                # 7. 强制执行文件与目录 fsync，异常时 fail closed 抛出 TaskPersistenceError
                try:
                    f_fd = os.open(final_file, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
                    try:
                        os.fsync(f_fd)
                    finally:
                        os.close(f_fd)
                except Exception as e:
                    raise TaskPersistenceError(f"File fsync failed for {final_file.name}: {e}") from e

                try:
                    d_fd = os.open(task_dir, os.O_RDONLY)
                    try:
                        os.fsync(d_fd)
                    finally:
                        os.close(d_fd)
                except Exception as e:
                    raise TaskPersistenceError(f"Directory fsync failed for {task_dir}: {e}") from e

                # 8. 提交成功后安全解绑定清理
                return final_file.name

            except FileExistsError:
                raise IntentIdConflict(f"Intent ID conflict for {intent_id}: target file exists.")
            except IntentIdConflict:
                raise
            except Exception as e:
                if tmp_file and tmp_file.exists() and tmp_stat:
                    try:
                        c_fd = os.open(tmp_file, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
                        try:
                            c_stat = os.fstat(c_fd)
                            if (c_stat.st_dev == tmp_stat.st_dev and
                                c_stat.st_ino == tmp_stat.st_ino and
                                c_stat.st_size == tmp_stat.st_size):
                                os.unlink(tmp_file)
                        finally:
                            os.close(c_fd)
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