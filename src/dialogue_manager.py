"""
dialogue_manager.py - 对话主控制器

协调提取、验证、知识检索、响应生成的完整流程。

阶段状态机:
  collecting
    → blocked_hard   (硬违规阻塞)
    → blocked_soft   (软违规阻塞)
    → confirming     (字段齐全无阻塞，等待确认)
    → done           (确认，输出最终JSON)
    → rejected       (拒绝)

约束检查策略:
  - 字段变化后增量检查
  - Hard违规阻塞，连续失败达上限则拒绝
  - Soft违规询问一次，用户可忽略并加入白名单
  - 白名单key: (field, str(value), constraint_id)，字段值变化时失效
"""

import json
from typing import Any
from zoneinfo import ZoneInfo
from datetime import datetime   # ✅ 新增导入

from .llm_client import LLMClient
from .knowledge_retriever import KnowledgeBase
from .extractor import ParameterExtractor
from .normalizer import FieldNormalizer
from .output_builder import OutputBuilder
from .validator import TaskValidator, Violation
from .prompts import build_responder_messages
from .task_intent_builder import TaskIntentBuilder
from .simulated_time import get_current_datetime
from .time_context import get_time_context, is_standalone_time_query
from .coord_parser import parse_coordinate_updates
from .oilfield_linker import OilfieldEntityLinker

HARD_REFUSAL_LIMIT = 4   # 连续拒绝上限

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
    "equipment_type":      "设备类型",
    "equipment_name":      "设备全称",
    "payload":             "携带工具",
    "support_vessel":      "支持船编号",
    "oilfield_name":       "油田名称",
    "oilfield_coordinates":"油田经纬度",
    "wellhead_id":         "井口编号",
    # 采油树不再区分立式/卧式，停用该状态标签。
    # "tree_type":           "采油树类型",
}

# 软约束忽略关键词
SOFT_IGNORE_KEYWORDS = {"忽略", "继续", "确认", "无视", "不管", "没关系", "ok", "好的", "是"}


