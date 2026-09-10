"""
src/knowledge/query_executor.py — 强类型只读知识查询、海域环境检索与设备别名索引执行器
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from .models import _norm

if TYPE_CHECKING:
    from ..knowledge_retriever import KnowledgeBase


class QueryExecutor:
    """强类型知识查询与别名索引执行器。"""

    def __init__(self, kb: "KnowledgeBase"):
        self.kb = kb

    @property
    def environment(self) -> dict:
        return self.kb.environment

    @property
    def assets(self) -> dict:
        return self.kb.assets

    @property
    def task_schemas(self) -> dict:
        return self.kb.task_schemas

    @property
    def constraints(self) -> list:
        return self.kb.constraints

    @property
    def robot_fleet(self) -> dict:
        return self.kb.robot_fleet

    @property
    def env_info(self) -> Any:
        return self.kb.env_info

    @property
    def state_info(self) -> Any:
        return self.kb.state_info

    def get_vessel(self, vessel_id: str) -> dict | None:
        vid_lower = vessel_id.lower().replace(" ", "")
        for v in self.assets["vessels"]:
            targets = [v["id"].lower()] + [a.lower().replace(" ", "") for a in v.get("aliases", [])]
            if any(vid_lower in t or t in vid_lower for t in targets):
                return v
        return None

    def get_task_schema(self, template_key: str) -> dict:
        """返回指定任务模板，兼容 main 的公开查询接口。"""
        return self.task_schemas.get("task_templates", {}).get(template_key, {})

    def get_task_type_map(self) -> dict[str, str]:
        """
        从 task_schemas.yaml 动态构建 {task_type_value: template_key} 反查字典。
        例如：{"管缆巡检": "pipeline_inspection",
               "采油树控制面板插入": "tree_valve_operation", ...}
        """
        mapping: dict[str, str] = {}
        for template_key, cfg in self.task_schemas["task_templates"].items():
            for value in cfg.get("task_type_values", []):
                mapping[value] = template_key
        return mapping

    def get_all_task_type_values(self) -> list[str]:
        """返回所有合法 task_type 值的平铺列表（供 LLM 提示和拒绝判断用）"""
        values: list[str] = []
        for cfg in self.task_schemas["task_templates"].values():
            values.extend(cfg.get("task_type_values", []))
        return values

    def get_environment_for_coords(self, coords: dict) -> dict | None:
        if not isinstance(coords, dict):
            return None
        lat = coords.get("lat")
        lon = coords.get("lon")
        if lat is None or lon is None:
            return None
        for area in self.environment["oil_fields"]:
            if area["lat_range"][0] <= lat <= area["lat_range"][1]:
                if area["lon_range"][0] <= lon <= area["lon_range"][1]:
                    return area
        return None

    def get_constraints(self) -> list[dict]:
        return self.constraints

    def get_environment_info_dict(self, coords: dict) -> dict:
        """根据坐标返回动态环境信息（随机 + 未知）"""
        empty_info = {
            "forbidden": None,
            "seabed_type": None,
            "obstacle_density": None,
            "acoustic_signal": None,
            "dvl_risk": None,
            "mothership_support": None,
        }
        if not isinstance(coords, dict):
            return empty_info
        lat = coords.get("lat")
        lon = coords.get("lon")
        if lat is None or lon is None:
            return empty_info
        return self.env_info.get_all_info(lat, lon)

    def get_robot_state_dict(self, equipment_selector: str) -> dict:
        empty_state = {
            "version": None,
            "water_current_velocity": None,
            "water_turbidity": None,
            "battery_percent": None,
            "current_mode": None,
            "communication_status": None,
            "latitude": None,
            "longitude": None,
            "update_timestamp": None,
            "confidence": None,
            "obstacle_density": None,
            "mothership_support": None,
            "overall_status": None,
            "survival_status": None,
            "thruster_status": None,
            "depth_keeping_status": None,
            "sonar_status": None,
            "vision_status": None,
            "arm_status": None,
            "end_effector_status": None,
            "acoustic_comms_status": None,
            "tether_connection_status": None,
        }
        if not equipment_selector or not isinstance(equipment_selector, str):
            return empty_state

        state = self.state_info.get_all_info(equipment_selector)
        if isinstance(state, dict):
            return state
        return empty_state

    def get_unit_state_snapshot(self, unit_id: str) -> dict:
        return self.state_info.get_unit_state_snapshot(unit_id)

    def check_runtime_availability(self, unit_id: str, *, max_age_seconds: int = 600) -> dict:
        return self.state_info.check_runtime_availability(unit_id, max_age_seconds=max_age_seconds)

    def get_device_alias_index(self) -> dict[str, list[str]]:
        """按类别、系列、型号、单机分层构建设备别名索引。

        索引值带有实体层级前缀，只用于只读路由和歧义判断，避免同名 ID
        在不同层级之间被误认为同一个实体。
        """
        index: dict[str, set[str]] = {}
        display_aliases: dict[str, str] = {}

        def add(alias: Any, target: str) -> None:
            if not isinstance(alias, str):
                return
            display = alias.strip()
            normalized = _norm(display)
            if not normalized:
                return
            canonical = display_aliases.setdefault(normalized, display)
            index.setdefault(canonical, set()).add(target)

        for class_id, robot_class in self.kb.get_robot_classes().items():
            target = f"class:{class_id}"
            add(class_id, target)
            add(robot_class.get("full_name"), target)
            for alias in robot_class.get("aliases", []):
                add(alias, target)

        for family_id, family in self.robot_fleet.get("robot_families", {}).items():
            target = f"family:{family_id}"
            add(family_id, target)
            add(family.get("full_name"), target)
            for alias in family.get("aliases", []):
                add(alias, target)

        for variant_id, variant in self.robot_fleet.get("model_variants", {}).items():
            target = f"variant:{variant_id}"
            add(variant_id, target)
            add(variant.get("full_name"), target)
            for alias in variant.get("aliases", []):
                add(alias, target)

        for unit in self.robot_fleet.get("fleet_units", []):
            unit_id = unit.get("unit_id")
            if not unit_id:
                continue
            target = f"unit:{unit_id}"
            for field in ("unit_id", "display_name", "serial_no", "status_ref"):
                add(unit.get(field), target)
            serial_no = str(unit.get("serial_no") or "").lstrip("0")
            if serial_no.isdigit():
                num = int(serial_no)
                cn_num = {1: "一", 2: "二", 3: "三", 4: "四", 5: "五", 6: "六", 7: "七", 8: "八", 9: "九"}.get(num, str(num))
                add(f"{num}号机", target)
                add(f"{cn_num}号机", target)
            for alias in unit.get("aliases", []):
                add(alias, target)

        return {alias: sorted(targets) for alias, targets in index.items()}

    def get_ambiguous_device_terms(self) -> set[str]:
        """返回映射到多个分层实体的设备别名。"""
        return {
            alias
            for alias, targets in self.get_device_alias_index().items()
            if len(targets) > 1
        }

    def get_all_device_terms(self) -> set[str]:
        """返回可安全用于意图路由的非歧义设备词集合。"""
        alias_index = self.get_device_alias_index()
        ambiguous = {
            alias for alias, targets in alias_index.items() if len(targets) > 1
        }
        terms = {
            alias
            for alias in alias_index
            if alias not in ambiguous
            and len(alias.strip()) >= 2
            and not alias.strip().isdigit()
        }
        for class_id, robot_class in self.kb.get_robot_classes().items():
            terms.add(class_id)
            full_name = robot_class.get("full_name")
            if isinstance(full_name, str) and full_name.strip():
                terms.add(full_name.strip())
        return terms

    def get_environment_alias_index(self) -> dict[str, list[str]]:
        """构建油气田、禁入保护区与DVL风险区的多维度别名索引。"""
        index: dict[str, set[str]] = {}
        display_aliases: dict[str, str] = {}

        def add(alias: Any, target: str) -> None:
            if not isinstance(alias, str):
                return
            display = alias.strip()
            normalized = _norm(display)
            if not normalized:
                return
            canonical = display_aliases.setdefault(normalized, display)
            index.setdefault(canonical, set()).add(target)

        for field in self.environment.get("oil_fields", []):
            field_id = field.get("id")
            if not field_id:
                continue
            target = f"oil_field:{field_id}"
            add(field_id, target)
            add(field.get("name"), target)
            for alias in field.get("aliases", []):
                add(alias, target)

        for area in self.environment.get("forbidden_areas", []):
            area_id = area.get("id")
            if not area_id:
                continue
            target = f"forbidden_area:{area_id}"
            add(area_id, target)
            add(area.get("name"), target)
            for alias in area.get("aliases", []):
                add(alias, target)

        for dvl in self.environment.get("dvl_bottom_lock_failure_areas", []):
            dvl_id = dvl.get("id")
            if not dvl_id:
                continue
            target = f"dvl_area:{dvl_id}"
            add(dvl_id, target)
            add(dvl.get("name"), target)
            for alias in dvl.get("aliases", []):
                add(alias, target)

        return {alias: sorted(targets) for alias, targets in index.items()}

    def _find_environment_entity_targets(self, user_message: str) -> tuple[str | None, list[str]]:
        message_norm = _norm(user_message)
        if not message_norm:
            return None, []
        matches = [
            (alias, targets)
            for alias, targets in self.get_environment_alias_index().items()
            if _norm(alias) and _norm(alias) in message_norm
        ]
        if not matches:
            return None, []
        matches.sort(key=lambda item: len(_norm(item[0])), reverse=True)
        return matches[0]

    def _resolve_typed_read_query(
        self,
        query_type: str,
        user_message: str,
        context: dict,
    ) -> tuple[str, str]:
        """用结构化主题和权威别名选择事实域，不重新猜测自然语言意图。"""
        if (
            context.get("relation") == "status"
            or context.get("source_policy") == "realtime_state"
        ):
            return query_type, user_message

        if query_type == "ENVIRONMENT_QUERY":
            return query_type, user_message

        if query_type != "KNOWLEDGE_QA":
            return query_type, user_message

        subject_type = context.get("subject_type")
        subject_text = context.get("subject_text")
        device_subject_types = {"device", "device_class", "device_family"}
        env_subject_types = {"environment", "oilfield", "location"}
        neutral_subject_types = {None, "unknown", "general_concept"}

        # 优先检查设备实体
        device_candidates: list[str] = []
        if subject_type in device_subject_types:
            if isinstance(subject_text, str) and subject_text.strip():
                device_candidates.append(subject_text.strip())
            device_candidates.append(user_message)
        elif subject_type in neutral_subject_types:
            device_candidates.append(user_message)

        for selector in device_candidates:
            _, entity_targets = self._find_query_entity_targets(selector)
            if not entity_targets:
                continue
            if selector == user_message:
                return "DEVICE_CAPABILITY", user_message
            return "DEVICE_CAPABILITY", f"{selector} {user_message}"

        # 检查环境/油田实体
        env_candidates: list[str] = []
        if subject_type in env_subject_types:
            if isinstance(subject_text, str) and subject_text.strip():
                env_candidates.append(subject_text.strip())
            env_candidates.append(user_message)
        elif subject_type in neutral_subject_types:
            env_candidates.append(user_message)

        env_generic_keywords = (
            "油田", "气田", "油气田", "海域", "禁入区", "保护区", "dvl", "底锁", "环境", "水深上限"
        )
        for selector in env_candidates:
            _, entity_targets = self._find_environment_entity_targets(selector)
            if entity_targets:
                if selector == user_message:
                    return "ENVIRONMENT_QUERY", user_message
                return "ENVIRONMENT_QUERY", f"{selector} {user_message}"
            if any(k in _norm(selector) for k in env_generic_keywords):
                return "ENVIRONMENT_QUERY", user_message

        return query_type, user_message

    @staticmethod
    def _match_payload_catalog(
        payload_catalog: dict,
        user_message: str,
    ) -> list[dict]:
        message_key = _norm(user_message)
        canonical_matches = []
        alias_matches = []

        for payload_info in payload_catalog.values():
            if not isinstance(payload_info, dict):
                continue
            name_key = _norm(payload_info.get("name"))
            if name_key and name_key in message_key:
                canonical_matches.append(payload_info)
                continue

            aliases = payload_info.get("aliases", [])
            if any(
                alias_key and alias_key in message_key
                for alias_key in (_norm(alias) for alias in aliases)
            ):
                alias_matches.append(payload_info)

        return canonical_matches or alias_matches

    def execute_typed_query(
        self,
        query_type: str,
        user_message: str,
        context: dict | None = None,
    ) -> dict:
        """执行强类型只读知识查询，并返回稳定的结构化证据。"""
        context = context if isinstance(context, dict) else {}
        requested_query_type = query_type
        query_type, retrieval_message = self._resolve_typed_read_query(
            query_type, user_message, context
        )
        response = {
            "query_type": query_type,
            "requested_query_type": requested_query_type,
            "results": [],
            "found": False,
            "source": "knowledge_base",
            "version": "kb_1.1_hierarchical",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

        if query_type == "TOOL_QUERY":
            task_type_key = context.get("task_type_key")
            user_reqs = context.get("user_requirements") if isinstance(context.get("user_requirements"), dict) else {}
            selected_equipment_type = (
                context.get("equipment_type")
                or user_reqs.get("equipment_type")
                or user_reqs.get("equipment_family")
                or user_reqs.get("equipment_class")
            )
            robots = (
                self.kb.get_task_allowed_robot_variants(task_type_key)
                if task_type_key
                else self.kb.get_all_rovs()
            )
            tool_set: set[str] = set()
            equipment_mappings: list[dict] = []
            selected_equipment_mapping = None

            for robot in robots:
                onboard = list(robot.get("onboard_payloads", []))
                supported = list(robot.get("supported_payloads", []))
                all_p = list(robot.get("all_payloads", []))
                tool_set.update(all_p)
                mapping_item = {
                    "equipment_type": robot.get("full_name"),
                    "variant_id": robot.get("variant_id"),
                    "family_id": robot.get("family_id"),
                    "robot_class": robot.get("robot_class_name"),
                    "onboard_payloads": onboard,
                    "supported_payloads": supported,
                    "all_payloads": all_p,
                }
                equipment_mappings.append(mapping_item)

                if selected_equipment_type and not selected_equipment_mapping:
                    if selected_equipment_type in (
                        robot.get("full_name"),
                        robot.get("variant_id"),
                        robot.get("family_id"),
                        robot.get("robot_class_name"),
                    ):
                        selected_equipment_mapping = mapping_item

            task_payloads = self.assets.get("payload_options", {})
            payload_catalog = self.assets.get("payload_catalog", {})

            matched_payloads = self._match_payload_catalog(
                payload_catalog,
                user_message,
            )

            current_suggestions = (
                task_payloads.get(task_type_key, {}) if task_type_key else {}
            )
            response["results"] = [
                {"category": "all_supported_tools", "tools": sorted(tool_set)},
                {
                    "category": "equipment_payload_mapping",
                    "mappings": equipment_mappings,
                },
                {
                    "category": "task_payload_suggestions",
                    "task_suggestions": task_payloads,
                    "current_task_suggestions": current_suggestions,
                },
                {
                    "category": "payload_catalog",
                    "catalog": payload_catalog,
                    "matched_payloads": matched_payloads,
                },
            ]
            if selected_equipment_mapping:
                supported_set = set(selected_equipment_mapping.get("supported_payloads", []))
                response["selected_equipment_info"] = {
                    "equipment_type": selected_equipment_mapping["equipment_type"],
                    "onboard_payloads": selected_equipment_mapping["onboard_payloads"],
                    "supported_payloads": selected_equipment_mapping["supported_payloads"],
                    "all_payloads": selected_equipment_mapping["all_payloads"],
                    "recommended_extension_payloads": [
                        p for p in current_suggestions.get("common", [])
                        if p in supported_set
                    ],
                    "guidance": (
                        "用户当前任务已选定该设备。在给出‘配置建议’或‘推荐携带工具’时，必须且只能推荐该选中设备允许的扩展加装载荷（supported_payloads）。"
                        "严禁把出厂自带的固定设施（onboard_payloads，如高清水下摄像机、LED水下照明灯、前视声呐、USBL、DVL、INS等）作为‘推荐加装工具’推荐给用户！"
                        "严禁展开发散列举其他未选择机器人的载荷与分类选型条件。"
                    ),
                }
            response["found"] = bool(tool_set or task_payloads or payload_catalog)
            if task_type_key:
                response["used_task_type_key"] = task_type_key
            return response

        if query_type == "DEVICE_CAPABILITY":
            return self._execute_device_capability_query(
                retrieval_message,
                context,
                response,
            )

        if query_type == "ENVIRONMENT_QUERY":
            return self._execute_environment_query(
                retrieval_message,
                context,
                response,
            )

        if query_type == "KNOWLEDGE_QA":
            response["results"] = [
                {
                    "category": "oil_fields",
                    "oil_fields": self.environment.get("oil_fields", []),
                },
                {
                    "category": "forbidden_areas",
                    "forbidden_areas": self.environment.get("forbidden_areas", []),
                },
                {
                    "category": "dvl_bottom_lock_failure_areas",
                    "dvl_areas": self.environment.get("dvl_bottom_lock_failure_areas", []),
                },
                {
                    "category": "task_templates",
                    "templates": self.task_schemas.get("task_templates", {}),
                },
                {
                    "category": "constraints_rules",
                    "constraints": [
                        {
                            "id": c.get("id"),
                            "name": c.get("name"),
                            "severity": c.get("severity"),
                            "message": c.get("violation_message", "").strip(),
                            "applies_to": c.get("applies_to", []),
                        }
                        for c in self.constraints
                    ],
                },
                {
                    "category": "workflow_and_persistence_rules",
                    "rules": {
                        "soft_warning_ignore": "当系统产生软约束警告 (blocked_soft) 时，用户可通过明确回复'确认'、'忽略'、'无视'等意图将警告加入白名单并继续流程；软警告忽略不会影响任务数据一致性。",
                        "hard_constraint_blocking": "当任务触发硬约束 (blocked_hard) 时，系统必须阻断发布。硬约束无法被'确认'或'忽略'绕过，必须修改参数直至合规后方可继续。",
                        "task_persistence_location": "任务发布后首先写入 staging 暂存文件，完成跨进程锁校验与原子重命名后持久化至系统的 final 任务目录中，绝对路径按任务类型与日流水号确定。",
                        "dialogue_phases": "系统维护 collecting（收集）、blocked_hard（硬阻断）、blocked_soft（软警告）、confirming（待确认）、done（已发布）、rejected（已拒绝）显式状态机。"
                    },
                },
                {
                    "category": "payload_catalog",
                    "catalog": self.assets.get("payload_catalog", {}),
                },
                {
                    "category": "robot_classes_summary",
                    "classes": self.kb.get_robot_classes(),
                    "families": self.robot_fleet.get("robot_families", {}),
                },
                {
                    "category": "cable_types",
                    "cable_types": self.assets.get("cable_types", []),
                },
                {
                    "category": "vessels",
                    "vessels": self.assets.get("vessels", []),
                },
            ]
            response["found"] = True
            return response

        response["reason"] = "unsupported_query_type"
        return response

    def _execute_environment_query(
        self,
        user_message: str,
        context: dict,
        response: dict,
    ) -> dict:
        """执行作业海域、油气田、禁入区与DVL风险区的结构化知识检索。"""
        matched_alias, targets = self._find_environment_entity_targets(user_message)
        if not targets:
            context_of = context.get("oilfield_name")
            if context_of and any(pronoun in user_message for pronoun in ("它", "这个", "那个", "该油田", "该海域", "此海域", "此区域")):
                matched_alias, targets = self._find_environment_entity_targets(str(context_of))

        oil_fields = self.environment.get("oil_fields", [])
        forbidden_areas = self.environment.get("forbidden_areas", [])
        dvl_areas = self.environment.get("dvl_bottom_lock_failure_areas", [])

        results = []
        if targets:
            for target in targets:
                if target.startswith("oil_field:"):
                    field_id = target.split(":", 1)[1]
                    for of in oil_fields:
                        if of.get("id") == field_id:
                            results.append({
                                "category": "oil_field_details",
                                "oil_field": of,
                            })
                elif target.startswith("forbidden_area:"):
                    area_id = target.split(":", 1)[1]
                    for fa in forbidden_areas:
                        if fa.get("id") == area_id:
                            results.append({
                                "category": "forbidden_area_details",
                                "forbidden_area": fa,
                            })
                elif target.startswith("dvl_area:"):
                    dvl_id = target.split(":", 1)[1]
                    for da in dvl_areas:
                        if da.get("id") == dvl_id:
                            results.append({
                                "category": "dvl_area_details",
                                "dvl_area": da,
                            })

        if not results:
            results = [
                {
                    "category": "oil_fields_summary",
                    "oil_fields": oil_fields,
                },
                {
                    "category": "forbidden_areas_summary",
                    "forbidden_areas": forbidden_areas,
                },
                {
                    "category": "dvl_bottom_lock_failure_areas_summary",
                    "dvl_areas": dvl_areas,
                },
            ]

        response["results"] = results
        response["found"] = True
        if matched_alias:
            response["matched_alias"] = matched_alias
            response["matched_targets"] = targets
        return response

    def _execute_device_capability_query(
        self,
        user_message: str,
        context: dict,
        response: dict,
    ) -> dict:
        task_type_key = context.get("task_type_key")
        depth_condition = self._parse_depth_condition(user_message)
        response["depth_condition"] = depth_condition

        matched_alias, entity_targets = self._find_query_entity_targets(user_message)
        if not entity_targets:
            context_selector = context.get("equipment_type")
            if context_selector:
                entity_targets = self._resolve_context_entity_targets(
                    str(context_selector),
                    task_type_key,
                )
                if entity_targets:
                    matched_alias = str(context_selector)

        list_keywords = ("哪些", "列表", "所有", "有哪些", "推荐", "选择", "可用", "什么型号", "查询", "查看", "列出", "当前支持", "支持的")
        is_list_query = any(keyword in user_message for keyword in list_keywords)

        generic_terms = {"设备", "机器人", "潜水器", "rov", "auv", "hov", "单机", "型号", "工具", "支持", "具备", "配备", "搭载", "当前", "目前", "全部", "所有", "系统", "清单", "列表"}
        query_strip_words = (
            "查询", "查看", "列出", "检索", "显示", "获取", "了解", "我要", "我想", "帮我",
            "可以", "能否", "请", "列表", "清单", "可用", "所有", "有哪些", "什么", "哪些",
            "推荐", "选择", "的", "一下", "看看", "知道", "信息", "能力", "状态", "目前", "现在",
            "支持", "具备", "配备", "搭载", "当前", "全部", "系统"
        )
        cleaned_msg = _norm(user_message)
        for w in query_strip_words:
            cleaned_msg = cleaned_msg.replace(_norm(w), "")

        is_broad_device_list_query = (not cleaned_msg) or all(
            token in generic_terms for token in re.findall(r"[a-zA-Z0-9]+|[\u4e00-\u9fa5]+", cleaned_msg)
        )

        is_system_capability_query = (
            context.get("subject_type") == "system_rule"
            or any(kw in user_message for kw in ("你具备", "你能干", "你会", "你的能力", "你能做", "干什么", "会什么", "自我介绍", "系统功能", "系统能力"))
        )

        if not entity_targets and is_system_capability_query:
            response["found"] = True
            response["reason"] = "system_identity"
            response["query_mode"] = "system_identity"
            return response

        if entity_targets and len(entity_targets) > 1:
            family_ids = set()
            class_ids = set()
            for target in entity_targets:
                if target.startswith("family:"):
                    family_ids.add(target.split(":", 1)[1])
                elif target.startswith("variant:"):
                    var_id = target.split(":", 1)[1]
                    rov = self.kb.get_rov(var_id)
                    if rov and rov.get("family_id"):
                        family_ids.add(rov["family_id"])
                elif target.startswith("class:"):
                    class_ids.add(target.split(":", 1)[1])

            if len(family_ids) == 1 or len(class_ids) == 1:
                robots = []
                for et in entity_targets:
                    _, r_list = self._robots_for_entity_target(et, task_type_key)
                    for r in r_list:
                        if r not in robots:
                            robots.append(r)
                response["matched_alias"] = matched_alias
                response["matched_entity"] = entity_targets[0]
                query_mode = "device_check"
            else:
                response["reason"] = "ambiguous_device_alias"
                response["matched_alias"] = matched_alias
                response["candidate_entities"] = entity_targets
                response["query_mode"] = "device_check"
                return response
        elif entity_targets:
            entity_target = entity_targets[0]
            entity_kind, robots = self._robots_for_entity_target(
                entity_target,
                task_type_key,
            )
            response["matched_alias"] = matched_alias
            response["matched_entity"] = entity_target
            is_check_query = any(kw in user_message for kw in ("能", "性能", "能否", "可以", "适不适合", "合适", "吗", "能力", "参数"))
            query_mode = (
                "device_list"
                if (entity_kind == "class" and not is_check_query) or (entity_kind == "family" and is_list_query and not is_check_query)
                else "device_check"
            )
        elif is_broad_device_list_query or is_list_query:
            if not is_broad_device_list_query and cleaned_msg and not depth_condition.get("has_depth_expression"):
                response["reason"] = "device_not_resolved"
                response["query_mode"] = "device_check"
                return response
            robots = (
                self.kb.get_task_allowed_robot_variants(task_type_key)
                if task_type_key
                else self.kb.get_all_rovs()
            )
            query_mode = "device_list"
        else:
            response["reason"] = "device_not_resolved"
            response["query_mode"] = "device_check"
            return response

        response["query_mode"] = query_mode
        if depth_condition["has_depth_expression"] and depth_condition["parse_status"] == "invalid":
            response["reason"] = "invalid_depth_expression"
            return response

        results: list[dict] = []
        for robot in robots:
            item = dict(robot)
            matches_depth = self._matches_depth_condition(
                robot.get("max_depth_m"),
                depth_condition,
            )
            item["matches_depth_condition"] = matches_depth
            if query_mode == "device_check" or matches_depth:
                results.append(item)

        response["results"] = results
        response["found"] = bool(results)
        if not results:
            response["reason"] = "no_matching_device"
        return response

    def _find_query_entity_targets(self, user_message: str) -> tuple[str | None, list[str]]:
        message_norm = _norm(user_message)
        if not message_norm:
            return None, []
        matches = [
            (alias, targets)
            for alias, targets in self.get_device_alias_index().items()
            if _norm(alias) and _norm(alias) in message_norm
        ]
        if not matches:
            class_matches = []
            for class_id, robot_class in self.kb.get_robot_classes().items():
                label = robot_class.get("full_name") or class_id
                if _norm(label) and _norm(label) in message_norm:
                    class_matches.append((label, [f"class:{class_id}"]))
            if not class_matches:
                return None, []
            class_matches.sort(key=lambda item: len(_norm(item[0])), reverse=True)
            return class_matches[0]
        matches.sort(key=lambda item: len(_norm(item[0])), reverse=True)
        return matches[0]

    def _resolve_context_entity_targets(
        self,
        selector: str,
        task_type_key: str | None,
    ) -> list[str]:
        unit = self.kb.resolve_robot_unit(selector, task_type_key)
        if unit:
            return [f"unit:{unit.get('unit_id')}"]
        variant = self.kb.get_rov_for_task(selector, task_type_key)
        if variant:
            return [f"variant:{variant.get('variant_id')}"]
        family_id = self.kb.resolve_robot_family_id(selector, task_type_key)
        if family_id:
            return [f"family:{family_id}"]
        return []

    def _robots_for_entity_target(
        self,
        entity_target: str,
        task_type_key: str | None,
    ) -> tuple[str | None, list[dict]]:
        if ":" not in entity_target:
            return None, []
        entity_kind, entity_id = entity_target.split(":", 1)
        allowed_robots = (
            self.kb.get_task_allowed_robot_variants(task_type_key)
            if task_type_key
            else self.kb.get_all_rovs()
        )

        if entity_kind == "class":
            return entity_kind, [
                robot
                for robot in allowed_robots
                if robot.get("robot_class") == entity_id
            ]
        if entity_kind == "family":
            return entity_kind, [
                robot
                for robot in allowed_robots
                if robot.get("family_id") == entity_id
            ]
        if entity_kind == "variant":
            return entity_kind, [
                robot
                for robot in allowed_robots
                if robot.get("variant_id") == entity_id
            ]
        if entity_kind == "unit":
            units = [
                unit
                for unit in self.robot_fleet.get("fleet_units", [])
                if unit.get("unit_id") == entity_id
            ]
            if len(units) != 1:
                return entity_kind, []
            unit = units[0]
            robots = [
                robot
                for robot in allowed_robots
                if robot.get("variant_id") == unit.get("variant_id")
            ]
            if len(robots) != 1:
                return entity_kind, []
            robot = dict(robots[0])
            robot["selected_unit"] = dict(unit)
            return entity_kind, [robot]
        return None, []

    @staticmethod
    def _parse_depth_condition(user_message: str) -> dict:
        condition = {
            "operator": None,
            "depth_m": None,
            "has_depth_expression": False,
            "parse_status": "absent",
        }
        patterns = (
            ("eq", r"(\d+)\s*米级"),
            ("lte", r"(?:最大(?:下潜|作业)?深度(?:为|是)?|下潜极限(?:为|是)?)\s*(\d+)\s*(?:米|m)?"),
            ("lte", r"(?:不超过|至多|最大不超过|不大于|最多)\s*(\d+)\s*(?:米|m)?"),
            ("lt", r"(?:低于|小于|不到)\s*(\d+)\s*(?:米|m)?"),
            ("gte", r"(?:不少于|不低于|至少)\s*(\d+)\s*(?:米|m)?"),
            ("gt", r"(?<!不)(?:超过|大于)\s*(\d+)\s*(?:米|m)?"),
            ("gte", r"(?:支持在?|能够下潜至|能够下潜到|能够在?|能下潜到?|可在?|在|水深(?:为|是)?)\s*(\d+)\s*(?:米|m)?"),
            ("eq", r"(\d+)\s*(?:米|m)\s*(?:水深|深)"),
            ("eq", r"(?:水深|深)\s*(\d+)\s*(?:米|m)"),
        )
        for operator, pattern in patterns:
            match = re.search(pattern, user_message, re.IGNORECASE)
            if match:
                condition.update({
                    "operator": operator,
                    "depth_m": int(match.group(1)),
                    "has_depth_expression": True,
                    "parse_status": "valid",
                })
                break
        return condition

    @staticmethod
    def _matches_depth_condition(max_depth: Any, condition: dict) -> bool:
        if condition.get("parse_status") != "valid":
            return True
        if isinstance(max_depth, bool) or not isinstance(max_depth, (int, float)):
            return False
        target = condition.get("depth_m")
        operator = condition.get("operator")
        if operator == "eq":
            return max_depth == target
        if operator == "gte":
            return max_depth >= target
        if operator == "gt":
            return max_depth > target
        if operator == "lte":
            return max_depth <= target
        if operator == "lt":
            return max_depth < target
        return False
