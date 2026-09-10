"""
src/handlers/grounded_catalog.py - 领域专有目录、能力问答与知识 Grounding 处理器

职责：
1. 真实装备阵列、作业任务、工具载荷、油田海域、准入规则专有知识介绍；
2. Grounded Recommendation：将模型推荐选择严格收敛至项目配置的合法候选；
3. 设备类别适用任务 Grounded 回答（依据 task_schemas/robot_fleet 权威边界）；
4. 文本领域实体快速解析提取（水下机器人、油田海域、载荷工具、任务类型）。
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from .base import BaseDialogueHandler
from ..intent_router import IntentRouteResult
from ..knowledge_retriever import (
    KnowledgeBase,
    format_seabed_type,
    format_telemetry_value,
)
from ..prompts import build_knowledge_responder_messages
from ..model_profile import ModelRole
from ..extractor import ParameterExtractor
from ..constants import (
    FIELD_LABELS as CORE_FIELD_LABELS,
    RECOMMENDATION_FIELD_BY_SUBJECT,
)

logger = logging.getLogger(__name__)

FIELD_LABELS = {
    **CORE_FIELD_LABELS,
    "task_type": "作业类型",
    "equipment_unit_id": "具体设备编号",
    "operating_mode": "作业模式",
    "pipeline_type": "管线类型",
    "target_depth": "作业水深",
    "start_point": "起始坐标",
    "end_point": "结束坐标",
    "work_duration": "作业时长",
    "payload": "搭载工具",
    "cleaning_tool": "清洗工具",
    "inspection_sensor": "巡检传感器",
    "operation_tool": "操作工具",
    "oilfield_name": "油田海域",
}


class GroundedCatalogHandler(BaseDialogueHandler):
    """领域目录介绍、能力问答与推荐约束处理器"""

    @property
    def task_state(self) -> dict:
        return getattr(self.manager, "task_state", {})

    @property
    def kb(self) -> Any:
        return getattr(self.manager, "kb", None)

    @property
    def builder(self) -> Any:
        return getattr(self.manager, "builder", None)

    @property
    def extractor(self) -> Any:
        return getattr(self.manager, "extractor", None)

    @property
    def mode(self) -> str:
        return getattr(self.manager, "mode", "normal")

    @property
    def conversation_history(self) -> list:
        return getattr(self.manager, "conversation_history", [])

    @property
    def _last_missing(self) -> list:
        return getattr(self.manager, "_last_missing", [])

    @property
    def _last_visible_catalog_items(self) -> list:
        return getattr(self.manager, "_last_visible_catalog_items", [])

    @_last_visible_catalog_items.setter
    def _last_visible_catalog_items(self, val: list) -> None:
        if hasattr(self.manager, "_last_visible_catalog_items"):
            self.manager._last_visible_catalog_items = val

    def missing_field_definition(self, key: str) -> dict | None:
        if hasattr(self.manager, "_missing_field_definition"):
            fn = getattr(self.manager, "_missing_field_definition")
            if callable(fn):
                return fn(key)
        return next(
            (
                item
                for item in self._last_missing
                if isinstance(item, dict) and item.get("key") == key
            ),
            None,
        )

    _missing_field_definition = missing_field_definition

    def build_grounded_recommendation(
        self,
        route: IntentRouteResult,
        user_message: str | None = None,
    ) -> str | None:
        """把模型的推荐选择约束到当前待填字段的配置候选中。

        设计原则：
        - 仅拦截 operation=READ、relation=recommend 的询问。
        - 合法候选（allowed_values）来自项目配置，是唯一可信来源。
        - LLM 的 subject_text 可能与配置名称存在出入（幻觉、别名等），
          因此优先从 allowed_values 中选取推荐值，而不依赖 subject_text 精确匹配。
        - 若 subject_type 无对应字段或当前任务无合法候选，则不拦截，
          让后续知识库检索逻辑处理。
        """
        plan = route.interaction_plan
        if plan is None or plan.operation != "READ" or plan.relation != "recommend":
            return None

        target_key = RECOMMENDATION_FIELD_BY_SUBJECT.get(plan.subject_type or "")
        if not target_key:
            # subject_type 不在推荐字段映射中，不拦截
            return None

        field_def = self.missing_field_definition(target_key)
        if not field_def and target_key == "equipment_family":
            legacy_def = self.missing_field_definition("equipment_class")
            if legacy_def:
                field_def = legacy_def
                target_key = "equipment_class"

        allowed_values = list((field_def or {}).get("allowed_values") or [])
        label = FIELD_LABELS.get(target_key) or (field_def or {}).get("label") or (target_key or "该字段")

        if not allowed_values:
            # 当前任务阶段无合法候选（字段尚未解析或不在缺失列表中），不拦截
            return None

        # OutputBuilder.build() 的 missing_fields 只承担确定性完整性校验，运行时
        # 不携带候选描述。推荐属于只读语义判断：从同一 task_state 下的权威 schema
        # 补齐别名和候选证据，但保留原 missing field 的 allowed_values 作为最终边界。
        semantic_field_def = dict(field_def or {})
        task_type_key = self.task_state.get("task_type_key")
        if task_type_key and self.builder:
            required = self.builder.get_required(
                task_type_key,
                self.mode,
                self.task_state,
            )
            match_keys = (target_key, "equipment_family") if target_key == "equipment_class" else (target_key,)
            authoritative = next(
                (
                    item
                    for item in required
                    if isinstance(item, dict) and item.get("key") in match_keys
                ),
                {},
            )
            allowed_set = set(allowed_values)
            for evidence_key in (
                "alias_mappings",
                "ambiguous_aliases",
                "candidate_evidence",
            ):
                value = authoritative.get(evidence_key)
                if evidence_key == "candidate_evidence" and isinstance(value, list):
                    filtered_items = []
                    for item in value:
                        if not isinstance(item, dict):
                            continue
                        canon = item.get("canonical_value")
                        aliases = item.get("aliases", [])
                        if canon in allowed_set:
                            filtered_items.append(item)
                        else:
                            matched_allowed = next((a for a in allowed_set if a == canon or a in aliases), None)
                            if matched_allowed:
                                item_copy = dict(item)
                                item_copy["canonical_value"] = matched_allowed
                                filtered_items.append(item_copy)
                    value = filtered_items
                if value:
                    semantic_field_def[evidence_key] = value
        semantic_field_def["allowed_values"] = allowed_values

        selected = plan.subject_text
        task_name = self.task_state.get("task_type") or self.task_state.get("task_type_key")
        task_prefix = f"针对当前【{task_name}】任务，" if task_name else ""

        # 兼容直接调用：没有用户原句时，模型初选若已是合法候选可直接使用。
        # 真实对话中则必须结合用户原句和候选证据复核，避免 TurnPlanner 在缺少
        # 候选说明时碰巧输出一个合法、但并不符合用户偏好的枚举值。
        if not user_message and selected and selected in allowed_values:
            chosen = selected
            return (
                f"{task_prefix}我明确推荐{label}【{chosen}】。"
                "本轮仅提供建议，尚未写入任务。若接受，请确认采用该选择。"
            )

        if len(allowed_values) == 1:
            chosen = allowed_values[0]
            return (
                f"{task_prefix}当前任务的{label}推荐选项为【{chosen}】。"
                "本轮建议采用该值，尚未写入任务。若接受，请确认采用该选择。"
            )

        # 有原始用户表达时以它为唯一偏好证据；TurnPlanner 初选可能缺少候选说明，
        # 把它再次塞给消歧模型反而会制造冲突。仅在没有原句的兼容调用中使用初选。
        semantic_input = user_message or selected or ""
        if semantic_input:
            chosen = (
                ParameterExtractor._match_allowed_value(
                    semantic_input,
                    allowed_values,
                )
                or ParameterExtractor._match_alias_value(
                    semantic_input,
                    semantic_field_def,
                )
            )
            if chosen in allowed_values:
                return (
                    f"{task_prefix}我明确推荐{label}【{chosen}】。"
                    "本轮仅提供建议，尚未写入任务。若接受，请确认采用该选择。"
                )
            chosen = self.extractor.resolve_allowed_candidate(
                semantic_input,
                target_key,
                semantic_field_def,
                current_state=self.task_state,
                conversation_history=self.conversation_history,
            )
            if chosen in allowed_values:
                return (
                    f"{task_prefix}我明确推荐{label}【{chosen}】。"
                    "本轮仅提供建议，尚未写入任务。若接受，请确认采用该选择。"
                )
            # 用户没有给出足以区分候选的偏好时，允许 TurnPlanner 在合法域内
            # 直接做一次语义选择。这不是按列表顺序默认；selected 必须是模型明确
            # 输出且可通过当前字段别名归一到 allowed_values。包含明确偏好时，
            # 上面的证据解析优先。
            if selected:
                selected_chosen = (
                    ParameterExtractor._match_allowed_value(selected, allowed_values)
                    or ParameterExtractor._match_alias_value(
                        selected,
                        semantic_field_def,
                    )
                )
                if selected_chosen in allowed_values:
                    return (
                        f"{task_prefix}我明确推荐{label}【{selected_chosen}】。"
                        "本轮仅提供建议，尚未写入任务。若接受，请确认采用该选择。"
                    )
                selected_chosen = self.extractor.resolve_allowed_candidate(
                    selected,
                    target_key,
                    semantic_field_def,
                    current_state=self.task_state,
                    conversation_history=self.conversation_history,
                )
                if selected_chosen in allowed_values:
                    return (
                        f"{task_prefix}我明确推荐{label}【{selected_chosen}】。"
                        "本轮仅提供建议，尚未写入任务。若接受，请确认采用该选择。"
                    )
            if selected in allowed_values:
                return (
                    f"{task_prefix}我明确推荐{label}【{selected}】。"
                    "本轮仅提供建议，尚未写入任务。若接受，请确认采用该选择。"
                )
            logger.info(
                "[GROUNDED_RECOMMEND] subject_text=%r 无法唯一映射到合法%s候选，"
                "allowed=%r",
                selected,
                label,
                allowed_values,
            )

        # 多候选仍无法消歧时只展示权威候选，不用列表顺序伪造推荐。
        candidates = "、".join(map(str, allowed_values))
        return (
            f"{task_prefix}当前任务允许的{label}选项有：{candidates}。\n"
            "目前信息不足以可靠推荐其中一个，请补充偏好或作业侧重点。"
            "本轮尚未写入任务。"
        )

    _build_grounded_recommendation = build_grounded_recommendation

    def resolve_project_robot_classes(self, text: str) -> list[tuple[str, str]]:
        """仅依据 robot_fleet 配置识别文本中明确提到的机器人类别。"""
        raw_text = str(text or "")
        compact = raw_text.lower().replace(" ", "")
        matched: set[str] = set()
        classes = self.kb.get_robot_classes()

        for class_id, config in classes.items():
            names = [class_id, config.get("full_name")]
            if any(
                str(name).lower().replace(" ", "") in compact
                for name in names
                if name
            ):
                matched.add(class_id)

        if re.search(r"(?<![A-Za-z0-9_])ROV(?![A-Za-z0-9_])", raw_text, re.IGNORECASE):
            task_type_key = self.task_state.get("task_type_key")
            if task_type_key:
                domain = self.kb.get_feasible_robot_selection_domain(
                    task_type_key,
                    self.task_state,
                )
                rov_classes = [
                    node.get("class_id")
                    for node in domain.get("classes", [])
                    if node.get("class_id") in classes
                    and (
                        str(node.get("class_id")).endswith("_rov")
                        or "ROV" in str(classes[node.get("class_id")].get("full_name") or "")
                    )
                ]
                if len(rov_classes) == 1:
                    matched.add(rov_classes[0])

        for family in self.kb.robot_fleet.get("robot_families", {}).values():
            class_id = family.get("robot_class")
            if class_id not in classes:
                continue
            names = [family.get("full_name"), *(family.get("aliases") or [])]
            if any(
                str(name).lower().replace(" ", "") in compact
                for name in names
                if name
            ):
                matched.add(class_id)

        return [
            (class_id, config.get("full_name", class_id))
            for class_id, config in classes.items()
            if class_id in matched
        ]

    _resolve_project_robot_classes = resolve_project_robot_classes

    def extract_robot_entity_from_text(self, text: str) -> str | None:
        """从文本中提取提及的水下机器人名称、系列或型号代号。"""
        if not text:
            return None
        raw = str(text).strip()
        families = self.kb.robot_fleet.get("robot_families", {})
        for fid, f_cfg in families.items():
            names = [f_cfg.get("full_name"), *(f_cfg.get("aliases") or [])]
            for n in names:
                if n and n in raw:
                    return f_cfg.get("full_name") or fid
        units = self.kb.robot_fleet.get("fleet_units", [])
        if isinstance(units, dict):
            units_list = list(units.values())
        elif isinstance(units, list):
            units_list = units
        else:
            units_list = []
        for u_cfg in units_list:
            if isinstance(u_cfg, dict):
                uid = u_cfg.get("id") or u_cfg.get("unit_id") or ""
                if uid and uid in raw:
                    return uid
        classes = self.kb.get_robot_classes()
        for cid, c_cfg in classes.items():
            name = c_cfg.get("full_name", cid)
            if name in raw or cid in raw:
                return name
        return None

    _extract_robot_entity_from_text = extract_robot_entity_from_text

    def extract_oilfield_entity_from_text(self, text: str) -> str | None:
        """从文本中提取提及的水下油气田或作业海域名称。"""
        if not text:
            return None
        raw = str(text).strip()
        oilfields = (
            self.kb.environment.get("oil_fields")
            or self.kb.environment.get("oilfields")
            or []
            if hasattr(self.kb, "environment")
            else []
        )
        if isinstance(oilfields, dict):
            oilfields_list = list(oilfields.values())
        elif isinstance(oilfields, list):
            oilfields_list = oilfields
        else:
            oilfields_list = []
        for o_cfg in oilfields_list:
            if isinstance(o_cfg, dict):
                name = o_cfg.get("name") or o_cfg.get("oilfield_name") or ""
                aliases = o_cfg.get("aliases") or []
                if (name and name in raw) or any(a in raw for a in aliases if a):
                    return name
        return None

    _extract_oilfield_entity_from_text = extract_oilfield_entity_from_text

    def extract_payload_entity_from_text(self, text: str) -> str | None:
        """从文本中提取提及的水下载荷或机械工器具名称。"""
        if not text:
            return None
        raw = str(text).strip()
        payload_catalog = getattr(self.kb, "onboard_payloads", {}).get("payload_catalog", {}) if hasattr(self.kb, "onboard_payloads") else {}
        for pid, p_cfg in payload_catalog.items():
            name = p_cfg.get("name") or pid
            aliases = p_cfg.get("aliases") or []
            if name in raw or any(a in raw for a in aliases if a):
                return name
        kw_map = {
            "摄像机": "高清水下摄像机",
            "声呐": "前视避障声呐",
            "激光": "水下激光标尺",
            "机械手": "七功能液压机械手",
            "腐蚀": "阴极保护腐蚀检测仪",
            "厚度": "超声波厚度传感器",
        }
        for kw, canonical in kw_map.items():
            if kw in raw:
                return canonical
        return None

    _extract_payload_entity_from_text = extract_payload_entity_from_text

    def extract_task_type_from_text(self, text: str) -> str | None:
        """从文本中提取明确提及或匹配的水下任务类型 Key。"""
        if not text:
            return None
        raw = str(text).lower()
        if any(kw in raw for kw in ("巡检", "管缆巡检", "管道巡检", "电缆巡检", "pipeline_inspection")):
            return "pipeline_inspection"
        if any(kw in raw for kw in ("埋设", "管缆埋设", "开沟埋设", "埋缆", "pipeline_burial")):
            return "pipeline_burial"
        if any(kw in raw for kw in ("采油树", "阀门", "控制面板", "阀门操作", "tree_valve_operation")):
            return "tree_valve_operation"
        return None

    _extract_task_type_from_text = extract_task_type_from_text

    def build_grounded_single_task_introduction(self, task_type_key: str) -> str:
        """依据 task_schemas 配置生成单个任务类型的权威介绍。"""
        templates = self.kb.task_schemas.get("task_templates", {})
        tv = templates.get(task_type_key, {})
        name = tv.get("display_name") or tv.get("name") or task_type_key
        desc = tv.get("description") or ""
        allowed_classes = tv.get("allowed_robot_classes") or tv.get("allowed_equipment_classes") or []
        cn_classes = []
        for cid in allowed_classes:
            c_info = self.kb.get_robot_classes().get(cid, {})
            cn_classes.append(c_info.get("full_name") or cid)
        class_str = f"，适用装备：{' / '.join(cn_classes)}" if cn_classes else ""
        desc_clean = desc.rstrip("。")
        return (
            f"**{name}**：{desc_clean}{class_str}。\n\n"
            f"如需开启该任务规划，请回复“开始这个任务”或直接提供作业参数（如水深、区域或机器人）。"
        )

    _build_grounded_single_task_introduction = build_grounded_single_task_introduction

    def build_grounded_fleet_introduction(self) -> str:
        """依据 robot_fleet 配置返回真实水下机器人与作业装备阵列介绍。"""
        classes = self.kb.get_robot_classes()
        families = self.kb.robot_fleet.get("robot_families", {})
        lines = ["本系统当前支持以下水下机器人与作业装备阵列："]
        visible_items = []
        for idx, (cid, cinfo) in enumerate(classes.items(), start=1):
            c_name = cinfo.get("full_name", cid)
            visible_items.append({"index": idx, "name": c_name, "type": "device_class", "key": cid})
            f_names = [f.get("full_name") for f in families.values() if f.get("robot_class") == cid and f.get("full_name")]
            aliases = []
            for f in families.values():
                if f.get("robot_class") == cid and f.get("aliases"):
                    aliases.extend([a for a in f.get("aliases") if "座" in a or "HP" in a or "马力" in a])
            alias_str = f"，涵盖系列代号：{'/'.join(list(dict.fromkeys(aliases))[:3])}" if aliases else ""
            f_str = f"包含 {', '.join(f_names)}" if f_names else ""
            lines.append(f"{idx}. **{c_name}**：{f_str}{alias_str}。".strip())

        self._last_visible_catalog_items = visible_items
        lines.append("\n您可以指定具体的机器人类别或系列代号，也可由系统根据任务水深与作业需求自动为您匹配推荐。")
        return "\n".join(lines)

    _build_grounded_fleet_introduction = build_grounded_fleet_introduction

    def build_grounded_task_catalog_introduction(self) -> str:
        """依据 task_schemas 配置返回真实水下作业任务类型清单。"""
        templates = self.kb.task_schemas.get("task_templates", {})
        lines = ["本系统当前支持以下水下作业任务类型："]
        visible_items = []
        for idx, (tk, tv) in enumerate(templates.items(), start=1):
            name = tv.get("display_name") or tv.get("name") or tk
            visible_items.append({"index": idx, "name": name, "type": "task_type", "key": tk})
            desc = tv.get("description") or ""
            allowed_classes = tv.get("allowed_robot_classes") or tv.get("allowed_equipment_classes") or []
            cn_classes = []
            for cid in allowed_classes:
                c_info = self.kb.get_robot_classes().get(cid, {})
                cn_classes.append(c_info.get("full_name") or cid)
            class_str = f"，适用装备：{' / '.join(cn_classes)}" if cn_classes else ""
            desc_clean = desc.rstrip("。")
            lines.append(f"{idx}. **{name}**：{desc_clean}{class_str}。".strip())
        self._last_visible_catalog_items = visible_items
        lines.append("\n您可以输入具体任务指令（例如“帮我安排一个管缆巡检任务”），系统将引导您收集参数并完成校验发布。")
        return "\n".join(lines)

    _build_grounded_task_catalog_introduction = build_grounded_task_catalog_introduction

    def build_grounded_tool_catalog_introduction(self) -> str:
        """依据 robot_fleet 配置返回真实水下载荷、工具与传感器清单。"""
        payloads = self.kb.robot_fleet.get("onboard_payloads", {})
        categories: dict[str, list[str]] = {}
        for pk, pv in payloads.items():
            cat = pv.get("category") or "通用载荷"
            name = pv.get("name") or pk
            categories.setdefault(cat, []).append(name)
        lines = ["本系统当前支持以下水下载荷、工具与传感器配置："]
        visible_items = []
        for idx, (cat, items) in enumerate(categories.items(), start=1):
            visible_items.append({"index": idx, "name": cat, "type": "tool_category", "key": cat, "items": items})
            lines.append(f"{idx}. **{cat}**：{'、'.join(items)}")
        self._last_visible_catalog_items = visible_items
        lines.append("\n创建任务时，您可以随时为机器人挂载或卸载特定载荷（例如：“机械臂带上海胆式刷洗工具”）。")
        return "\n".join(lines)

    _build_grounded_tool_catalog_introduction = build_grounded_tool_catalog_introduction

    def build_grounded_oilfield_catalog_introduction(self) -> str:
        """依据 environment_info 配置返回收录的水下油气田清单。"""
        oil_fields = self.kb.environment.get("oil_fields", [])
        lines = ["本系统知识库目前收录的水下油气田与作业海域包括："]
        visible_items = []
        for idx, of in enumerate(oil_fields, start=1):
            name = of.get("name") or "未命名油田"
            visible_items.append({"index": idx, "name": name, "type": "environment", "key": of.get("id")})
            depth = of.get("water_depth")
            max_d = of.get("maximum_reference_water_depth")
            seabed_raw = of.get("seabed_type") or "未知"
            seabed_cn = format_seabed_type(seabed_raw)
            lines.append(f"{idx}. **{name}**：参考水深 {depth}m，校验上限 {max_d}m，{seabed_cn}。")
        self._last_visible_catalog_items = visible_items
        lines.append("\n系统会根据您选择的油气田自动校验水下机器人的额定耐压水深与履带接地比压。")
        return "\n".join(lines)

    _build_grounded_oilfield_catalog_introduction = build_grounded_oilfield_catalog_introduction

    def build_grounded_rule_catalog_introduction(self) -> str:
        """返回系统安全准入与硬约束校验规则说明。"""
        return (
            "本系统在任务创建与发布前会自动执行以下四项严格的物理与物理安全约束校验：\n"
            "1. **耐压水深与接地比压**：校验机器人额定最大深度是否满足目标油气田水深，履带式机器人额外校验海床硬度与接地比压；\n"
            "2. **海流与水体能见度**：校验现场海流是否超过 3.0 节抗流上限，能见度是否低于 0.5 米；\n"
            "3. **地理与禁航区限制**：校验作业起始点与终点坐标是否侵入生态敏感保护区或危险禁航区；\n"
            "4. **DVL底锁与配载浮力**：校验海床泥沙高度是否引发DVL失锁风险，以及工具载荷配平与电力配额。\n\n"
            "存在任何硬约束违规时系统将阻断任务发布并引导修正。"
        )

    _build_grounded_rule_catalog_introduction = build_grounded_rule_catalog_introduction

    def build_grounded_device_class_answer(
        self,
        user_message: str,
        route: IntentRouteResult,
    ) -> str | None:
        """用 task_schemas/robot_fleet 回答类别适用任务，禁止自由补充项目事实。"""
        plan = route.interaction_plan
        if (
            plan is None
            or plan.operation != "READ"
            or plan.subject_type != "device_class"
            or plan.relation not in {"compare", "supports", "capabilities", "describe", "list"}
        ):
            return None

        if any(kw in user_message for kw in ("所有", "全部", "清单", "有哪些机器人", "支持的所有机器人", "当前支持")):
            return None

        mentioned = self.resolve_project_robot_classes(
            f"{plan.subject_text or ''} {user_message}"
        )
        if not mentioned:
            return None

        templates = self.kb.task_schemas.get("task_templates", {})
        task_type_key = self.task_state.get("task_type_key")

        robot_classes = self.kb.get_robot_classes()
        robot_families = self.kb.robot_fleet.get("robot_families", {})

        required = (
            self.builder.get_required(
                task_type_key,
                self.mode,
                self.task_state,
            )
            if task_type_key
            else []
        )
        class_field = next(
            (field for field in required if isinstance(field, dict) and field.get("key") == "equipment_class"),
            {},
        ) if required else {}
        evidence_by_name = {
            item.get("canonical_value"): item
            for item in class_field.get("candidate_evidence", [])
            if isinstance(item, dict) and item.get("canonical_value")
        }

        results = []
        for class_id, class_name in mentioned:
            supported_tasks: list[str] = []
            for task_key, template in templates.items():
                domain = self.kb.get_feasible_robot_selection_domain(task_key)
                if any(node.get("class_id") == class_id for node in domain.get("classes", [])):
                    supported_tasks.append(template.get("display_name", task_key))

            class_info = robot_classes.get(class_id, {})
            assoc_families = [
                {
                    "family_id": f.get("family_id"),
                    "full_name": f.get("full_name"),
                    "aliases": f.get("aliases", []),
                }
                for f in robot_families.values()
                if isinstance(f, dict) and f.get("robot_class") == class_id
            ]

            results.append({
                "class_id": class_id,
                "full_name": class_name,
                "supported_tasks": supported_tasks,
                "class_info": class_info,
                "associated_families": assoc_families,
                "candidate_evidence": evidence_by_name.get(class_name, {}),
            })

        kb_evidence = {
            "found": True,
            "query_type": "DEVICE_CAPABILITY",
            "query_mode": "device_class_compare" if plan.relation == "compare" else "device_class_describe",
            "relation": plan.relation,
            "task_type": self.task_state.get("task_type") or task_type_key,
            "results": results,
            "note": "本轮未创建或修改任务，仅为只读信息展示",
        }

        messages = build_knowledge_responder_messages(
            kb_evidence,
            self.conversation_history,
            user_message,
            task_state=self.task_state,
        )
        reply = self._call_safe_llm_chat(
            messages,
            temperature=0.1,
            role=ModelRole.KNOWLEDGE_QA,
        )
        if reply and reply.strip() and reply.strip() != "不应调用自由回答模型":
            return self._call_safe_llm_filter_reply(
                reply,
                role=ModelRole.FILTER_REPLY,
            )

        lines = ["依据项目配置："]
        for item in results:
            class_name = item["full_name"]
            supported_tasks = item["supported_tasks"]
            rendered = "、".join(supported_tasks) if supported_tasks else "暂无已配置的适用任务"
            lines.append(f"- 【{class_name}】：{rendered}。")
        lines.append("以上仅说明项目知识库中已配置的适用关系，本轮未创建或修改任务。")
        return "\n".join(lines)

    _build_grounded_device_class_answer = build_grounded_device_class_answer

    def _call_safe_llm_chat(self, messages: list[dict], temperature: float = 0.7, role: ModelRole | str | None = None) -> str:
        fn = getattr(self.manager, "_safe_llm_chat", None)
        if callable(fn):
            return fn(messages, temperature=temperature, role=role)
        llm = getattr(self.manager, "llm", None)
        if not llm:
            return ""
        try:
            return llm.chat(messages, temperature=temperature, role=role)
        except Exception as e:
            logger.error("[GroundedCatalogHandler] safe_llm_chat failed: %s", e)
            return ""

    def _call_safe_llm_filter_reply(self, reply: str, role: ModelRole = ModelRole.FILTER_REPLY) -> str:
        fn = getattr(self.manager, "_safe_llm_filter_reply", None)
        if callable(fn):
            return fn(reply, role=role)
        return reply

    def can_handle(self, ctx: Any) -> bool:
        return False

    def handle(self, ctx: Any) -> Any:
        raise NotImplementedError