class DialogueManager:
    def __init__(self, llm: LLMClient, kb: KnowledgeBase):
        self.llm = llm
        self.kb = kb
        self.extractor = ParameterExtractor(llm)
        self.normalizer = FieldNormalizer(llm)
        self.builder = OutputBuilder(kb)
        self.validator = TaskValidator(kb)
        self.oilfield_linker = OilfieldEntityLinker(kb.environment)

        # 对话核心状态
        self.conversation_history: list[dict] = []
        self.task_state: dict = {}
        self.mode: str = "normal"
        self.phase: str = "collecting"
        self.final_result: dict | None = None
        self.awaiting_final_confirm = False
        self.task_start_now = False

        # 约束管理状态
        self._blocking_violations: list[Violation] = []
        self._soft_whitelist: set[tuple[str, str, str]] = set()
        self._hard_refusal_counts: dict[str, int] = {}

        # ROV候选暂存
        self._pending_rov_candidates: list[dict] = []

        # 缓存构建结果
        self._last_built_json: dict = {}
        self._last_missing: list[dict] = []

    # --------------------------------------------------------------------------
    # 主入口
    # --------------------------------------------------------------------------

    def process(self, user_message: str) -> str:
        old_phase = self.phase

        if self._is_business_identity_query(user_message):
            reply = "我是一个专业的水下多智能体任务决策大模型，可用于辅助水下任务规划、参数收集与可行性验证。请描述您的水下任务需求，我会继续帮您完善任务参数。"
            self.conversation_history.append({"role": "user", "content": user_message})
            self.conversation_history.append({"role": "assistant", "content": reply})
            return reply

        if is_standalone_time_query(user_message):
            reply = get_time_context().user_reply
            self.conversation_history.append({"role": "user", "content": user_message})
            self.conversation_history.append({"role": "assistant", "content": reply})
            return reply


        pending_reply = self._resolve_pending_oilfield_confirmation(user_message)
        if pending_reply is not None:
            self.conversation_history.append({"role": "user", "content": user_message})
            self.conversation_history.append({"role": "assistant", "content": pending_reply})
            return pending_reply

        # 提取任务类型
        task_type_key = self.task_state.get("task_type_key")
        if task_type_key is None:
            updates = self.extractor.extract_updates(
                user_message, self.conversation_history, self.task_state,
                task_type_key=None,
                task_type_map=self.kb.get_task_type_map(),
                required=None,
            )
            updates = self._link_oilfield_update(updates)
            changed_fields = self._compute_changed_fields(updates)
            self._apply_updates(updates)
            print('task extract | '*10)
            print(updates)

        # 提取参数更新
        task_type_key = self.task_state.get("task_type_key")
        if task_type_key:
            required = self.builder.get_required(task_type_key, self.mode)
            updates = self.extractor.extract_updates(
                user_message, self.conversation_history, self.task_state,
                task_type_key=task_type_key,
                task_type_map=self.kb.get_task_type_map(),
                required=required,
                ROV2type=self.kb.ROV2type
            )
            updates = self._merge_coordinate_updates(user_message, updates, required)
            updates = self._link_oilfield_update(updates)
            changed_fields = self._compute_changed_fields(updates)
            print('key extract | ' * 10)
            print(updates)

        # 字段变化 → 使相关soft白名单失效
        self._invalidate_whitelist(changed_fields)

        # 应用更新 + 规范化
        self._apply_updates(updates)

        if changed_fields or not self.task_state.get("task_type_key"):
            self._normalize_constrained_fields(changed_fields)

        # 构建flat JSON，获取缺失字段
        task_type_key = self.task_state.get("task_type_key")
        if task_type_key:
            built, missing = self.builder.build(self.task_state, task_type_key, self.mode)
            if "task_id" in built and not self.task_state.get("task_id"):
                self.task_state["task_id"] = built["task_id"]
            self._last_built_json = built
            self._last_missing = missing
        else:
            built, missing = {}, [{"key": "task_type", "label": "任务类型", "type": "string",
                                    "allowed_values": self.kb.get_all_task_type_values()}]
            self._last_built_json = built
            self._last_missing = missing

        self.task_start_now = self.is_start_time_near_now()
        print('【是否现在开始】')
        print(self.task_start_now)
        print('='*60)
        print(missing)

        pending_oilfield_reply = self._build_pending_oilfield_reply()
        if pending_oilfield_reply:
            self.phase = "collecting"
            self.conversation_history.append({"role": "user", "content": user_message})
            self.conversation_history.append({"role": "assistant", "content": pending_oilfield_reply})
            return pending_oilfield_reply

        # 处理软约束忽略（blocked_soft阶段）
        if self.phase == "blocked_soft":
            user_ignore = any(kw in user_message.lower() for kw in SOFT_IGNORE_KEYWORDS)
            if self._blocking_violations:
                soft_related_fields = set()
                for v in self._blocking_violations:
                    soft_related_fields.update(v.related_fields)
                if user_ignore and not (soft_related_fields & changed_fields):
                    for v in self._blocking_violations:
                        for f in v.related_fields:
                            val = self.task_state.get(f)
                            if val is not None:
                                self._soft_whitelist.add((f, str(val), v.constraint_id))
                    self.phase = "collecting"
                    self._blocking_violations = []

        # 约束检查
        ALL_FIELDS = {"task_type", "start_time", "end_time", "cable_position", "cable_type", "start_point", "end_point",
                      "water_depth", "equipment_type", "equipment_name", "payload", "support_vessel", "oilfield_name",
                      "oilfield_coordinates", "wellhead_id"}
                      # 采油树不再区分立式/卧式，约束检查不再跟踪 tree_type。

        if not missing and self.phase not in ("blocked_hard", "blocked_soft"):
            constraint_context = self._run_constraint_check(ALL_FIELDS)
        elif not missing and self.phase == "blocked_soft":
            constraint_context = self._run_constraint_check(changed_fields)
        elif not missing and self.phase == "blocked_hard":
            constraint_context = self._run_constraint_check(ALL_FIELDS)
        else:
            constraint_context = self._run_constraint_check(changed_fields)

        if (self.phase not in ("blocked_hard", "blocked_soft", "confirming") and not missing):
            self.phase = "confirming"

        # 处理用户确认/取消
        if old_phase == "confirming" and self._user_confirmed(user_message):
            all_violations = self.validator.validate(self.task_state)
            if not missing and not self.validator.has_hard_violations(all_violations):
                self.phase = "done"
                self.final_result = built
                # 生成 TaskIntent JSON
                ti_builder = TaskIntentBuilder(self.kb)
                ti_json = ti_builder.build(
                    task_state=self.task_state,
                    built_json=built,
                    mode=self.mode,
                    task_type_key=self.task_state.get("task_type_key")
                )
                self.task_state['intent_id'] = ti_json['intent_id']  # 保存intent_id供历史快照使用
                # ========== 调试打印：TaskIntent 生成信息 ==========
                print("\n" + "="*60)
                print("🔔 [DEBUG] TaskIntent 生成成功")
                print(json.dumps(ti_json, ensure_ascii=False, indent=2))
                print(f"📁 文件位置: /root/autodl-tmp/result/task/task_intent_{ti_json['intent_id']}.json")
                print("="*60 + "\n")
                # =================================================
                if self.task_start_now:
                    reply = (f"✅ 信息收集完成，当前为【立即执行任务】，任务已生成并下发。\n"
                             f"{json.dumps(built, ensure_ascii=False, indent=2)}")
                else:
                    reply = (f"✅ 信息收集完成，当前为【未来规划任务】，已加入计划池。\n"
                             f"{json.dumps(built, ensure_ascii=False, indent=2)}")
                self.conversation_history.append({"role": "user", "content": user_message})
                self.conversation_history.append({"role": "assistant", "content": reply})
                return reply
            else:
                self.phase = "collecting"
                self.awaiting_final_confirm = False

        if self._user_cancelled(user_message):
            self.phase = "rejected"
            self.final_result = None
            reply = "任务已取消。如需重新规划，请重新开始。"
            self.conversation_history.append({"role": "user", "content": user_message})
            self.conversation_history.append({"role": "assistant", "content": reply})
            return reply

        print('=' * 60)
        print(self.phase)

        # 知识上下文
        knowledge_context = self.kb.get_context_for_state(self.task_state)

        # 生成回复
        messages = build_responder_messages(
            task_state=self.task_state,
            built_json=built,
            missing_fields=missing,
            mode=self.mode,
            phase=self.phase,
            knowledge_context=knowledge_context,
            constraint_context=constraint_context,
            conversation_history=self.conversation_history,
            latest_user_message=user_message,
            ROV2type=self.kb.ROV2type,
            support_task=self.kb.get_supported_task(),
        )
        print("DEBUG SYSTEM PROMPT START" + "="*40)
        print(messages[0]["content"])
        print("DEBUG SYSTEM PROMPT END" + "="*40)
        reply = self.llm.chat(messages, temperature=0.7, max_tokens=1500)
        reply = self.llm.filter_reply(reply, temperature=0.1, max_tokens=1500)

        self.conversation_history.append({"role": "user", "content": user_message})
        self.conversation_history.append({"role": "assistant", "content": reply})
        print('=' * 60)
        print(self.phase)
        return reply

    # --------------------------------------------------------------------------
    # 参数更新与规范化
    # --------------------------------------------------------------------------

    def _link_oilfield_update(self, updates: dict) -> dict:
        raw_name = updates.get("oilfield_name")
        if not raw_name:
            return updates

        coords = (
            updates.get("oilfield_coordinates")
            or updates.get("start_point")
            or updates.get("cable_position")
            or self.task_state.get("oilfield_coordinates")
            or self.task_state.get("start_point")
            or self.task_state.get("cable_position")
        )
        match = self.oilfield_linker.link(str(raw_name), coords)
        linked = dict(updates)
        linked["raw_oilfield_name"] = match.raw
        linked["oilfield_match_status"] = match.status
        linked["oilfield_match_confidence"] = match.confidence
        linked["oilfield_match_evidence"] = match.evidence
        linked["oilfield_match_candidates"] = match.candidates

        if match.status == "accepted" and match.standard_name:
            linked["oilfield_name"] = match.standard_name
            linked["oilfield_entity_id"] = match.entity_id
            linked["__clear_pending_oilfield"] = True
        else:
            linked.pop("oilfield_name", None)
            linked["pending_oilfield_name"] = match.raw
            linked["pending_oilfield_candidates"] = match.candidates
            linked["__clear_oilfield_name"] = True
        return linked

    def _apply_updates(self, updates: dict):
        if updates.get("__clear_oilfield_name"):
            self.task_state.pop("oilfield_name", None)
            self.task_state.pop("oilfield_entity_id", None)
        if updates.get("__clear_pending_oilfield"):
            self._clear_pending_oilfield()

        skip = {"emergency_mode", "rov_description", "__clear_oilfield_name", "__clear_pending_oilfield"}
        for k, v in updates.items():
            if k in skip or v is None or v == "":
                continue
            if k in ("task_type", "task_type_key"):
                self._handle_task_type_update(k, v)
                continue
            self.task_state[k] = v

        if updates.get("emergency_mode") and self.mode != "emergency":
            self.mode = "emergency"
        if "rov_description" in updates:
            self._handle_rov_description(updates["rov_description"])

        # Auto-synchronize equipment_type when equipment_name is set/updated
        name = self.task_state.get("equipment_name")
        if name:
            rov = self.kb.get_rov(name)
            if rov:
                self.task_state["equipment_name"] = rov["full_name"]
                category = rov.get("category")
                cats = self.kb.robot_fleet.get("robot_categories", {})
                if category in cats:
                    self.task_state["equipment_type"] = cats[category]["label"]

    def _resolve_pending_oilfield_confirmation(self, user_message: str) -> str | None:
        if not self.task_state.get("pending_oilfield_name"):
            return None
        if self._user_cancelled_oilfield(user_message):
            self._clear_pending_oilfield()
            self.task_state.pop("oilfield_name", None)
            self.task_state.pop("oilfield_entity_id", None)
            self._rebuild_cache()
            return "已取消当前待确认油田名称，请提供标准的油田名称（例如：流花11-1油田、陵水17-2油田等），或补充油田坐标。"
        if not self._user_confirmed_oilfield(user_message):
            return None

        candidate = self._top_pending_oilfield_candidate()
        if not candidate:
            return self._build_pending_oilfield_reply()

        self.task_state["oilfield_name"] = candidate.get("name")
        self.task_state["oilfield_entity_id"] = candidate.get("id")
        self.task_state["oilfield_match_status"] = "confirmed"
        self.task_state["oilfield_match_confidence"] = candidate.get("confidence")
        self.task_state["oilfield_match_evidence"] = candidate.get("evidence", [])
        confirmed_name = candidate.get("name")
        self._clear_pending_oilfield()
        self._rebuild_cache()
        return f"已确认油田名称为“{confirmed_name}”，我会按这个标准名称继续收集任务信息。"

    def _build_pending_oilfield_reply(self) -> str | None:
        raw_name = self.task_state.get("pending_oilfield_name")
        if not raw_name or self.task_state.get("oilfield_name"):
            return None

        candidate = self._top_pending_oilfield_candidate()
        if candidate:
            name = candidate.get("name")
            return f"我识别到油田名称“{raw_name}”，疑似为“{name}”。请确认是否采用该标准油田名称？"
        return (
            f"我识别到油田名称“{raw_name}”，但没有匹配到标准油田。"
            "请提供标准的油田名称（例如：流花11-1油田、陵水17-2油田等），或补充油田坐标。"
        )

    def _top_pending_oilfield_candidate(self) -> dict | None:
        candidates = self.task_state.get("pending_oilfield_candidates")
        if isinstance(candidates, list) and candidates:
            candidate = candidates[0]
            if isinstance(candidate, dict) and candidate.get("name"):
                return candidate
        return None

    def _clear_pending_oilfield(self):
        for key in ("pending_oilfield_name", "pending_oilfield_candidates"):
            self.task_state.pop(key, None)

    def _user_confirmed_oilfield(self, message: str) -> bool:
        keywords = ["是", "对", "就是", "采用", "确认", "确定", "可以", "好的", "ok"]
        return self._user_confirmed(message) or any(kw in message.lower() for kw in keywords)

    @staticmethod
    def _user_cancelled_oilfield(message: str) -> bool:
        keywords = ["不是", "不对", "否", "错了", "重新", "不要"]
        return any(kw in message for kw in keywords)

    def _handle_task_type_update(self, key: str, value: str):
        """验证任务类型合法性，并设置task_type_key"""
        task_type_map = self.kb.get_task_type_map()
        templates = self.kb.task_schemas.get("task_templates", {})

        if value in task_type_map:
            self.task_state["task_type"] = value
            self.task_state["task_type_key"] = task_type_map[value]
        elif key == "task_type_key" and value in templates:
            self.task_state["task_type_key"] = value
            values = templates[value].get("task_type_values", [])
            if len(values) == 1:
                self.task_state["task_type"] = values[0]

    def _normalize_constrained_fields(self, changed_fields: set[str]):
        """规范化有allowed_values约束的字段"""
        task_type_key = self.task_state.get("task_type_key")
        if not task_type_key:
            return

        schema = self.builder.get_schema(task_type_key, self.mode)
        for field_def in schema:
            key = field_def["key"]
            ftype = field_def["type"]
            if ftype not in ("string", "list", "tasktype"):
                continue
            if changed_fields and key not in changed_fields:
                continue

            allowed = self.builder._resolve_allowed(field_def, task_type_key)
            if not allowed:
                continue

            raw = self.task_state.get(key)
            if raw is None:
                continue

            # 已经是合法值则跳过
            if ftype in ("string", "tasktype") and raw in allowed:
                continue
            if ftype == "list" and isinstance(raw, list) and all(v in allowed for v in raw):
                continue

            # tasktype不走通用normalizer
            if ftype == "tasktype":
                continue

            normalized = self.normalizer.normalize(
                key, field_def["label"], raw, allowed, ftype
            )
            if normalized is not None:
                self.task_state[key] = normalized

    def _handle_rov_description(self, description: str):
        all_rovs = self.kb.get_all_rovs()
        candidates = self.extractor.resolve_rov_description(
            description, all_rovs, self.task_state.get("task_type_key")
        )
        self._pending_rov_candidates = candidates
        if candidates:
            self.task_state["_rov_candidates"] = [
                {"model": r["model"], "full_name": r["full_name"],
                 "category": r["category"], "available": True}
                for r in candidates[:3]
            ]

    # --------------------------------------------------------------------------
    # 约束检查（硬解除后检查软）
    # --------------------------------------------------------------------------

    def _run_constraint_check(self, changed_fields: set[str]) -> dict:
        """执行约束检查，返回上下文"""
        if not changed_fields and self.phase not in ("blocked_hard", "blocked_soft"):
            return {"type": "none", "violations": [], "hard_refusal_counts": {}}

        if self.phase in ("blocked_hard", "blocked_soft"):
            new_violations = self.validator.validate(self.task_state)
        else:
            new_violations = self.validator.validate_for_fields(self.task_state, changed_fields)

        # 处理soft阻塞解除/升级为hard
        if self.phase == "blocked_soft":
            current_soft = [v for v in new_violations
                            if v.severity == "soft" and not self._is_whitelisted(v)]
            if not current_soft:
                self._blocking_violations = []
                self.phase = "collecting"
                current_hard = [v for v in new_violations if v.severity == "hard"]
                if current_hard:
                    self.phase = "blocked_hard"
                    self._blocking_violations = current_hard
                    return {"type": "hard", "violations": current_hard, "hard_refusal_counts": {}}
                return {"type": "none", "violations": [], "hard_refusal_counts": {}}
            else:
                self._blocking_violations = current_soft
                return {"type": "soft", "violations": current_soft, "hard_refusal_counts": {}}

        # 处理hard阻塞解除
        if self.phase == "blocked_hard":
            current_hard = [v for v in new_violations if v.severity == "hard"]
            if current_hard:
                self._blocking_violations = current_hard
                for v in current_hard:
                    self._hard_refusal_counts[v.constraint_id] = \
                        self._hard_refusal_counts.get(v.constraint_id, 0) + 1
                final_ids = {cid for cid, cnt in self._hard_refusal_counts.items()
                             if cnt >= HARD_REFUSAL_LIMIT}
                if final_ids:
                    self.phase = "rejected"
                    self._blocking_violations = []
                    return {"type": "hard_rejected", "violations": current_hard,
                            "hard_refusal_counts": dict(self._hard_refusal_counts)}
                warn_ids = {cid for cid, cnt in self._hard_refusal_counts.items()
                            if cnt == HARD_REFUSAL_LIMIT - 1}
                ctx_type = "hard_final_warning" if warn_ids else "hard"
                return {"type": ctx_type, "violations": current_hard,
                        "hard_refusal_counts": dict(self._hard_refusal_counts)}
            else:
                # 硬约束解除，清除计数
                self._blocking_violations = []
                self.phase = "collecting"
                resolved_ids = set(self._hard_refusal_counts.keys()) - {v.constraint_id for v in new_violations if
                                                                        v.severity == "hard"}
                for cid in resolved_ids:
                    self._hard_refusal_counts.pop(cid, None)

                # 硬解除后检查软约束
                current_soft = [v for v in new_violations
                                if v.severity == "soft" and not self._is_whitelisted(v)]
                if current_soft:
                    self.phase = "blocked_soft"
                    self._blocking_violations = current_soft
                    return {"type": "soft", "violations": current_soft,
                            "hard_refusal_counts": {}}
                return {"type": "none", "violations": [], "hard_refusal_counts": {}}

        # collecting状态下的新违规
        if self.phase == "collecting":
            hard_new = [v for v in new_violations if v.severity == "hard"]
            soft_new = [v for v in new_violations
                        if v.severity == "soft" and not self._is_whitelisted(v)]

            if hard_new:
                self.phase = "blocked_hard"
                self._blocking_violations = hard_new
                for v in hard_new:
                    if v.constraint_id not in self._hard_refusal_counts:
                        self._hard_refusal_counts[v.constraint_id] = 0
                return {"type": "hard", "violations": hard_new,
                        "hard_refusal_counts": dict(self._hard_refusal_counts)}
            if soft_new:
                self.phase = "blocked_soft"
                self._blocking_violations = soft_new
                return {"type": "soft", "violations": soft_new, "hard_refusal_counts": {}}

        return {"type": "none", "violations": [], "hard_refusal_counts": {}}

    # --------------------------------------------------------------------------
    # 工具方法
    # --------------------------------------------------------------------------

    def _compute_changed_fields(self, updates: dict) -> set[str]:
        changed = set()
        skip = {"emergency_mode", "rov_description", "__clear_oilfield_name", "__clear_pending_oilfield"}
        if updates.get("__clear_oilfield_name") and self.task_state.get("oilfield_name"):
            changed.add("oilfield_name")
        for k, v in updates.items():
            if k in skip or v is None or v == "":
                continue
            if self.task_state.get(k) != v:
                changed.add(k)
        return changed

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
        return any(
            (f, str(self.task_state.get(f)), v.constraint_id) in self._soft_whitelist
            for f in v.related_fields
        )

    @staticmethod
    def _is_business_identity_query(message: str) -> bool:
        text = message.strip().lower()
        identity_patterns = (
            "你是什么", "你是谁", "你是啥", "你的身份", "你叫什么",
            "介绍一下你自己", "自我介绍", "what are you", "who are you",
        )
        return any(pattern in text for pattern in identity_patterns)


    @staticmethod
    def _user_confirmed(message: str) -> bool:
        keywords = ["确认", "没问题", "发布", "提交", "ok", "好的", "可以", "确定"]
        return any(kw in message.lower() for kw in keywords)

    @staticmethod
    def _user_cancelled(message: str) -> bool:
        keywords = ["取消", "放弃", "不要了", "终止", "退出"]
        return any(kw in message for kw in keywords)

    # --------------------------------------------------------------------------
    # 状态查询与重置
    # --------------------------------------------------------------------------

    def get_status(self) -> dict:
        filled: dict = {}
        missing_display: list[dict] = []

        for k, v in self._last_built_json.items():
            if k.startswith("_"):
                continue
            label = FIELD_LABELS.get(k, k)
            filled[k] = {"label": label, "value": v}

        for m in self._last_missing:
            missing_display.append({
                "key": m["key"],
                "label": m["label"],
                "allowed_values": m.get("allowed_values", []),
            })

        return {
            "phase": self.phase,
            "mode": self.mode,
            "filled": filled,
            "missing": missing_display,
            "whitelisted_soft": sorted({e[2] for e in self._soft_whitelist}),
        }

    def get_final_result(self) -> dict | None:
        return self.final_result

    def reset(self):
        self.conversation_history = []
        self.task_state = {}
        self.mode = "normal"
        self.phase = "collecting"
        self.final_result = None
        self.awaiting_final_confirm = False
        self.task_start_now = False
        self._blocking_violations = []
        self._soft_whitelist = set()
        self._hard_refusal_counts = {}
        self._pending_rov_candidates = []
        self._last_built_json = {}
        self._last_missing = []

    # --------------------------------------------------------------------------
    # 时间判断
    # --------------------------------------------------------------------------

    def is_start_time_near_now(self, time_window_minutes: int = 10) -> bool:
        try:
            start_time_str = self.task_state.get("start_time")
            if not start_time_str:
                return False

            # 使用模拟时间代替系统时间
            now = get_current_datetime()
            now = now.replace(microsecond=0)

            start_time_str = start_time_str.replace("T", " ").replace("：", ":").strip()
            if start_time_str.endswith("Z"):
                start_time_str = start_time_str[:-1] + "+00:00"
            start_time = datetime.fromisoformat(start_time_str)
            if start_time.tzinfo is None:
                start_time = start_time.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
            else:
                start_time = start_time.astimezone(ZoneInfo("Asia/Shanghai"))

            delta_seconds = (start_time - now).total_seconds()
            return 0 <= delta_seconds <= time_window_minutes * 60
        except Exception as e:
            print("时间判断出错:", e)
            return False

    # --------------------------------------------------------------------------
    # 缓存重建
    # --------------------------------------------------------------------------

    def _rebuild_cache(self) -> None:
        """根据当前 task_state 重新构建 _last_built_json 和 _last_missing"""
        task_type_key = self.task_state.get("task_type_key")
        if task_type_key:
            built, missing = self.builder.build(self.task_state, task_type_key, self.mode)
            if "task_id" in built and not self.task_state.get("task_id"):
                self.task_state["task_id"] = built["task_id"]
            self._last_built_json = built
            self._last_missing = missing
        else:
            self._last_built_json = {}
            self._last_missing = [{
                "key": "task_type",
                "label": "任务类型",
                "type": "string",
                "allowed_values": self.kb.get_all_task_type_values()
            }]
        self.task_start_now = self.is_start_time_near_now()

    # --------------------------------------------------------------------------
    # 历史快照恢复
    # --------------------------------------------------------------------------

    def load_snapshot(self, snapshot: dict) -> None:
        """从历史快照恢复对话管理器的状态"""
        # 恢复基本状态
        self.conversation_history = snapshot.get("conversation_history", [])
        self.task_state = snapshot.get("task_state", {})
        self.mode = snapshot.get("mode", "normal")
        self.phase = snapshot.get("phase", "collecting")
        self.final_result = None
        self.awaiting_final_confirm = False
        self.task_start_now = False
        # 清空阻塞与白名单，重新构建缓存
        self._blocking_violations = []
        self._soft_whitelist = set()
        self._hard_refusal_counts = {}
        self._pending_rov_candidates = []
        self._rebuild_cache()
