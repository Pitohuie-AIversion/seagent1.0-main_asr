"""
src/handlers/constraint_decision.py - 约束决策生命周期处理器

职责：
1. 软约束（Soft Warning）提示与用户确认/显式忽略拦截；
2. 硬约束（Hard Constraint）严格拦截与防绕过拦截（fail-closed）；
3. 外部状态（环境海况、机器人遥测）动态约束刷新与阻断解除；
4. 约束白名单跟踪与违规统计。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from .base import BaseDialogueHandler, DialogueContext, HandlerResult
from ..slot_store import ValidationAcknowledgement
from ..validator import Violation
from ..coord_parser import parse_coordinate_updates
from ..simulated_time import get_current_datetime
from ..constants import HARD_REFUSAL_LIMIT

logger = logging.getLogger("src.dialogue_manager")


class ConstraintDecisionHandler(BaseDialogueHandler):
    """约束决策与阻断处理生命周期处理器"""

    def _blocked_validation_purpose(self, user_message: str = "") -> str:
        previous = getattr(self.manager.slot_store, "validation_result", None)
        previous_purpose = getattr(previous, "purpose", "preview")
        if previous_purpose == "runtime_execution":
            return "runtime_execution"
        if previous_purpose == "publish" or self.manager._is_final_publish_confirmation(user_message):
            return "publish"
        return "preview"

    def can_handle(self, ctx: DialogueContext) -> bool:
        """
        判断是否处于约束决策阶段：
        1. 当前处于 blocked_hard 阶段且用户尝试确认/忽略；
        2. 当前处于 blocked_soft 阶段且用户输入忽略/确认。
        """
        phase = self.manager.phase
        user_message = ctx.user_message

        if phase == "blocked_hard":
            if (
                self.manager._is_confirmation_only(user_message)
                or self.manager._is_final_publish_confirmation(user_message)
                or self.manager._is_ignore_warning(user_message)
            ):
                return True

        if phase == "blocked_soft":
            if (
                self.manager._is_ignore_warning(user_message)
                or self.manager._is_final_publish_confirmation(user_message)
            ):
                return True

        return False

    def handle(self, ctx: DialogueContext) -> HandlerResult:
        """执行约束相关的拦截与决策流转"""
        phase = self.manager.phase
        user_message = ctx.user_message
        request_id = ctx.request_id

        # 1. 硬约束阻断态下的确认/绕过拦截
        if phase == "blocked_hard":
            prev_val = getattr(self.manager.slot_store, "validation_result", None)
            prev_has_hard = bool(
                (prev_val and getattr(prev_val, "violations", None) and any(getattr(v, "severity", "") == "hard" for v in prev_val.violations))
                or (self.manager._blocking_violations and any(getattr(v, "severity", "") == "hard" for v in self.manager._blocking_violations))
            )

            # Reconfirming a blocked task cannot weaken the checks that blocked
            # it. Interactive collection deliberately skips runtime availability.
            purpose = self._blocked_validation_purpose(user_message)
            val_res = self.manager._refresh_validation(purpose=purpose)
            current_hard = [
                v for v in val_res.violations
                if v.severity == "hard"
            ]
            if current_hard:
                self.manager._blocking_violations = current_hard
                reply = self.manager._reject_hard_constraint_bypass(user_message)
                return HandlerResult.success(reply=reply)

            is_real_validation = getattr(val_res, "validation_version", 0) > 0
            if prev_has_hard and is_real_validation and getattr(val_res, "overall_status", "") in ("valid", "none"):
                self.manager._blocking_violations = []
                self.manager._hard_refusal_counts.clear()
                self.manager._transition_phase("collecting", reason="external_state_constraint_resolved")
                return HandlerResult.not_handled()
            else:
                reply = self.manager._reject_hard_constraint_bypass(user_message)
                return HandlerResult.success(reply=reply)

        # 2. 软约束阻断态下的忽略与确认
        if phase == "blocked_soft":
            if self.manager._is_ignore_warning(user_message):
                reply = self.manager._handle_soft_warning_confirmation(user_message, request_id)
                return HandlerResult.success(reply=reply)
            if self.manager._is_final_publish_confirmation(user_message):
                reply = "当前仍存在软警告。请先修改相关参数，或明确接受当前软警告后继续。"
                self.manager.conversation_history.append({"role": "user", "content": user_message})
                self.manager.conversation_history.append({"role": "assistant", "content": reply})
                return HandlerResult.success(reply=reply)

        return HandlerResult.not_handled()

    def _handle_soft_warning_confirmation(self, user_message: str, request_id: str) -> str:
        """blocked_soft 阶段的确认/忽略处理。

        将已确认忽略的软警告录入 SlotStore.validation_acknowledgements 绑定快照版本，
        清除 _blocking_violations，然后根据缺失槽位决定进入 collecting 或 confirming。
        """
        dm = self.manager
        task_type_key = dm.task_state.get("task_type_key")
        missing = []
        if task_type_key:
            req_schema = dm.builder.get_schema(task_type_key, dm.mode)
            user_req_schema = [f for f in req_schema if f.get("type") not in ("auto", "fixed")]
            missing = dm.slot_store.get_missing_slots(
                user_req_schema,
                allowed_values_resolver=lambda field: dm.builder.resolve_allowed_values(
                    field,
                    task_type_key,
                    dm.task_state,
                ),
            )
        if not task_type_key:
            target_purpose = "interactive"
        else:
            target_purpose = "preview" if not missing else "interactive"

        res = dm._refresh_validation(purpose=target_purpose)
        status_ref = res.state_snapshot.get("status_ref") if res.state_snapshot else None
        state_ver = res.state_snapshot.get("state_version") if res.state_snapshot else None

        if dm._blocking_violations:
            for v in dm._blocking_violations:
                if getattr(v, "severity", "soft") == "soft":
                    ack = ValidationAcknowledgement(
                        constraint_id=v.constraint_id,
                        acknowledged_at=get_current_datetime().isoformat(timespec="seconds"),
                        task_version=res.task_version,
                        validation_version=res.validation_version,
                        validation_fingerprint=res.validation_fingerprint,
                        status_ref=status_ref or "",
                        state_version=state_ver or 0,
                        field=getattr(v, "related_fields", [""])[0] if getattr(v, "related_fields", None) else "",
                        value=getattr(v, "observed_value", None),
                    )
                    if ack not in dm.slot_store.validation_acknowledgements:
                        dm.slot_store.validation_acknowledgements.append(ack)
                for f in v.related_fields:
                    val = dm.task_state.get(f)
                    if val is not None:
                        dm._soft_whitelist.add((f, str(val), v.constraint_id))
            dm._blocking_violations = []

        # 重新检查约束（使用白名单过滤后的结果）
        res = dm._refresh_validation(purpose=target_purpose)
        all_violations = res.violations
        remaining_soft = [v for v in all_violations
                          if v.severity == "soft" and not dm._is_whitelisted(v)]
        remaining_hard = [v for v in all_violations if v.severity == "hard"]

        if remaining_hard:
            dm._transition_phase("blocked_hard", reason="hard_constraint_detected")
            dm._blocking_violations = remaining_hard
        elif remaining_soft:
            dm._transition_phase("blocked_soft", reason="soft_warning_detected")
            dm._blocking_violations = remaining_soft
        else:
            if task_type_key:
                if not missing:
                    dm._transition_phase("confirming", reason="required_slots_complete")
                else:
                    dm._transition_phase("collecting", reason="required_slots_missing")
            else:
                dm._transition_phase("collecting", reason="task_type_missing")

        if remaining_hard:
            reply = (
                "未能继续：重新校验发现硬约束，软警告确认不能绕过硬约束。"
                "任务尚未发布，请先修改相关参数。"
            )
        elif remaining_soft:
            reply = (
                "已记录可忽略软警告的确认，但仍有未确认的软警告。"
                "任务保持阻断且尚未发布。"
            )
        elif dm.phase == "confirming":
            reply = (
                "已记录您对当前软警告的确认。任务尚未发布；"
                "所有必填字段已完整，如确认无误，请回复“确认发布”。"
            )
        else:
            labels = [
                item.get("label") or item.get("key")
                for item in dm._last_missing
                if isinstance(item, dict) and (item.get("label") or item.get("key"))
            ]
            missing_text = "、".join(labels)
            suffix = (
                f"请继续补充：{missing_text}。"
                if missing_text
                else "请继续补充任务信息。"
            )
            reply = f"已记录您对当前软警告的确认。任务尚未发布；{suffix}"

        dm.conversation_history.append({"role": "user", "content": user_message})
        dm.conversation_history.append({"role": "assistant", "content": reply})
        return reply

    def _reject_hard_constraint_bypass(self, user_message: str) -> str:
        """Reject confirmation/ignore commands while hard violations remain."""
        dm = self.manager
        violations = [
            violation
            for violation in dm._blocking_violations
            if violation.severity == "hard"
        ]
        if not violations:
            val_res = dm._refresh_validation(purpose=self._blocked_validation_purpose(user_message))
            violations = [
                v for v in val_res.violations
                if v.severity == "hard"
            ]

        reply = "硬性约束不能通过确认或忽略警告绕过。请先修正以下问题后再发布任务。"
        if violations:
            reply = f"{reply}\n\n{dm.validator.format_violations(violations)}"
            dm._blocking_violations = violations

        dm.conversation_history.append({"role": "user", "content": user_message})
        dm.conversation_history.append({"role": "assistant", "content": reply})
        return reply

    def _merge_oilfield_context_violations(self, new_violations: list[Violation]) -> list[Violation]:
        task_type_key = self.task_state.get("task_type_key")
        if task_type_key:
            schema_keys = {
                str(field.get("key"))
                for field in self.builder.get_schema(task_type_key, self.mode)
                if isinstance(field, dict) and field.get("key")
            }
            # Oilfield linker metadata is task-scoped.  Even if an old/legacy
            # snapshot leaked an entity id, it must not inject C028/C029 into a
            # known task whose schema has no oilfield contract.  A missing task
            # key is retained as a fail-closed compatibility path for direct
            # validation of pre-schema/legacy state.
            if "oilfield_name" not in schema_keys:
                return new_violations

        entity_id = self.task_state.get("oilfield_entity_id")
        if not entity_id:
            return new_violations

        # Omit uncollected fields so the linker uses its own sentinel.  Keep
        # explicit null/invalid values distinct from absent optional context.
        context = {"entity_id": entity_id}
        if "oilfield_coordinates" in self.task_state:
            context["coordinates"] = self.task_state["oilfield_coordinates"]
        elif "start_point" in self.task_state:
            context["coordinates"] = self.task_state["start_point"]
        if "water_depth" in self.task_state:
            context["water_depth"] = self.task_state["water_depth"]

        try:
            ctx_res = self.oilfield_linker.evaluate_context(**context)
            if ctx_res and (
                ctx_res.coordinate_status == "invalid"
                or ctx_res.depth_status == "invalid"
            ):
                raise ValueError("已提供的油田坐标或水深无效")
            if ctx_res and ctx_res.issues:
                merged = list(new_violations)
                existing_ids = {v.constraint_id for v in merged}
                applicable_ids = None
                if task_type_key:
                    applicable_ids = {
                        str(item.get("id"))
                        for item in self.kb.get_constraints()
                        if isinstance(item, dict)
                        and (
                            task_type_key in (item.get("applies_to") or [])
                            or "all" in (item.get("applies_to") or [])
                        )
                    }
                for issue in ctx_res.issues:
                    if (
                        (
                            applicable_ids is None
                            or issue.constraint_id in applicable_ids
                        )
                        and issue.constraint_id not in existing_ids
                    ):
                        merged.append(
                            Violation(
                                constraint_id=issue.constraint_id,
                                constraint_name=issue.constraint_name,
                                check_type=issue.check_type,
                                severity=issue.severity,
                                message=issue.message,
                                related_fields=list(issue.related_fields),
                            )
                        )
                return merged
        except Exception as exc:
            merged = list(new_violations)
            merged.append(
                Violation(
                    constraint_id="C029",
                    constraint_name="油田上下文计算异常",
                    check_type="oilfield_context_failure",
                    severity="hard",
                    message=f"油田上下文计算失败，安全熔断: {exc}",
                    related_fields=["oilfield_name"],
                )
            )
            return merged

        return new_violations
    def _is_state_snapshot_stale(self) -> bool:
        """检查当前 validation_result 中绑定的 state_snapshot 是否已过时或与 state.yaml 不一致。"""
        val_res = getattr(self.slot_store, "validation_result", None)
        if not val_res:
            return True
        state_snap = getattr(val_res, "state_snapshot", None)
        if not state_snap or not isinstance(state_snap, dict):
            return False
        unit_id = state_snap.get("unit_id") or self.task_state.get("equipment_unit_id")
        if unit_id and isinstance(unit_id, str):
            try:
                curr_snap = self.kb.get_unit_state_snapshot(unit_id)
                if not curr_snap or not isinstance(curr_snap, dict):
                    return True
                if curr_snap.get("state_version") != state_snap.get("state_version"):
                    return True
                if curr_snap.get("updated_at") != state_snap.get("updated_at"):
                    return True
                if curr_snap.get("state") != state_snap.get("state"):
                    return True
            except Exception:
                return True
        else:
            try:
                if hasattr(self.kb, "state_info") and self.kb.state_info.get_store_version() != state_snap.get("store_version", 0):
                    return True
            except Exception:
                return True
        return False

    def _run_constraint_check(self, changed_fields: set[str], purpose: str = "interactive") -> dict:
        """执行约束检查，返回上下文"""
        if not changed_fields and self.phase not in ("blocked_hard", "blocked_soft") and not self._is_state_snapshot_stale():
            state_snap = getattr(self.slot_store.validation_result, "state_snapshot", None)
            return {"type": "none", "violations": [], "hard_refusal_counts": {}, "state_snapshot": state_snap}

        val_res = self._refresh_validation(purpose=purpose, changed_fields=changed_fields)
        state_snap = val_res.state_snapshot
        new_violations = self._merge_oilfield_context_violations(val_res.violations)

        current_hard = [
            v for v in new_violations
            if v.severity == "hard" and (purpose != "interactive" or v.constraint_id not in ("CLASS_NOT_ALLOWED_FOR_TASK", "FAMILY_CLASS_MISMATCH"))
        ]
        current_soft = [
            v for v in new_violations
            if v.severity == "soft" and not self._is_whitelisted(v)
        ]
        current_blockers = current_hard + current_soft

        # 处理 soft 阻塞升级为 hard / 维持 / 解除
        if self.phase == "blocked_soft":
            if current_hard:
                self._transition_phase("blocked_hard", reason="soft_upgraded_to_hard")
                self._blocking_violations = current_blockers
                for v in current_hard:
                    if v.constraint_id not in self._hard_refusal_counts:
                        self._hard_refusal_counts[v.constraint_id] = 0

                return {
                    "type": "hard",
                    "violations": current_hard,
                    "hard_refusal_counts": dict(self._hard_refusal_counts),
                    "state_snapshot": state_snap,
                }

            if current_soft:
                self._blocking_violations = current_soft
                return {
                    "type": "soft",
                    "violations": current_soft,
                    "hard_refusal_counts": {},
                    "state_snapshot": state_snap,
                }

            self._blocking_violations = []
            self._transition_phase("collecting", reason="soft_warning_resolved")
            return {
                "type": "none",
                "violations": [],
                "hard_refusal_counts": {},
                "state_snapshot": state_snap,
            }

        # 处理 hard 阻塞维持 / 降级为 soft / 解除
        if self.phase == "blocked_hard":
            if current_hard:
                self._blocking_violations = current_blockers
                for v in current_hard:
                    self._hard_refusal_counts[v.constraint_id] = \
                        self._hard_refusal_counts.get(v.constraint_id, 0) + 1

                final_ids = {
                    cid for cid, cnt in self._hard_refusal_counts.items()
                    if cnt >= HARD_REFUSAL_LIMIT
                }
                if final_ids:
                    self._transition_phase("rejected", reason="hard_refusal_limit_reached")
                    self._blocking_violations = []
                    return {
                        "type": "hard_rejected",
                        "violations": current_hard,
                        "hard_refusal_counts": dict(self._hard_refusal_counts),
                        "state_snapshot": state_snap,
                    }

                warn_ids = {
                    cid for cid, cnt in self._hard_refusal_counts.items()
                    if cnt == HARD_REFUSAL_LIMIT - 1
                }
                ctx_type = "hard_final_warning" if warn_ids else "hard"
                return {
                    "type": ctx_type,
                    "violations": current_hard,
                    "hard_refusal_counts": dict(self._hard_refusal_counts),
                    "state_snapshot": state_snap,
                }
            else:
                # 硬约束解除，清除计数
                resolved_ids = set(self._hard_refusal_counts.keys())
                for cid in resolved_ids:
                    self._hard_refusal_counts.pop(cid, None)

                if current_soft and purpose in ("preview", "publish"):
                    self._transition_phase("blocked_soft", reason="hard_downgraded_to_soft")
                    self._blocking_violations = current_soft
                    return {
                        "type": "soft",
                        "violations": current_soft,
                        "hard_refusal_counts": {},
                        "state_snapshot": state_snap,
                    }

                self._transition_phase("collecting", reason="hard_constraint_resolved")
                self._blocking_violations = []
                return {
                    "type": "none",
                    "violations": [],
                    "hard_refusal_counts": {},
                    "state_snapshot": state_snap,
                }

        # collecting / confirming 状态下的新违规
        if self.phase in ("collecting", "confirming"):
            if current_hard:
                self._transition_phase("blocked_hard", reason="hard_constraint_detected")
                self._blocking_violations = current_blockers
                for v in current_hard:
                    if v.constraint_id not in self._hard_refusal_counts:
                        self._hard_refusal_counts[v.constraint_id] = 0
                return {
                    "type": "hard",
                    "violations": current_hard,
                    "hard_refusal_counts": dict(self._hard_refusal_counts),
                    "state_snapshot": state_snap,
                }

            if current_soft:
                # 统一规则：所有的软警告都在任务字段收集完毕进行统一检查（purpose in ("preview", "publish") 或 confirming 阶段）。
                # 在字段收集阶段（collecting 且 purpose == "interactive"），软警告不中断槽位收集，只有硬约束可以在收集过程中即时触发阻断。
                if self.phase != "collecting" or purpose in ("preview", "publish"):
                    self._transition_phase("blocked_soft", reason="soft_warning_detected")
                    self._blocking_violations = current_soft
                    return {
                        "type": "soft",
                        "violations": current_soft,
                        "hard_refusal_counts": {},
                        "kb_alternatives": self._get_kb_alternatives_for_violations(current_soft),
                        "state_snapshot": state_snap,
                    }

        res = {"type": "none", "violations": [], "hard_refusal_counts": {}, "state_snapshot": state_snap}
        if current_blockers:
            res["kb_alternatives"] = self._get_kb_alternatives_for_violations(current_blockers)
        return res

    def _get_kb_alternatives_for_violations(self, violations: list) -> list[dict]:
        """从 KnowledgeBase 中检索真实的合规替代设备，严禁凭空编造非 KB 型号。"""
        task_type_key = self.task_state.get("task_type_key")
        water_depth = self.task_state.get("water_depth")
        if not task_type_key:
            return []

        try:
            wd = float(water_depth) if water_depth is not None else None
        except (ValueError, TypeError):
            wd = None

        valid_robots = self.kb.get_task_allowed_robot_variants(task_type_key)
        if wd is not None:
            valid_robots = [
                r for r in valid_robots
                if r.get("max_depth_m") is not None and float(r.get("max_depth_m")) >= wd
            ]

        curr_eq = self.task_state.get("equipment_type")
        alts = []
        for r in valid_robots:
            name = r.get("full_name") or r.get("name")
            if name and name != curr_eq:
                alts.append({
                    "name": name,
                    "max_depth_m": r.get("max_depth_m"),
                    "capabilities": r.get("capabilities") or [],
                })
        return alts[:3]

    # --------------------------------------------------------------------------
    # 工具方法
    # --------------------------------------------------------------------------

    def _merge_coordinate_updates(
        self,
        user_message: str,
        updates: dict,
        required: list[dict] | None,
    ) -> dict:
        coord_fields = {
            item["key"]
            for item in (required or [])
            if item.get("type") == "coord" and item.get("key")
        }
        coord_updates = parse_coordinate_updates(
            user_message,
            coord_fields,
            current_state=self.task_state,
            proposed_updates=updates,
        )
        if not coord_updates:
            return updates
        merged = dict(updates)
        merged.update(coord_updates)
        return merged

    def _invalidate_whitelist(self, changed_fields: set[str]):
        if changed_fields:
            self._soft_whitelist -= {e for e in self._soft_whitelist if e[0] in changed_fields}

    def _is_whitelisted(self, v: Violation) -> bool:
        res = getattr(self.slot_store, "validation_result", None)
        if res is None:
            return False

        curr_fp = getattr(res, "validation_fingerprint", None)
        state_snap = getattr(res, "state_snapshot", None)
        # 非遥测类约束（例如时间、区域风险）在尚未选择机器人时没有
        # state_snapshot。ValidationAcknowledgement 与 UI 契约均使用 ("", 0)
        # 表示该合法空状态；白名单校验必须采用同一标准值，否则确认会被永久判旧。
        curr_state_ver = (
            state_snap.get("state_version")
            if isinstance(state_snap, dict)
            else 0
        )
        curr_status_ref = (
            state_snap.get("status_ref")
            if isinstance(state_snap, dict)
            else ""
        )
        curr_state_ver = 0 if curr_state_ver is None else curr_state_ver
        curr_status_ref = curr_status_ref or ""

        if not curr_fp:
            return False

        acks = getattr(self.slot_store, "validation_acknowledgements", [])
        for ack in acks:
            ack_cid = getattr(ack, "constraint_id", None) if not isinstance(ack, dict) else ack.get("constraint_id")
            if ack_cid != v.constraint_id:
                continue

            ack_tv = getattr(ack, "task_version", None) if not isinstance(ack, dict) else ack.get("task_version")
            ack_vv = getattr(ack, "validation_version", None) if not isinstance(ack, dict) else ack.get("validation_version")
            ack_fp = getattr(ack, "validation_fingerprint", None) if not isinstance(ack, dict) else ack.get("validation_fingerprint")
            ack_sref = getattr(ack, "status_ref", None) if not isinstance(ack, dict) else ack.get("status_ref")
            ack_sver = getattr(ack, "state_version", None) if not isinstance(ack, dict) else ack.get("state_version")

            ack_val = getattr(ack, "value", None) if not isinstance(ack, dict) else ack.get("value")

            if (
                ack_tv == getattr(res, "task_version", 1)
                and ack_vv == getattr(res, "validation_version", 1)
                and ack_fp == curr_fp
                and ack_sref == curr_status_ref
                and ack_sver == curr_state_ver
            ):
                return True

            # 对于 check_type == 'state_timestamp' (如 C019)，只要环境观察值未改变，在补充槽位过程中保持白名单有效
            if (
                getattr(v, "check_type", None) == "state_timestamp"
                and ack_val is not None
                and ack_val == getattr(v, "observed_value", None)
            ):
                return True

            # 若单机状态版本与引用未变，且针对该约束的观察值一致，白名单保持有效
            if (
                ack_tv <= getattr(res, "task_version", 1)
                and ack_sref == curr_status_ref
                and ack_sver == curr_state_ver
                and (
                    ack_val is None
                    or ack_val == getattr(v, "observed_value", None)
                )
            ):
                return True

            # 兼容基于字段与值的 _soft_whitelist 检查
            for f in getattr(v, "related_fields", []):
                val = self.task_state.get(f)
                if val is not None and (f, str(val), v.constraint_id) in self._soft_whitelist:
                    return True

        # 若 _soft_whitelist 中包含相关字段且字段值未变，同样放行
        for f in getattr(v, "related_fields", []):
            val = self.task_state.get(f)
            if val is not None and (f, str(val), v.constraint_id) in self._soft_whitelist:
                return True

        return False

    def _ensure_constraint_details(self, reply: str, constraint_context: dict) -> str:
        """Append canonical hard-blocking details omitted or paraphrased by the LLM."""
        context_type = str((constraint_context or {}).get("type") or "")
        if not context_type.startswith("hard"):
            return reply

        violations = [
            violation
            for violation in ((constraint_context or {}).get("violations") or [])
            if getattr(violation, "severity", "") == "hard"
        ]
        import re

        reply_str = str(reply)

        def _normalize(text: object) -> str:
            if not isinstance(text, str):
                text = str(text)
            return re.sub(r"[\s\W_]+", "", text, flags=re.UNICODE)

        norm_reply = _normalize(reply_str)

        missing = []
        for violation in violations:
            msg = getattr(violation, "message", "") or ""
            cid = getattr(violation, "id", "") or getattr(violation, "constraint_id", "") or ""
            name = getattr(violation, "name", "") or getattr(violation, "constraint_name", "") or ""

            # 1. 严格包含
            if msg and str(msg) in reply_str:
                continue
            # 2. 规范化包含（忽略空格/标点差异）
            if msg and _normalize(msg) in norm_reply:
                continue
            # 3. 如果 reply 已经包含了约束 ID 或约束名称
            if cid and str(cid) in reply_str:
                continue
            if name and (_normalize(name) in norm_reply or str(name) in reply_str):
                continue
            missing.append(violation)

        if not missing:
            return reply

        details = self.validator.format_violations(missing)
        if isinstance(reply, str):
            return f"{reply.rstrip()}\n\n{details}" if reply.strip() else details
        return f"{reply}\n\n{details}"

    def task_uses_status_ref(self, status_ref: str | None) -> bool:
        """Return whether the current task is tied to a specific robot state ref."""
        if not status_ref:
            return True

        dm = self.manager
        selectors = [
            dm.task_state.get("equipment_unit_id"),
            dm._last_built_json.get("equipment_unit_id"),
        ]
        unit_slot = dm.slot_store.slots.get("equipment_unit_id")
        if unit_slot and unit_slot.status == "valid" and unit_slot.value is not None:
            selectors.append(unit_slot.value)

        for selector in selectors:
            if selector is None or selector == "":
                continue
            try:
                resolved_ref = dm.kb.state_info.resolve_status_ref(str(selector))
            except Exception:
                resolved_ref = None
            if resolved_ref == status_ref or str(selector) == status_ref:
                return True
        return False

    def refresh_external_state_constraints(self, status_ref: str | None = None) -> dict:
        """Refresh validation after external robot telemetry/state changes.

        This does not publish or edit task slots; it only synchronizes phase,
        blockers, validation_result, and missing fields with current evidence.
        """
        dm = self.manager
        if dm.phase in ("done", "rejected"):
            return {"refreshed": False, "reason": "terminal_phase"}
        if not self.task_uses_status_ref(status_ref):
            return {"refreshed": False, "reason": "unrelated_status_ref"}

        task_type_key = dm.task_state.get("task_type_key")
        missing: list[dict] = []
        if task_type_key:
            schema = dm.builder.get_schema(task_type_key, dm.mode)
            user_req_schema = [
                field for field in schema
                if field.get("type") not in ("auto", "fixed")
            ]
            missing = dm.slot_store.get_missing_slots(
                user_req_schema,
                allowed_values_resolver=lambda field: dm.builder.resolve_allowed_values(
                    field,
                    task_type_key,
                    dm.task_state,
                ),
            )
            dm._last_missing = missing
        else:
            dm._last_missing = [{
                "key": "task_type",
                "label": "任务类型",
                "type": "string",
                "allowed_values": dm.kb.get_all_task_type_values(),
            }]
            missing = dm._last_missing

        purpose = "preview" if task_type_key and not missing else "interactive"
        if dm.phase == "blocked_hard":
            purpose = self._blocked_validation_purpose()
        val_res = dm._refresh_validation(purpose=purpose)
        violations = self._merge_oilfield_context_violations(val_res.violations)
        hard = [v for v in violations if v.severity == "hard"]
        soft = [
            v for v in violations
            if v.severity == "soft" and not self._is_whitelisted(v)
        ]

        if hard or val_res.overall_status == "validation_error":
            dm._transition_phase("blocked_hard", reason="external_state_hard_detected")
            dm._blocking_violations = hard
        elif soft:
            dm._transition_phase("blocked_soft", reason="external_state_soft_detected")
            dm._blocking_violations = soft
        else:
            dm._blocking_violations = []
            dm._hard_refusal_counts.clear()
            if task_type_key and not missing:
                dm._transition_phase("confirming", reason="external_state_constraints_resolved")
            else:
                dm._transition_phase("collecting", reason="external_state_constraints_resolved")

        return {
            "refreshed": True,
            "phase": dm.phase,
            "overall_status": val_res.overall_status,
            "hard_violations": len(hard),
            "soft_violations": len(soft),
            "missing": [m.get("key") for m in missing if isinstance(m, dict)],
        }

    def get_valid_acknowledgements(
        self,
        validation_result: Any,
    ) -> list[ValidationAcknowledgement]:
        """
        过滤并返回与当前 validation_result 完全匹配的有效确认。
        至少匹配：
        - constraint_id
        - task_version
        - validation_version
        - validation_fingerprint
        - status_ref
        - state_version
        - observed_value (or field/value)
        """
        dm = self.manager
        if not validation_result or not dm.slot_store.validation_acknowledgements:
            return []

        status_ref = (
            validation_result.state_snapshot.get("status_ref", "")
            if validation_result.state_snapshot
            else ""
        )
        state_version = (
            validation_result.state_snapshot.get("state_version", 0)
            if validation_result.state_snapshot
            else 0
        )

        violation_map = {
            v.constraint_id: v for v in (validation_result.violations or [])
        }

        valid_acks = []
        for ack in dm.slot_store.validation_acknowledgements:
            if not isinstance(ack, ValidationAcknowledgement):
                continue
            # ack 的创建版本不能晚于当前 validation_result
            if ack.task_version > validation_result.task_version:
                continue
            if ack.status_ref != status_ref:
                continue
            if ack.state_version != state_version:
                continue
            # 必须对应当前实际存在的软警告 Violation
            if ack.constraint_id not in violation_map:
                continue
            v = violation_map[ack.constraint_id]
            if ack.value != getattr(v, "observed_value", None) and ack.field not in getattr(v, "related_fields", []):
                continue
            valid_acks.append(ack)
        return valid_acks

    # 别名兼容
    merge_oilfield_context_violations = _merge_oilfield_context_violations
    is_state_snapshot_stale = _is_state_snapshot_stale
    run_constraint_check = _run_constraint_check
    get_kb_alternatives_for_violations = _get_kb_alternatives_for_violations
    merge_coordinate_updates = _merge_coordinate_updates
    invalidate_whitelist = _invalidate_whitelist
    is_whitelisted = _is_whitelisted
    ensure_constraint_details = _ensure_constraint_details
    _task_uses_status_ref = task_uses_status_ref
    _get_valid_acknowledgements = get_valid_acknowledgements
