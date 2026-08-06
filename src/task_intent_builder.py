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
from .id_sequence import next_daily_id, validate_intent_id, validate_task_id, validate_task_id_for_task_type
from .knowledge_retriever import KnowledgeBase
from .result_paths import get_task_dir
from .simulated_time import get_current_datetime

BEIJING_TZ = timezone(timedelta(hours=8))
TASK_ALLOWED_ROBOT_TYPES = {
    "pipeline_inspection": {"observation_rov", "auv"},
    "pipeline_burial": {"work_class_rov"},
    "tree_valve_operation": {"work_class_rov"},
    "valve_operation": {"work_class_rov"},
}
VALID_ROBOT_TYPES = {"observation_rov", "work_class_rov", "auv"}


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


def validate_uuid4(val: Any) -> bool:
    """验证值是否为符合规范的 UUIDv4 字符串 (必须为规范小写)。"""
    if type(val) is not str or not val:
        return False
    try:
        parsed = uuid.UUID(val)
        return parsed.version == 4 and str(parsed) == val
    except (ValueError, TypeError, AttributeError):
        return False


def _is_exact_schema_version(val: Any, expected: int) -> bool:
    """严格判断值是否为精确整数类型且数值等于 expected (排除 bool、float 及 None)。"""
    return type(val) is int and val == expected


def validate_task_intent_v1(intent: dict) -> bool:
    """v1 历史 TaskIntent 结构校验：schema_version 缺失或等于精确整数 1，internal_id 与 task_id 必须同时不存在。"""
    if not isinstance(intent, dict):
        return False
    if "schema_version" in intent:
        if not _is_exact_schema_version(intent["schema_version"], 1):
            return False
    if "internal_id" in intent or "task_id" in intent:
        return False
    intent_id = intent.get("intent_id")
    if not validate_intent_id(intent_id):
        return False
    top_task_type = intent.get("task_type")
    if top_task_type not in TASK_ALLOWED_ROBOT_TYPES:
        return False
    priority = intent.get("priority")
    if isinstance(priority, bool) or not isinstance(priority, int):
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
    if task_info.get("type") != top_task_type:
        return False
    eq_info = intent.get("equipment")
    if not isinstance(eq_info, dict) or "robot_type" not in eq_info or "payload" not in eq_info or "support_vessel" not in eq_info:
        return False
    robot_type = eq_info.get("robot_type")
    allowed_robots = TASK_ALLOWED_ROBOT_TYPES.get(top_task_type, set())
    if robot_type not in allowed_robots:
        return False
    cond_info = intent.get("conditions")
    if not isinstance(cond_info, dict):
        return False
    return True


def validate_task_intent_v2(intent: dict, task_schemas: dict | None = None) -> bool:
    """v2 TaskIntent 结构与类别编号权威校验器：schema_version 必须为精确整数 2，task_schemas 必传，internal_id (UUIDv4) 与 task_id (前缀匹配) 必填。"""
    if not isinstance(intent, dict):
        return False
    if not _is_exact_schema_version(intent.get("schema_version"), 2):
        return False
    if task_schemas is None:
        return False
    internal_id = intent.get("internal_id")
    if not validate_uuid4(internal_id):
        return False
    task_id = intent.get("task_id")
    if not validate_task_id(task_id):
        return False
    intent_id = intent.get("intent_id")
    if not validate_intent_id(intent_id):
        return False
    top_task_type = intent.get("task_type")
    if top_task_type not in TASK_ALLOWED_ROBOT_TYPES:
        return False
    rev_map = {"pipeline_inspection": "pipeline_inspection", "pipeline_burial": "pipeline_burial", "valve_operation": "tree_valve_operation"}
    task_type_key = intent.get("task_type_key") or rev_map.get(top_task_type, top_task_type)
    if not validate_task_id_for_task_type(task_id, task_type_key, task_schemas):
        return False
    priority = intent.get("priority")
    if isinstance(priority, bool) or not isinstance(priority, int):
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
    if task_info.get("type") != top_task_type:
        return False
    eq_info = intent.get("equipment")
    if not isinstance(eq_info, dict) or "robot_type" not in eq_info or "payload" not in eq_info or "support_vessel" not in eq_info:
        return False
    robot_type = eq_info.get("robot_type")
    allowed_robots = TASK_ALLOWED_ROBOT_TYPES.get(top_task_type, set())
    if robot_type not in allowed_robots:
        return False
    cond_info = intent.get("conditions")
    if not isinstance(cond_info, dict):
        return False
    return True


