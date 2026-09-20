"""
src/slot_list_mutation.py - 槽位列表增量修改与载荷变异引擎

职责：
1. 载荷规范化键解析与展示后缀（如“（可选）”）消除；
2. 针对列表类型槽位（如 payload）的增量变异操作（add, remove, replace, set, clear）；
3. 知识库 allowed_values 优先校验与 payload_catalog 别名推导；
4. 当前执行机器人机载默认工具（onboard_payloads）与领域近义词去重保护；
5. 列表操作原子回滚与失败上下文构建。
"""

from __future__ import annotations

import copy
import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from .slot_store import Slot

logger = logging.getLogger("backend.slot_store")

_OPTIONAL_SUFFIXES = ("（可选）", "(可选)")

DOMAIN_SYNONYMS = [
    (
        {"单目水下成像系统", "水下成像系统", "云台摄像机"},
        ["高清水下摄像机", "云台摄像", "水下成像", "摄像机", "led", "照明"],
    ),
    ({"fls声呐系统", "前视声呐系统", "前视声呐"}, ["fls声呐", "前视声呐"]),
    ({"ins惯性导航系统"}, ["ins", "惯导", "惯性导航"]),
    ({"dvl测速系统", "dvl多普勒测速仪", "dvl多普勒测速系统"}, ["dvl", "多普勒"]),
    ({"usbl定位系统", "usbl定位设备"}, ["usbl", "超短基线"]),
    ({"深度计", "深度传感器"}, ["深度计", "深度传感器"]),
    ({"高度计"}, ["高度计"]),
    ({"履带模块"}, ["履带模块"]),
]


def normalize_payload_match_key(value: str) -> str:
    """消除首尾空格、小写化并去除“（可选）”等展示后缀，用于严格载荷等价判定。"""
    text = str(value or "").strip().lower().replace(" ", "")
    for suffix in _OPTIONAL_SUFFIXES:
        if text.endswith(suffix):
            text = text[:-len(suffix)]
            break
    return text


