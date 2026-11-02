"""
src/knowledge/query/typed_query_engine.py — 强类型只读知识查询、载荷匹配与设备能力校验引擎
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from ..selection.models import _norm

if TYPE_CHECKING:
    from ...knowledge_retriever import KnowledgeBase
    from .alias_indexer import DeviceAliasIndexer
    from .environment_retriever import EnvironmentRetriever


class TypedQueryEngine:
    """强类型知识查询、载荷建议与设备能力校验引擎。"""

    def __init__(
        self,
        kb: "KnowledgeBase",
        alias_indexer: "DeviceAliasIndexer",
        environment_retriever: "EnvironmentRetriever",
    ):
        self.kb = kb
        self.alias_indexer = alias_indexer
        self.environment_retriever = environment_retriever

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

    @staticmethod
    def parse_depth_condition(user_message: str) -> dict:
        """从用户文本中解析水深约束条件。"""
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
    def matches_depth_condition(max_depth: Any, condition: dict) -> bool:
        """判断最大水深是否满足给定的深度条件。"""
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

    @staticmethod
    def match_payload_catalog(
        payload_catalog: dict,
        user_message: str,
    ) -> list[dict]:
        """从载荷目录中根据名称与别名匹配候选载荷。"""
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

    def payload_configuration_evidence(self) -> dict:
        """Keep selection semantics with the actual onboard/optional mappings.

        A tool catalogue describes capabilities, not an installation requirement.
        Pin these compact facts outside the trimmable general catalogue so that
        a large query cannot lose the distinction while retaining rule prose.
        """
        return {
            "selection_semantics": {
                "validation_scope": "本证据只说明任务能力与载荷配置，不是完整任务校验结果，不证明可以发布或执行。发布还需完整合法参数、软硬约束处理及明确确认；实际下发还需执行协议支持。",
                "task_capability": "任务能力取自任务模板 required_capabilities 和机器人族 capabilities，不能由某件选配工具是否出现推断。",
                "onboard_payloads": "已随机器人自带的机载设备，无需另行加装，不应重复列为必需选配工具。",
                "supported_payloads": "机器人支持的可选扩展载荷，不表示必须携带。",
                "task_payload_options": "任务载荷候选范围，不是必选清单；是否允许另带某项还须按所选机器人筛选。",
                "required_payload": "携带工具字段需要填写不等于某件具体工具必选。只有模板或有效约束明确指定该工具必选时，才能声称不另带它会阻断发布。",
            },
            "task_required_capabilities": {
                key: {
                    "name": template.get("display_name", key),
                    "required_capabilities": template.get("required_capabilities", []),
                }
                for key, template in self.task_schemas.get("task_templates", {}).items()
            },
            "robot_configurations": [
                {
                    "equipment_type": robot.get("full_name"),
                    "capabilities": robot.get("capabilities", []),
                    "onboard_payloads": robot.get("onboard_payloads", []),
                    "supported_payloads": robot.get("supported_payloads", []),
                }
                for robot in self.kb.get_all_rovs()
            ],
        }

    def resolve_typed_read_query(
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
            _, entity_targets = self.alias_indexer.find_query_entity_targets(selector)
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
            _, entity_targets = self.environment_retriever.find_environment_entity_targets(selector)
            if entity_targets:
                if selector == user_message:
                    return "ENVIRONMENT_QUERY", user_message
                return "ENVIRONMENT_QUERY", f"{selector} {user_message}"
            if any(k in _norm(selector) for k in env_generic_keywords):
                return "ENVIRONMENT_QUERY", user_message

        return query_type, user_message

    def execute_device_capability_query(
        self,
        user_message: str,
        context: dict,
        response: dict,
    ) -> dict:
        """执行机器人型号规格、水深能力与下潜限制匹配查询。"""
        task_type_key = context.get("task_type_key")
        depth_condition = self.parse_depth_condition(user_message)
        response["depth_condition"] = depth_condition

        matched_alias, entity_targets = self.alias_indexer.find_query_entity_targets(user_message)
        if not entity_targets:
            context_selector = context.get("equipment_type")
            if context_selector:
                entity_targets = self.alias_indexer.resolve_context_entity_targets(
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
                    _, r_list = self.alias_indexer.robots_for_entity_target(et, task_type_key)
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
            entity_kind, robots = self.alias_indexer.robots_for_entity_target(
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
            matches_depth = self.matches_depth_condition(
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

    def execute_typed_query(
        self,
        query_type: str,
        user_message: str,
        context: dict | None = None,
    ) -> dict:
        """执行强类型只读知识查询，并返回稳定的结构化证据。"""
        context = context if isinstance(context, dict) else {}
        requested_query_type = query_type
        query_type, retrieval_message = self.resolve_typed_read_query(
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

        if query_type == "TOOL_QUERY" or (
            query_type == "KNOWLEDGE_QA"
            and re.search(r"载荷|携带|选配|加装|自带|机载|工具|机械臂|声呐", user_message)
        ):
            response["payload_configuration_evidence"] = self.payload_configuration_evidence()

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

            matched_payloads = self.match_payload_catalog(
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
                        "只有当前设备的 onboard_payloads 清单明确列出的项目才属于机载设备，不得将其作为‘推荐加装工具’；supported_payloads 中的项目属于选配载荷，不能因功能相近而归入机载设备。"
                        "严禁展开发散列举其他未选择机器人的载荷与分类选型条件。"
                    ),
                }
            response["found"] = bool(tool_set or task_payloads or payload_catalog)
            if task_type_key:
                response["used_task_type_key"] = task_type_key
            return response

        if query_type == "DEVICE_CAPABILITY":
            return self.execute_device_capability_query(
                retrieval_message,
                context,
                response,
            )

        if query_type == "ENVIRONMENT_QUERY":
            return self.environment_retriever.execute_environment_query(
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
                            "check_type": c.get("check_type"),
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
            if "payload_configuration_evidence" in response and re.search(
                r"(?:必须|必选|一定要|需要|需不需要|是否).{0,8}(?:携带|另带|带上|加装|选配)"
                r"|(?:工具|载荷|机械臂|声呐).{0,8}(?:必须|必选|一定要)",
                user_message,
            ):
                response["query_mode"] = "payload_requirements"
                templates = self.task_schemas.get("task_templates", {})
                response["results"] = [
                    {
                        "category": "task_templates",
                        "templates": {
                            key: {
                                "display_name": template.get("display_name", key),
                                "required_capabilities": template.get("required_capabilities", []),
                                "payload_fields_by_mode": {
                                    mode: [field for field in fields if field.get("key") == "payload"]
                                    for mode, fields in template.get("output_schema", {}).items()
                                    if isinstance(fields, list)
                                },
                            }
                            for key, template in templates.items()
                        },
                    },
                    next(item for item in response["results"] if item["category"] == "constraints_rules"),
                    {
                        "category": "payload_catalog",
                        "matched_payloads": self.match_payload_catalog(
                            self.assets.get("payload_catalog", {}), user_message,
                        ),
                    },
                ]
            response["found"] = True
            return response

        response["reason"] = "unsupported_query_type"
        return response