def validate_task_intent(intent: Any, task_schemas: dict | None = None) -> bool:
    """权威完整 TaskIntent 结构与交叉约束校验器 (根据 schema_version 显式分派)"""
    if not isinstance(intent, dict):
        return False
    if "schema_version" not in intent:
        return validate_task_intent_v1(intent)
    ver = intent["schema_version"]
    if _is_exact_schema_version(ver, 1):
        return validate_task_intent_v1(intent)
    elif _is_exact_schema_version(ver, 2):
        return validate_task_intent_v2(intent, task_schemas)
    return False


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
        validation_result: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """prepare() 不预留、不上盘、不修改 task_id 与 internal_id；若 task_id 或 internal_id 缺失或非法，fail closed。注意：若 intent_id 尚未指定，本函数会为 TaskIntent 预留并上盘 counter 生成 intent_id。"""
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

        val_dict = {}
        if validation_result is not None:
            if hasattr(validation_result, "overall_status"):
                state_snap = getattr(validation_result, "state_snapshot", None) or {}
                val_dict = {
                    "overall_status": getattr(validation_result, "overall_status", "valid"),
                    "task_version": getattr(validation_result, "task_version", 1),
                    "validation_version": getattr(validation_result, "validation_version", 1),
                    "validated_at": getattr(validation_result, "validated_at", ""),
                    "status_ref": state_snap.get("status_ref") if isinstance(state_snap, dict) else None,
                    "state_version": state_snap.get("state_version") if isinstance(state_snap, dict) else None,
                    "state_updated_at": state_snap.get("updated_at") if isinstance(state_snap, dict) else None,
                    "violations": [v.constraint_id for v in getattr(validation_result, "violations", [])],
                }
            elif isinstance(validation_result, dict):
                val_dict = validation_result

        is_future = val_dict.get("overall_status") == "pending_runtime_validation"

        conditions = {
            "validation": val_dict,
            "runtime_validation": {
                "required": is_future,
                "status": "pending_runtime_validation" if is_future else "completed",
            }
        }

        task_id = built_json.get("task_id") or task_state.get("task_id")
        if not task_id:
            raise TaskPersistenceError(f"TaskIntent prepare 失败：task_state 与 built_json 中缺少有效的 task_id。")

        cand_internal = built_json.get("internal_id") or task_state.get("internal_id")
        if not cand_internal or not validate_uuid4(cand_internal):
            raise TaskPersistenceError(f"TaskIntent prepare 失败：task_state 与 built_json 中缺少有效的 internal_id UUIDv4: {cand_internal}")
        internal_id = cand_internal

        res = {
            "schema_version": 2,
            "internal_id": internal_id,
            "task_id": task_id,
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
        self._validate_intent(res)
        return res

    def create_staging(self, intent: Dict[str, Any]) -> Path:
        """创建临时 staging 任务文件"""
        self._validate_intent(intent)
        intent_id = intent.get("intent_id")
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
        self._validate_intent(intent)
        intent_id = intent.get("intent_id")

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

    def _normalize_task_time(self, value: Optional[str]) -> Optional[str]:
        if not value:
            return None

        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise TaskPersistenceError(f"非法任务时间格式: {value}") from exc

        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=BEIJING_TZ)

        return parsed.isoformat(timespec="seconds")

    def _resolve_output_task_type(self, task_type_key: str) -> str:
        mapping = {
            "pipeline_inspection": "pipeline_inspection",
            "pipeline_burial": "pipeline_burial",
            "tree_valve_operation": "valve_operation",
            "valve_operation": "valve_operation",
        }
        output_type = mapping.get(task_type_key)
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

        if not unit_selector and not variant_selector:
            raise TaskPersistenceError("缺少可解析的机器人型号或单机编号")

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
            if not rov:
                family_info = self.kb.resolve_robot_family(str(variant_selector), task_type_key)
                if family_info:
                    robot_class = family_info.get("robot_class")
                    if robot_class:
                        rov = {"robot_class": robot_class}

        if rov is None:
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
        if task_type_key in ("pipeline_inspection", "pipeline_burial"):
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

        if intent.get("schema_version") != 2:
            raise TaskPersistenceError(f"TaskIntent schema_version 必须为 2: {intent.get('schema_version')}")

        required_keys = {
            "schema_version",
            "internal_id",
            "task_id",
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

        internal_id = intent.get("internal_id")
        if not validate_uuid4(internal_id):
            raise TaskPersistenceError(f"internal_id 非法或非有效 UUIDv4: {internal_id}")

        top_task_type = intent.get("task_type")
        task_type_key = intent.get("task_type_key")
        if not task_type_key:
            rev_map = {"pipeline_inspection": "pipeline_inspection", "pipeline_burial": "pipeline_burial", "valve_operation": "tree_valve_operation"}
            task_type_key = rev_map.get(top_task_type, top_task_type)

        if not validate_task_id_for_task_type(intent.get("task_id"), task_type_key, self.kb.task_schemas):
            raise TaskPersistenceError(f"task_id 非法或与任务类型 {task_type_key!r} 前缀不匹配: {intent.get('task_id')}")

        if not validate_intent_id(intent.get("intent_id")):
            raise TaskPersistenceError(f"intent_id 非法: {intent.get('intent_id')}")

        top_task_type = intent.get("task_type")
        if top_task_type not in TASK_ALLOWED_ROBOT_TYPES:
            raise TaskPersistenceError(f"非法输出任务类型: {top_task_type}")

        if intent.get("task", {}).get("type") != top_task_type:
            raise TaskPersistenceError("task.type 与顶层 task_type 不一致")

        priority = intent.get("priority")
        if isinstance(priority, bool) or not isinstance(priority, int) or priority not in range(1, 11):
            raise TaskPersistenceError("priority 超出范围")

        for section in ("time", "location", "task", "equipment", "conditions"):
            if not isinstance(intent.get(section), dict):
                raise TaskPersistenceError(f"TaskIntent section must be dict: {section}")

        robot_type = intent["equipment"].get("robot_type")
        allowed_robots = TASK_ALLOWED_ROBOT_TYPES.get(top_task_type, set())
        if robot_type not in allowed_robots:
            raise TaskPersistenceError(f"任务类型 {top_task_type} 不支持机器人类型 {robot_type}")