class SlotListMutationEngine:
    """槽位列表增量变异引擎"""

    def __init__(self, store: Any = None) -> None:
        self.store = store

    @property
    def kb(self) -> Any:
        return getattr(self.store, "kb", None)

    def apply_list_mutation(
        self,
        new_slots: Dict[str, Any],
        mutation: Dict[str, Any],
        required_schema: Optional[List[Dict[str, Any]]] = None,
        payload_catalog: Optional[Dict[str, Any]] = None,
        allowed_values_resolver: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """统一列表增量修改入口（add/remove/replace/clear/set）。"""
        from .slot_store import Slot

        field_name = mutation.get("field", "payload")
        op = mutation.get("operation")
        raw_text = mutation.get("raw_text", "")
        confidence = mutation.get("confidence", 0.95)
        source = mutation.get("source", "user_input")

        schema_field = None
        if isinstance(required_schema, dict):
            if required_schema.get("key") == field_name:
                schema_field = required_schema
            elif "fields" in required_schema and isinstance(required_schema["fields"], list):
                schema_field = next(
                    (f for f in required_schema["fields"] if isinstance(f, dict) and f.get("key") == field_name),
                    None,
                )
        elif isinstance(required_schema, list):
            schema_field = next(
                (f for f in required_schema if isinstance(f, dict) and f.get("key") == field_name),
                None,
            )
        if required_schema is not None and schema_field is None:
            return {
                "success": False,
                "changed": False,
                "operation": op,
                "old_value": copy.deepcopy(
                    new_slots.get(field_name).value
                    if new_slots.get(field_name) is not None
                    else []
                ),
                "new_value": copy.deepcopy(
                    new_slots.get(field_name).value
                    if new_slots.get(field_name) is not None
                    else []
                ),
                "error": f"列表字段 '{field_name}' 不属于当前任务 schema",
            }

        if payload_catalog is None:
            if self.kb and hasattr(self.kb, "assets") and isinstance(self.kb.assets, dict):
                payload_catalog = self.kb.assets.get("payload_catalog", {})
            else:
                try:
                    from src.extraction.extractor import _load_payload_catalog
                    payload_catalog = _load_payload_catalog()
                except Exception:
                    payload_catalog = {}

        allowed_values = []
        constrained_field = bool(
            schema_field
            and (
                "allowed_values" in schema_field
                or schema_field.get("allowed_values_ref")
            )
        )
        if schema_field is not None:
            if allowed_values_resolver:
                allowed_values = allowed_values_resolver(schema_field) or []
            else:
                allowed_values = schema_field.get("allowed_values") or []
                if (
                    not allowed_values
                    and schema_field.get("allowed_values_ref")
                    and self.kb
                ):
                    try:
                        from src.dispatch.output_builder import OutputBuilder
                        task_type_slot = new_slots.get("task_type_key")
                        task_type_key = (
                            task_type_slot.value
                            if task_type_slot
                            and task_type_slot.status == "valid"
                            and task_type_slot.value is not None
                            else ""
                        )
                        current_state = {
                            k: v.value
                            for k, v in new_slots.items()
                            if v
                            and v.status == "valid"
                            and v.value is not None
                        }
                        allowed_values = OutputBuilder(self.kb).resolve_allowed_values(
                            schema_field,
                            str(task_type_key or ""),
                            current_state,
                        ) or []
                    except Exception as exc:
                        logger.debug(
                            "SlotStore: resolve_allowed_values fallback skipped: %s",
                            exc,
                        )

        def _resolve(item_str: str) -> Tuple[Optional[str], Optional[str]]:
            """解析项的 (catalog_id, task_canonical_name)。

            P1-1: 第一优先级：检查 item_str 是否匹配当前任务 allowed_values（允许忽略“（可选）”等受控展示后缀）。
            只有在用户输入未匹配 allowed_values 时，才退而使用 catalog 别名映射。
            """
            text = str(item_str or "").strip()
            if not text:
                return None, None

            text_key = normalize_payload_match_key(text)

            if constrained_field and not allowed_values:
                return None, None

            if allowed_values:
                for a_val in allowed_values:
                    if isinstance(a_val, str) and normalize_payload_match_key(a_val) == text_key:
                        cat_id = None
                        for c_id, info in payload_catalog.items():
                            name = info.get("name", "")
                            aliases = info.get("aliases") or []
                            if any(cand and normalize_payload_match_key(cand) == text_key for cand in [name, *aliases]):
                                cat_id = c_id
                                break
                        return cat_id, a_val

            cat_id = None
            cat_candidates = []
            for c_id, info in payload_catalog.items():
                name = info.get("name", "")
                aliases = info.get("aliases") or []
                all_cands = [name, *aliases]
                for cand in all_cands:
                    if cand and normalize_payload_match_key(cand) == text_key:
                        cat_id = c_id
                        cat_candidates = [c for c in all_cands if c]
                        break
                if cat_id:
                    break

            if not cat_candidates:
                cat_candidates = [text]

            if allowed_values:
                for cand in cat_candidates:
                    cand_key = normalize_payload_match_key(cand)
                    for a_val in allowed_values:
                        if isinstance(a_val, str) and normalize_payload_match_key(a_val) == cand_key:
                            return cat_id, a_val
                return cat_id, None

            if cat_id and payload_catalog.get(cat_id, {}).get("name"):
                return cat_id, payload_catalog[cat_id]["name"]
            return cat_id, text

        def _contains(item_list: List[str], target: str) -> bool:
            t_id, t_name = _resolve(target)
            for item in item_list:
                i_id, i_name = _resolve(item)
                if t_id and i_id and t_id == i_id:
                    return True
                if t_name and i_name and t_name.lower().replace(" ", "") == i_name.lower().replace(" ", ""):
                    return True
            return False

        def _find_index(item_list: List[str], target: str) -> int:
            t_id, t_name = _resolve(target)
            for idx, item in enumerate(item_list):
                i_id, i_name = _resolve(item)
                if t_id and i_id and t_id == i_id:
                    return idx
                if t_name and i_name and t_name.lower().replace(" ", "") == i_name.lower().replace(" ", ""):
                    return idx
            return -1

        slot = new_slots.get(field_name)
        if slot is None:
            slot = Slot(slot_name=field_name, value_type="list", status="missing")
            new_slots[field_name] = slot

        old_val = slot.value
        if isinstance(old_val, list):
            old_value = copy.deepcopy(old_val)
        elif isinstance(old_val, str) and old_val.strip():
            old_value = [old_val.strip()]
        else:
            old_value = []

        temp_list = copy.deepcopy(old_value)

        def _fail(op_name: str, err_msg: str) -> Dict[str, Any]:
            slot.raw_value = raw_text
            slot.source = source
            slot.confidence = confidence
            slot.validation_error = err_msg
            return {
                "success": False,
                "changed": False,
                "operation": op_name,
                "old_value": old_value,
                "new_value": old_value,
                "error": err_msg,
            }

        onboard_payload_keys = set()
        supported_payload_keys = set()
        onboard_catalog_ids = set()
        onboard_alias_keys = set()
        eq_slot = new_slots.get("equipment_type")
        eq_type = str(eq_slot.value if eq_slot and eq_slot.status == "valid" and eq_slot.value else "")
        if eq_type and self.kb:
            robot = self.kb.get_rov(eq_type)
            if robot:
                supported_payload_keys = {
                    normalize_payload_match_key(item)
                    for item in robot.get("supported_payloads", [])
                    if isinstance(item, str)
                }
                for ob in robot.get("onboard_payloads", []):
                    if isinstance(ob, str):
                        ob_norm = normalize_payload_match_key(ob)
                        onboard_payload_keys.add(ob_norm)
                        for c_id, info in payload_catalog.items():
                            name = info.get("name", "")
                            aliases = info.get("aliases") or []
                            cand_keys = {normalize_payload_match_key(c) for c in [name, *aliases] if c}
                            if ob_norm in cand_keys:
                                onboard_catalog_ids.add(c_id)
                                onboard_alias_keys.update(cand_keys)

        def _is_onboard(item_raw: str, cat_id: Optional[str]) -> bool:
            if not onboard_payload_keys and not onboard_catalog_ids:
                return False
            raw_norm = normalize_payload_match_key(item_raw)
            if raw_norm in onboard_payload_keys:
                return True
            # Explicit optional models (e.g. stereo/turbid-water imaging) are
            # distinct from installed hardware even when a broad alias overlaps.
            if raw_norm in supported_payload_keys:
                return False
            if cat_id and cat_id in onboard_catalog_ids:
                return True
            if raw_norm in onboard_alias_keys:
                return True
            item_cand_keys = {raw_norm}
            if cat_id and payload_catalog.get(cat_id):
                info = payload_catalog[cat_id]
                cands = [info.get("name", ""), *(info.get("aliases") or [])]
                item_cand_keys.update({normalize_payload_match_key(c) for c in cands if c})
            if bool(item_cand_keys & onboard_alias_keys):
                return True
            for targets, keywords in DOMAIN_SYNONYMS:
                if any(normalize_payload_match_key(t) in onboard_payload_keys for t in targets):
                    # A broad word inside an unknown name is not proof that
                    # the requested tool is already installed. Keep exact
                    # generic aliases and configured catalog names, but reject
                    # invented replacements rather than deleting their target.
                    if any(
                        normalize_payload_match_key(kw) == raw_norm
                        or (cat_id and kw.lower() in item_raw.lower())
                        for kw in keywords
                    ):
                        return True
            return False

        def _flatten_items(raw_items: Any) -> List[Any]:
            if isinstance(raw_items, str):
                s = raw_items.strip()
                try:
                    p = json.loads(s)
                    if isinstance(p, list):
                        raw_items = p
                    else:
                        raw_items = [s]
                except Exception:
                    try:
                        import ast
                        p = ast.literal_eval(s)
                        if isinstance(p, (list, tuple, set)):
                            raw_items = list(p)
                        else:
                            raw_items = [s]
                    except Exception:
                        raw_items = [s]
            elif not isinstance(raw_items, (list, tuple, set)):
                return []
            res = []
            for item in raw_items:
                if isinstance(item, str):
                    cleaned = item.strip(" \t\n\r'\"[]()")
                    parts = [p.strip(" \t\n\r'\"") for p in re.split(r'[,\+，、；;\n]+', cleaned) if p.strip(" \t\n\r'\"")]
                    res.extend(parts)
                else:
                    res.append(item)
            return res

        if op == "add":
            items = _flatten_items(mutation.get("items"))
            new_canonicals = []
            for item_raw in items:
                cat_id, c_name = _resolve(item_raw)
                if c_name is None:
                    if _is_onboard(item_raw, cat_id):
                        continue
                    return _fail("add", f"添加的载荷 '{item_raw}' 非法或不属于当前任务允许范围")
                if _is_onboard(c_name, cat_id) or _is_onboard(item_raw, cat_id):
                    continue
                new_canonicals.append(c_name)

            for item_to_add in new_canonicals:
                if item_to_add and not _contains(temp_list, item_to_add):
                    temp_list.append(item_to_add)
            new_value = temp_list

        elif op == "remove":
            targets = _flatten_items(mutation.get("items") or mutation.get("target_items"))
            for target_raw in targets:
                _, c_name = _resolve(target_raw)
                target_to_remove = c_name or target_raw
                idx = _find_index(temp_list, target_to_remove)
                if idx < 0:
                    raw_norm = normalize_payload_match_key(target_raw)
                    if raw_norm in onboard_payload_keys:
                        continue
                    return _fail("remove", f"待删除载荷 '{target_raw}' 不在当前列表中")
                temp_list.pop(idx)
            new_value = temp_list

        elif op == "replace":
            targets = _flatten_items(mutation.get("target_items"))
            new_items_raw = _flatten_items(mutation.get("items"))
            if not targets:
                return _fail("replace", "替换操作必须指定待替换的目标载荷；全量配置请使用 set")

            target_indices = []
            for target_raw in targets:
                _, c_name = _resolve(target_raw)
                target_to_find = c_name or target_raw
                idx = _find_index(temp_list, target_to_find)
                if idx < 0:
                    raw_norm = normalize_payload_match_key(target_raw)
                    if raw_norm in onboard_payload_keys:
                        continue
                    return _fail("replace", f"待替换的目标载荷 '{target_raw}' 不在当前列表中")
                target_indices.append(idx)

            new_canonicals = []
            for n_raw in new_items_raw:
                cat_id, n_cname = _resolve(n_raw)
                if n_cname is None:
                    if _is_onboard(n_raw, cat_id):
                        continue
                    raw_norm = normalize_payload_match_key(n_raw)
                    if raw_norm in onboard_payload_keys:
                        continue
                    return _fail("replace", f"替换的新载荷 '{n_raw}' 非法或不属于当前任务允许范围")
                if _is_onboard(n_cname, cat_id) or _is_onboard(n_raw, cat_id):
                    continue
                new_canonicals.append(n_cname)

            for idx in sorted(set(target_indices), reverse=True):
                temp_list.pop(idx)
            for item_to_add in new_canonicals:
                if item_to_add and not _contains(temp_list, item_to_add):
                    temp_list.append(item_to_add)
            new_value = temp_list

        elif op in ("set", "override"):
            items = _flatten_items(mutation.get("items"))
            new_canonicals = []
            for item_raw in items:
                cat_id, c_name = _resolve(item_raw)
                if c_name is None:
                    if _is_onboard(item_raw, cat_id):
                        continue
                    raw_norm = normalize_payload_match_key(item_raw)
                    if raw_norm in onboard_payload_keys:
                        continue
                    return _fail(str(op), f"设置的载荷 '{item_raw}' 非法或不属于当前任务允许范围")
                if _is_onboard(c_name, cat_id) or _is_onboard(item_raw, cat_id):
                    continue
                if c_name not in new_canonicals:
                    new_canonicals.append(c_name)
            new_value = new_canonicals


        elif op == "clear":
            slot.value = []
            slot.value_type = "list"
            slot.status = "missing"
            slot.source = source
            slot.raw_value = raw_text
            slot.confidence = confidence
            slot.candidate_value = None
            slot.validation_error = None
            return {
                "success": True,
                "changed": (old_value != []),
                "operation": "clear",
                "old_value": old_value,
                "new_value": [],
                "error": None,
            }

        else:
            err = f"不支持的 list mutation 操作 '{op}'"
            slot.validation_error = err
            return {
                "success": False,
                "changed": False,
                "operation": str(op),
                "old_value": old_value,
                "new_value": old_value,
                "error": err,
            }

        slot.value = new_value
        slot.value_type = "list"
        slot.status = "candidate"
        slot.source = source
        slot.raw_value = raw_text
        slot.confidence = confidence
        slot.candidate_value = None
        slot.validation_error = None

        return {
            "success": True,
            "changed": (old_value != new_value),
            "operation": op,
            "old_value": old_value,
            "new_value": new_value,
            "error": None,
        }
