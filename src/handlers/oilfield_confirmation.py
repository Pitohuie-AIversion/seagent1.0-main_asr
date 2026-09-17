"""
src/handlers/oilfield_confirmation.py - 油田交互确认与指代回溯处理器

职责：
1. 四大实体（任务、机器人、油田、工具）指代消歧与上下文回溯（process_referential_carryover）；
2. 待确认油田名称确认与取消交互（resolve_pending_oilfield_confirmation, user_confirmed_oilfield, user_cancelled_oilfield）；
3. 待确认油田引导提问生成（build_pending_oilfield_reply, top_pending_oilfield_candidate）；
4. 事务内油田实体链接、坐标反向推导与上下文映射（link_oilfield_update_in_transaction）。
"""

from __future__ import annotations

import copy
import logging
import re
from typing import Any

from ..slot_store import Slot

logger = logging.getLogger(__name__)


class OilfieldConfirmationHandler:
    """油田交互确认与指代回溯处理器"""

    def __init__(self, manager: Any = None) -> None:
        self.manager = manager

    def __getattr__(self, name: str) -> Any:
        if "manager" in self.__dict__ and self.manager is not None:
            return getattr(self.manager, name)
        raise AttributeError(f"'{type(self).__name__}' object has no attribute '{name}'")


    def resolve_pending_oilfield_confirmation(
        self,
        user_message: str,
        request_id: str = "req_default",
        pending_action: str | None = None,
        subject_text: str | None = None,
    ) -> str | None:
        manager = self.manager
        pending_slot = manager.slot_store.slots.get("pending_oilfield_name")
        if not pending_slot or not pending_slot.value or pending_slot.status != "valid":
            return None
        if pending_action == "reject" or (
            pending_action is None and self.user_cancelled_oilfield(user_message)
        ):
            oil_slot = manager.slot_store.slots.get("oilfield_name")
            clear_oil = ("oilfield_name", "oilfield_entity_id") if (not oil_slot or oil_slot.status != "valid") else ()
            manager._commit_internal_slot_values(
                {},
                clear_keys=(
                    "pending_oilfield_name",
                    "pending_oilfield_candidates",
                    *clear_oil,
                ),
            )
            manager._rebuild_cache()
            return "已取消当前待确认油田名称，请提供标准的油田名称（例如：流花11-1油田、陵水17-2油田等），或补充油田坐标。"

        if pending_action not in {"confirm", None}:
            return None
        if pending_action is None and not self.user_confirmed_oilfield(user_message):
            return None

        candidate = self.top_pending_oilfield_candidate(subject_text or user_message)
        if not candidate:
            return self.build_pending_oilfield_reply()

        confirmed_name = candidate.get("name")
        manager._commit_internal_slot_values(
            {
                "oilfield_name": confirmed_name,
                "raw_oilfield_name": confirmed_name,
                "oilfield_entity_id": candidate.get("id"),
                "oilfield_match_status": "accepted",
                "oilfield_match_confidence": candidate.get("confidence"),
                "oilfield_match_evidence": candidate.get("evidence", []),
            },
            clear_keys=(
                "pending_oilfield_name",
                "pending_oilfield_candidates",
            ),
        )
        manager._rebuild_cache()
        return f"已确认油田名称为“{confirmed_name}”，我会按这个标准名称继续收集任务信息。"

    def build_pending_oilfield_reply(self) -> str | None:
        manager = self.manager
        task_type_slot = manager.slot_store.slots.get("task_type_key")
        task_type_key = task_type_slot.value if task_type_slot and task_type_slot.status == "valid" else None
        if task_type_key:
            field_defs = manager.builder.get_schema(task_type_key, manager.mode)
            schema_keys = {str(field.get("key")) for field in field_defs if field.get("key")}
            if not manager.slot_filter.supports_oilfield_slots(schema_keys):
                return None

        pending_slot = manager.slot_store.slots.get("pending_oilfield_name")
        raw_name = pending_slot.value if (pending_slot and pending_slot.status == "valid") else None
        oil_slot = manager.slot_store.slots.get("oilfield_name")
        has_oilfield = oil_slot.value if (oil_slot and oil_slot.status == "valid") else None
        if not raw_name or has_oilfield:
            return None

        candidate = self.top_pending_oilfield_candidate()
        if candidate:
            name = candidate.get("name")
            return f"我识别到油田名称“{raw_name}”，疑似为“{name}”。请确认是否采用该标准油田名称？"
        return (
            f"我识别到油田名称“{raw_name}”，但没有匹配到标准油田。"
            "请提供标准的油田名称（例如：流花11-1油田、陵水17-2油田等），或补充油田坐标。"
        )

    def top_pending_oilfield_candidate(self, user_message: str = "") -> dict | None:
        cand_slot = self.manager.slot_store.slots.get("pending_oilfield_candidates")
        candidates = cand_slot.value if (cand_slot and cand_slot.status == "valid") else None
        if isinstance(candidates, list) and candidates:
            if user_message:
                for c in candidates:
                    if isinstance(c, dict) and c.get("name") and c.get("name") in user_message:
                        return c
            candidate = candidates[0]
            if isinstance(candidate, dict) and candidate.get("name"):
                return candidate
        return None

    def user_confirmed_oilfield(self, message: str) -> bool:
        keywords = ["是", "对", "就是", "采用", "确认", "确定", "可以", "好的", "ok", "使用"]
        negations = ["不", "别", "不要", "不是", "取消"]
        msg = message.strip().lower()
        if any(neg in msg for neg in negations):
            return False
        return any(kw in msg for kw in keywords)

    def user_cancelled_oilfield(self, message: str) -> bool:
        msg = message.strip()
        if any(neg in msg for neg in ["不是要取消", "不是取消", "不要取消", "不取消", "别取消", "不要修改", "不修改"]):
            return False
        if any(mod_kw in msg for mod_kw in ["改成", "修改", "水深", "支持船", "管缆", "设备", "载荷"]) and "油田" not in msg:
            return False
        if "任务" in msg or "取消任务" in msg:
            return False

        pending_slot = self.manager.slot_store.slots.get("pending_oilfield_name")
        pending_name = pending_slot.value if (pending_slot and pending_slot.status == "valid") else None
        if pending_name:
            mentioned_oilfields = re.findall(r"[\u4e00-\u9fa50-9\-]+油田", msg)
            if mentioned_oilfields:
                p_norm = pending_name.replace("油田", "").strip()
                matched_any = any(p_norm in m or m.replace("油田", "").strip() in p_norm for m in mentioned_oilfields)
                if not matched_any:
                    return False

        keywords = ["不是", "不对", "否", "错了", "重新", "取消油田", "不要此油田", "这个油田不对", "不要"]
        return any(kw in msg for kw in keywords) or msg in ("不要", "取消", "不对", "不是")

    def remember_discussed_entities(self, user_message: str) -> None:
        """Remember query context without changing the task or its lifecycle."""
        manager = self.manager
        # 四大实体 (Task, Robot, Oilfield, Payload) 全量上下文提取与暂存
        r_ent = manager._extract_robot_entity_from_text(user_message)
        if r_ent:
            manager._last_discussed_robot = r_ent

        o_ent = manager._extract_oilfield_entity_from_text(user_message)
        if o_ent:
            manager._last_discussed_oilfield = o_ent

        p_ent = manager._extract_payload_entity_from_text(user_message)
        if p_ent:
            manager._last_discussed_payload = p_ent

    def process_referential_carryover(self, user_message: str) -> list[dict]:
        """Resolve WRITE references into candidates for the ordinary transaction.

        This helper never writes slots, rewrites user text, or changes phase.
        The caller must first route the original message and enforce done guards.
        """
        manager = self.manager
        candidates: list[dict] = []
        if re.search(r"不要|不用|不选|不去|不带|别|取消|如果|假如|是否|能否", user_message):
            return candidates

        def add(key: str, value: Any) -> None:
            candidates.append({
                "canonical_key": key, "raw_key": key,
                "raw_value": copy.deepcopy(value),
                "normalized_value": copy.deepcopy(value),
                "confidence": 1.0, "resolution_method": "reference_context",
            })

        # 1. 任务类型指代继承
        REFERENTIAL_START_TRIGGERS = (
            "那开始这个任务", "开始这个任务", "就做这个任务", "就安排这个", "开始创建",
            "就这个吧", "安排这个任务", "创建这个任务", "就按这个做", "开始做这个",
            "开启这个任务", "按这个开始", "就选这个任务", "那就这个", "开始这个",
            "开始该任务", "就做这个", "安排这个", "创建这个", "选这个任务", "那开始这个",
            "开始吧", "开始该作业", "就按这个", "开始任务"
        )
        is_referential_start = any(kw in user_message for kw in REFERENTIAL_START_TRIGGERS) or (
            ("开始" in user_message or "做" in user_message or "安排" in user_message or "创建" in user_message)
            and ("这个" in user_message or "任务" in user_message)
            and "不" not in user_message and "别" not in user_message and "取消" not in user_message
        )
        if is_referential_start and not manager.task_state.get("task_type_key") and manager._last_discussed_task_type:
            add("task_type_key", manager._last_discussed_task_type)

        # 2. 机器人实体指代继承
        ROBOT_TRIGGERS = ("就用这个机器人", "选这个机器人", "用这个设备", "就用这款", "选这个型", "安排这个机", "就用它", "用它", "选这个设备", "用这个机器人", "就这个机器人")
        is_robot_ref = any(kw in user_message for kw in ROBOT_TRIGGERS) or (
            ("用" in user_message or "选" in user_message or "安排" in user_message)
            and ("这个机器人" in user_message or "该设备" in user_message or "这款" in user_message or "它" in user_message)
        )
        if is_robot_ref and manager._last_discussed_robot:
            r_val = manager._last_discussed_robot
            if manager.kb._resolve_robot_variant_exact(r_val):
                add("equipment_type", r_val)
            elif manager.kb.resolve_robot_family(r_val):
                add("equipment_family", r_val)
            elif manager.kb._resolve_robot_class_key(r_val) in manager.kb.robot_fleet.get("robot_classes", {}):
                add("equipment_class", manager.kb._resolve_robot_class_key(r_val))
            else:
                # Unit aliases are resolved by the normal equipment cascade;
                # unknown or ambiguous names must remain uncommitted.
                add("equipment_name", r_val)

        # 3. 油田海域实体指代继承
        OILFIELD_TRIGGERS = ("就去这个油田", "选这个油田", "去这个海域", "选这个区域", "就在这做", "去这里", "就选这个油田", "去这个油田", "在这做")
        is_oilfield_ref = any(kw in user_message for kw in OILFIELD_TRIGGERS) or (
            ("去" in user_message or "选" in user_message or "在" in user_message)
            and ("这个油田" in user_message or "该海域" in user_message or "这个区域" in user_message or "这里" in user_message)
        )
        if is_oilfield_ref and manager._last_discussed_oilfield:
            add("oilfield_name", manager._last_discussed_oilfield)

        # 4. 载荷工具实体指代继承
        PAYLOAD_TRIGGERS = ("就用这个工具", "带上这个", "选这个载荷", "挂载这个", "就带这个", "就用这个载荷", "装上这个", "用这个工具", "带这个")
        is_payload_ref = any(kw in user_message for kw in PAYLOAD_TRIGGERS) or (
            ("用" in user_message or "带" in user_message or "挂载" in user_message or "装" in user_message)
            and ("这个工具" in user_message or "该载荷" in user_message or "这个传感器" in user_message)
        )
        if is_payload_ref and manager._last_discussed_payload:
            add("payload", [manager._last_discussed_payload])

        return candidates

    @staticmethod
    def merge_referential_candidates(extraction: dict, candidates: list[dict]) -> dict:
        """Fill absent model candidates; explicit values in this turn take priority."""
        result = copy.deepcopy(extraction)
        existing = result.setdefault("slot_candidates", [])
        keys = {item.get("canonical_key") for item in existing if isinstance(item, dict)}
        selector_groups = (
            {"task_type", "task_type_key"},
            {"equipment_class", "equipment_family", "equipment_type", "equipment_unit_id", "equipment_name"},
        )
        for candidate in candidates:
            key = candidate["canonical_key"]
            if any(key in group and keys.intersection(group) for group in selector_groups):
                continue
            if key not in keys:
                existing.append(copy.deepcopy(candidate))
                keys.add(key)
        return result

    def link_oilfield_update_in_transaction(
        self,
        updates: dict,
        new_slots: dict,
        user_message: str = "",
        extracted_oilfield: str | None = None,
    ) -> dict:
        manager = self.manager
        existing_task_id = new_slots.get("task_id")
        is_task_id_locked = bool(existing_task_id and existing_task_id.status == "valid" and existing_task_id.value)
        current_tt = (
            (new_slots["task_type_key"].value if new_slots.get("task_type_key") and new_slots["task_type_key"].value else None)
            or manager.task_state.get("task_type_key")
        )

        tt_val = updates.get("task_type_key")
        if isinstance(tt_val, dict):
            tt_val = tt_val.get("value")

        if is_task_id_locked and current_tt:
            task_type_key = current_tt
        else:
            task_type_key = tt_val or current_tt
        supports_oilfield = True
        if task_type_key:
            field_defs = manager.builder.get_schema(task_type_key, manager.mode)
            schema_keys = {str(field.get("key")) for field in field_defs if field.get("key")}
            supports_oilfield = manager.slot_filter.supports_oilfield_slots(schema_keys)

        raw_name = (
            updates.get("oilfield_name")
            or updates.get("raw_oilfield_name")
            or extracted_oilfield
        )
        if isinstance(raw_name, dict):
            raw_name = raw_name.get("value")

        if not raw_name and user_message and any(kw in user_message for kw in ("油田", "气田", "海域")):
            m = manager.oilfield_linker.link(user_message)
            if m and m.status == "accepted" and m.standard_name:
                raw_name = m.standard_name

        is_task_switching = bool(current_tt and tt_val and tt_val != current_tt)
        is_locked_switch_rejected = bool(is_task_id_locked and current_tt and tt_val and tt_val != current_tt)
        if (is_locked_switch_rejected and not supports_oilfield) or (is_task_switching and not supports_oilfield and not raw_name):
            linked = dict(updates)
            linked.pop("oilfield_name", None)
            linked.pop("raw_oilfield_name", None)
            for k in (
                "oilfield_name",
                "raw_oilfield_name",
                "oilfield_match_status",
                "oilfield_match_confidence",
                "oilfield_match_evidence",
                "oilfield_match_candidates",
                "oilfield_entity_id",
                "pending_oilfield_name",
                "pending_oilfield_candidates",
            ):
                if k in new_slots:
                    new_slots[k].value = None
                    new_slots[k].status = "missing"
            return linked

        coords = (
            updates.get("oilfield_coordinates")
            or updates.get("start_point")
            or updates.get("cable_position")
            or next(
                (
                    new_slots[key].value
                    for key in (
                        "oilfield_coordinates",
                        "start_point",
                        "cable_position",
                    )
                    if new_slots.get(key)
                    and new_slots[key].status == "valid"
                    and new_slots[key].value is not None
                ),
                None,
            )
        )
        linked = dict(updates)

        # 反向映射：检查坐标是否包含在知识库某油田范围内
        matched_entity_by_coords = manager.oilfield_linker.find_entity_by_coords(coords) if coords else None

        if not raw_name:
            if matched_entity_by_coords:
                # 坐标落入已知油田，反向自动跟随推导油田名称
                raw_name = matched_entity_by_coords.get("name")
            else:
                # 坐标不落入任何已知油田，且用户未提供油田名称：
                # 若坐标被更新且原槽位绑定了知识库油田，解绑原知识库油田，让用户提供名称占位
                existing_entity_id_slot = new_slots.get("oilfield_entity_id")
                if (
                    "oilfield_coordinates" in updates
                    or "start_point" in updates
                    or "cable_position" in updates
                ) and existing_entity_id_slot and existing_entity_id_slot.value is not None:
                    linked.pop("oilfield_name", None)
                    if "oilfield_name" in new_slots:
                        new_slots["oilfield_name"].value = None
                        new_slots["oilfield_name"].status = "missing"
                    if "oilfield_entity_id" in new_slots:
                        new_slots["oilfield_entity_id"].value = None
                        new_slots["oilfield_entity_id"].status = "missing"
                    linked["__clear_oilfield_name"] = True
                return linked

        match = manager.oilfield_linker.link(str(raw_name), coords)

        for k in ("raw_oilfield_name", "oilfield_match_status", "oilfield_match_confidence", "oilfield_match_evidence", "oilfield_match_candidates"):
            if k not in new_slots:
                new_slots[k] = Slot(slot_name=k)

        new_slots["raw_oilfield_name"].value = match.raw
        new_slots["raw_oilfield_name"].status = "valid"
        new_slots["oilfield_match_status"].value = match.status
        new_slots["oilfield_match_status"].status = "valid"
        new_slots["oilfield_match_confidence"].value = match.confidence
        new_slots["oilfield_match_confidence"].status = "valid"
        new_slots["oilfield_match_evidence"].value = match.evidence
        new_slots["oilfield_match_evidence"].status = "valid"
        new_slots["oilfield_match_candidates"].value = match.candidates
        new_slots["oilfield_match_candidates"].status = "valid"

        if not supports_oilfield:
            linked.pop("oilfield_name", None)
            linked.pop("raw_oilfield_name", None)
            if "oilfield_name" in new_slots:
                new_slots["oilfield_name"].value = None
                new_slots["oilfield_name"].status = "missing"
            if "oilfield_entity_id" in new_slots:
                new_slots["oilfield_entity_id"].value = None
                new_slots["oilfield_entity_id"].status = "missing"
            return linked

        if match.status == "accepted" and match.standard_name:
            linked["oilfield_name"] = match.standard_name
            if "oilfield_name" not in new_slots:
                new_slots["oilfield_name"] = Slot("oilfield_name")
            new_slots["oilfield_name"].value = match.standard_name
            new_slots["oilfield_name"].status = "valid"
            if "oilfield_entity_id" not in new_slots:
                new_slots["oilfield_entity_id"] = Slot("oilfield_entity_id")
            new_slots["oilfield_entity_id"].value = match.entity_id
            new_slots["oilfield_entity_id"].status = "valid"
            linked["__clear_pending_oilfield"] = True

            # 自动映射油田坐标（若用户未上报自定义坐标）
            existing_coord_slot = new_slots.get("oilfield_coordinates")
            has_user_custom_coord = (
                "oilfield_coordinates" in updates
                or (
                    existing_coord_slot
                    and existing_coord_slot.status == "valid"
                    and existing_coord_slot.value is not None
                    and getattr(existing_coord_slot, "source", None) != "oilfield_default"
                )
            )
            if not has_user_custom_coord and match.entity_id:
                try:
                    ctx_res = manager.oilfield_linker.evaluate_context(entity_id=match.entity_id)
                    if ctx_res and ctx_res.default_coordinates:
                        default_coord = ctx_res.default_coordinates
                        linked["oilfield_coordinates"] = default_coord
                        if "oilfield_coordinates" not in new_slots:
                            new_slots["oilfield_coordinates"] = Slot("oilfield_coordinates")
                        new_slots["oilfield_coordinates"].value = default_coord
                        new_slots["oilfield_coordinates"].status = "valid"
                        new_slots["oilfield_coordinates"].source = "oilfield_default"
                except Exception as exc:
                    logger.debug("Failed to populate oilfield default coordinates for %s: %s", match.entity_id, exc)
        elif match.raw and not matched_entity_by_coords and not match.candidates:
            # 用户显式输入了自定义名称（如“自设A区”），且坐标不属于知识库任何油田：
            # 允许自定义名称作为 oilfield_name 生效（自定义名称占位），entity_id 为 None
            linked["oilfield_name"] = match.raw
            if "oilfield_name" not in new_slots:
                new_slots["oilfield_name"] = Slot("oilfield_name")
            new_slots["oilfield_name"].value = match.raw
            new_slots["oilfield_name"].status = "valid"
            new_slots["oilfield_name"].source = "user_input"
            if "oilfield_entity_id" not in new_slots:
                new_slots["oilfield_entity_id"] = Slot("oilfield_entity_id")
            new_slots["oilfield_entity_id"].value = None
            new_slots["oilfield_entity_id"].status = "missing"
            linked["__clear_pending_oilfield"] = True
        else:
            linked.pop("oilfield_name", None)
            for k in ("pending_oilfield_name", "pending_oilfield_candidates"):
                if k not in new_slots:
                    new_slots[k] = Slot(slot_name=k)
            new_slots["pending_oilfield_name"].value = match.raw
            new_slots["pending_oilfield_name"].status = "valid"
            new_slots["pending_oilfield_candidates"].value = match.candidates
            new_slots["pending_oilfield_candidates"].status = "valid"
            linked["__clear_oilfield_name"] = True
        return linked
