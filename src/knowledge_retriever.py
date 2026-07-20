"""
knowledge_retriever.py — 知识库加载与按需检索
不使用向量数据库，基于任务状态进行规则化知识片段选取。
知识总量在10000字以内，精准注入比全量注入更高效。
"""

import yaml
from pathlib import Path
from typing import Any
from .environment_info import EnvironmentInfo
from .state_info import RobotStateInfo

CONFIG_DIR = Path(__file__).parent.parent / "config"


def _load(filename: str) -> dict | list:
    with open(CONFIG_DIR / filename, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _norm(value: object) -> str:
    return str(value or "").lower().replace(" ", "")


class KnowledgeBase:
    def __init__(self):
        self.load_all()  # 改成调用方法

    # ✅ 新增：热重载配置
    def load_all(self):
        self.task_schemas: dict = _load("task_schemas.yaml")
        self.robot_fleet: dict = _load("robot_fleet.yaml")
        self.assets: dict = _load("assets.yaml")
        self.constraints: list = _load("constraints.yaml")["constraints"]
        self.environment: dict = _load("environment.yaml")

        self.env_info = EnvironmentInfo()
        self.state_info = RobotStateInfo()
        self._robot_variants_cache: list[dict] | None = None
        self.ROV2type = self.get_ROV2type()

    # ──────────────────────────────────────────────────────────────────────────
    # 新机器人索引：robot_classes -> robot_families -> model_variants -> fleet_units
    # ──────────────────────────────────────────────────────────────────────────

    def get_robot_classes(self) -> dict:
        return self.robot_fleet.get("robot_classes", {})

    def get_robot_class_labels(self) -> list[str]:
        return [v.get("full_name", k) for k, v in self.get_robot_classes().items()]

    def get_task_allowed_robot_classes(self, task_type_key: str | None) -> list[str]:
        if not task_type_key:
            return list(self.get_robot_classes().keys())
        template = self.task_schemas.get("task_templates", {}).get(task_type_key, {})
        return template.get("allowed_robot_classes", [])

    def get_task_required_capabilities(self, task_type_key: str | None) -> list[str]:
        if not task_type_key:
            return []
        template = self.task_schemas.get("task_templates", {}).get(task_type_key, {})
        return template.get("required_capabilities", [])

    def robot_matches_task(self, robot: dict | None, task_type_key: str | None) -> bool:
        if not robot or not task_type_key:
            return True
        allowed_classes = set(self.get_task_allowed_robot_classes(task_type_key))
        required_caps = set(self.get_task_required_capabilities(task_type_key))
        if allowed_classes and robot.get("robot_class") not in allowed_classes:
            return False
        return required_caps.issubset(set(robot.get("capabilities", [])))

    def get_robot_families_for_classes(
        self,
        robot_class_keys: list[str],
        required_capabilities: list[str] | None = None,
    ) -> list[tuple[str, dict]]:
        required = set(required_capabilities or [])
        allowed_classes = set(robot_class_keys or [])
        result: list[tuple[str, dict]] = []
        for family_id, family in self.robot_fleet.get("robot_families", {}).items():
            if allowed_classes and family.get("robot_class") not in allowed_classes:
                continue
            if not required.issubset(set(family.get("capabilities", []))):
                continue
            result.append((family_id, family))
        return result

    def get_robot_families_for_task(self, task_type_key: str | None) -> list[tuple[str, dict]]:
        return self.get_robot_families_for_classes(
            self.get_task_allowed_robot_classes(task_type_key),
            self.get_task_required_capabilities(task_type_key),
        )

    def get_model_variants_for_family(self, family_id: str) -> list[tuple[str, dict]]:
        result: list[tuple[str, dict]] = []
        for variant_id, variant in self.robot_fleet.get("model_variants", {}).items():
            if variant.get("family_id") == family_id:
                result.append((variant_id, variant))
        return result

    def get_model_variants_for_task(self, task_type_key: str | None) -> list[tuple[str, dict]]:
        result: list[tuple[str, dict]] = []
        for family_id, _family in self.get_robot_families_for_task(task_type_key):
            result.extend(self.get_model_variants_for_family(family_id))
        return result

    def get_fleet_units_for_variant(self, variant_id: str) -> list[dict]:
        return [
            unit
            for unit in self.robot_fleet.get("fleet_units", [])
            if unit.get("variant_id") == variant_id
        ]

    def _build_robot_variant(
        self,
        robot_classes: dict,
        family_id: str,
        family: dict,
        variant_id: str,
        variant: dict,
    ) -> dict:
        robot_class = family.get("robot_class")
        robot_class_name = robot_classes.get(robot_class, {}).get("full_name", robot_class)
        hard_params = variant.get("hard_params", {}) or {}
        units = self.get_fleet_units_for_variant(variant_id)

        # aliases is intentionally only the natural-language mapping list
        # supplied for the robot family. It is shown to the LLM so user terms
        # can be classified to this variant's full_name.
        aliases: list[str] = list(family.get("aliases", []))

        # Fleet-unit aliases identify physical units, not the model variant
        # full_name. Keep them on fleet_units; use private lookup targets only
        # for backend retrieval.
        lookup_targets: list[str] = [
            variant.get("full_name", ""),
            family.get("full_name", ""),
            variant_id,
            family_id,
        ]
        lookup_targets.extend(aliases)
        for unit in units:
            lookup_targets.extend([
                unit.get("unit_id", ""),
                unit.get("display_name", ""),
                *unit.get("aliases", []),
            ])

        deduped_aliases: list[str] = []
        seen_aliases = set()
        for alias in aliases:
            if alias and alias not in seen_aliases:
                deduped_aliases.append(alias)
                seen_aliases.add(alias)

        deduped_lookup_targets: list[str] = []
        seen_targets = set()
        for target in lookup_targets:
            if target and target not in seen_targets:
                deduped_lookup_targets.append(target)
                seen_targets.add(target)

        robot = {
            "model": variant_id,
            "variant_id": variant_id,
            "family_id": family_id,
            "full_name": variant.get("full_name"),
            "family_full_name": family.get("full_name"),
            "robot_class": robot_class,
            "robot_class_name": robot_class_name,
            # Backward-compatible keys used by existing prompts/status code.
            "category": robot_class,
            "category_name": robot_class_name,
            "capabilities": family.get("capabilities", []),
            "aliases": deduped_aliases,
            "_lookup_targets": deduped_lookup_targets,
            "brief": family.get("brief", ""),
            "hard_params": hard_params,
            "fleet_units": units,
            "unit_ids": [u.get("unit_id") for u in units if u.get("unit_id")],
        }
        robot.update(hard_params)
        robot.setdefault("supported_payloads", hard_params.get("supported_payloads", []))
        return robot

    def _build_robot_variant_index(self) -> list[dict]:
        robot_classes = self.get_robot_classes()
        families = self.robot_fleet.get("robot_families", {})
        robots: list[dict] = []

        # Layered traversal: class -> family -> variant -> fleet unit.
        # Do not discover variants by jumping directly into the full variant set
        # for task matching; relationship fields are the source of truth.
        for robot_class_key in robot_classes:
            family_items = self.get_robot_families_for_classes([robot_class_key])
            for family_id, family in family_items:
                for variant_id, variant in self.get_model_variants_for_family(family_id):
                    robots.append(
                        self._build_robot_variant(
                            robot_classes,
                            family_id,
                            family,
                            variant_id,
                            variant,
                        )
                    )
        return robots

    def get_all_rovs(self) -> list[dict]:
        if self._robot_variants_cache is None:
            self._robot_variants_cache = self._build_robot_variant_index()
        return list(self._robot_variants_cache)

    def get_task_allowed_robot_variants(self, task_type_key: str | None) -> list[dict]:
        robot_classes = self.get_robot_classes()
        robots: list[dict] = []
        for family_id, family in self.get_robot_families_for_task(task_type_key):
            for variant_id, variant in self.get_model_variants_for_family(family_id):
                robots.append(
                    self._build_robot_variant(
                        robot_classes,
                        family_id,
                        family,
                        variant_id,
                        variant,
                    )
                )
        return robots

    def get_ROV2type(self) -> dict:
        return {r["full_name"]: r.get("robot_class_name") for r in self.get_all_rovs()}

    # ──────────────────────────────────────────────────────────────────────────
    # 按任务状态选取相关知识片段
    # ──────────────────────────────────────────────────────────────────────────

    def get_supported_task(self) -> list:
        res = self.task_schemas.get("task_templates", {})
        return [res[t]["task_type_values"] for t in res]

    def get_context_for_state(self, task_state: dict) -> str:
        task_type = task_state.get("task_type_key")
        equipment = task_state.get("equipment_name")
        equipment_type = task_state.get("equipment_type")
        coords = task_state.get("start_point") or task_state.get("oilfield_coordinates")
        depth = task_state.get("water_depth")

        sections = [self._robot_category_overview()]

        # 2. 与当前任务类型相关的 ROV 约束
        if task_type:
            sections.append(self._task_rov_constraint(task_type))

        equipment_selector = (
            task_state.get("equipment_unit_id")
            or task_state.get("equipment_name")
            or equipment_type
        )
        if equipment_selector:
            rov_info = self._get_rov_info(equipment_selector)
            if rov_info:
                sections.append(f"【当前选定设备详情】\n{rov_info}")
                state_dict = self.get_robot_state_dict(equipment_selector)
                if state_dict and isinstance(state_dict, dict):
                    state_lines = []
                    label_map = {
                        "current_velocity": "当前流速",
                        "turbidity": "浑浊度",
                        "obstacle_density": "障碍物密度",
                        "mothership_support": "母船支援",
                        "update_timestamp": "更新时间",
                        "confidence": "置信度",
                        "overall_status": "总体状态",
                        "survival_status": "生存状态",
                        "thruster_status": "推进器状态",
                        "depth_keeping_status": "定深能力",
                        "sonar_status": "声呐状态",
                        "vision_status": "视觉系统状态",
                        "arm_status": "机械臂状态",
                        "end_effector_status": "末端执行器状态",
                        "acoustic_comms_status": "水声无线通信状态",
                        "tether_connection_status": "脐带缆连接状态"
                    }
                    for k, v in state_dict.items():
                        if v is not None and not k.startswith("_"):
                            label = label_map.get(k, k)
                            if isinstance(v, float):
                                state_lines.append(f"  - {label} ({k}): {v:.2f}")
                            else:
                                state_lines.append(f"  - {label} ({k}): {v}")
                    if state_lines:
                        sections.append("【当前设备实时状态】\n" + "\n".join(state_lines))
        elif equipment_type:
                sections.append(self._rovs_by_category(equipment_type, task_type))
        elif task_type:
            sections.append(self._rovs_for_task(task_type))

        # 4. 管缆类型（管缆巡检任务）
        if task_type == "pipeline_inspection":
            sections.append(self._cable_types_overview())
            sections.append(self._payload_suggestions("pipeline_inspection"))
        elif task_type == "pipeline_burial":
            sections.append(self._cable_types_overview())
            sections.append(self._payload_suggestions("pipeline_burial"))
        elif task_type == "tree_valve_operation":
            sections.append(self._payload_suggestions("tree_valve_operation"))

        # 6. 支持船信息
        sections.append(self._vessels_overview())

        # 7. 海域环境（有坐标时匹配）
        if coords:
            env_info = self._get_environment(coords)
            if env_info:
                sections.append(f"【作业区域环境状态】\n{env_info}")

        # 8. 适用约束规则摘要
        sections.append(self._relevant_constraints(task_type, equipment_type, depth))

        return "\n\n".join(s for s in sections if s.strip())

    # ──────────────────────────────────────────────────────────────────────────
    # 内部片段构建方法
    # ──────────────────────────────────────────────────────────────────────────

    def _robot_category_overview(self) -> str:
        lines = ["【机器人四大类说明】"]
        for key, value in self.get_robot_classes().items():
            lines.append(f"- {value.get('full_name', key)}（{key}）")
        return "\n".join(lines)

    def _task_rov_constraint(self, task_type: str) -> str:
        schema = self.task_schemas["task_templates"].get(task_type, {})
        class_names = [
            self.get_robot_classes().get(key, {}).get("full_name", key)
            for key in schema.get("allowed_robot_classes", [])
        ]
        caps = "、".join(schema.get("required_capabilities", []))
        if not class_names and not caps:
            return ""
        return (
            f"【任务设备约束】{schema.get('display_name', task_type)} 任务只允许使用"
            f"{'、'.join(class_names)}，且设备能力必须覆盖：{caps or '无特殊能力'}。"
        )

    def _rovs_by_category(self, category_value: str, task_type: str | None = None) -> str:
        class_key = self._resolve_robot_class_key(category_value)
        cat_label = self.get_robot_classes().get(class_key, {}).get("full_name", category_value)
        rovs = [
            r for r in self.get_all_rovs()
            if r.get("robot_class") == class_key and self.robot_matches_task(r, task_type)
        ]
        if not rovs:
            return f"【{cat_label}】当前无可用设备。"
        lines = [f"【{cat_label}设备列表】"]
        for r in rovs:
            lines.append(f"- {r['full_name']} | 最大水深:{r.get('max_depth_m')}m\n  {r.get('brief', '')}")
        return "\n".join(lines)

    def _rovs_for_task(self, task_type: str) -> str:
        rovs = self.get_task_allowed_robot_variants(task_type)
        if not rovs:
            return "【任务可用设备】当前无符合任务条件的设备。"
        lines = ["【任务可用设备】"]
        for r in rovs:
            lines.append(
                f"- {r['full_name']} | 类型:{r.get('robot_class_name')} | "
                f"能力:{'、'.join(r.get('capabilities', []))} | 最大水深:{r.get('max_depth_m')}m"
            )
        return "\n".join(lines)

    def _get_rov_info(self, model_or_alias: str) -> str | None:
        rov = self._find_rov(model_or_alias)
        if not rov:
            return None
        payloads = "、".join(rov.get("supported_payloads", []))
        return (
            f"{rov['full_name']}\n"
            f"类型: {rov.get('robot_class_name')} | 能力: {'、'.join(rov.get('capabilities', []))} | "
            f"最大水深: {rov.get('max_depth_m')}m\n"
            f"可搭载载荷: {payloads}\n"
            f"简介: {rov.get('brief', '')}"
        )

    def _find_rov(self, name: str) -> dict | None:
        needle = _norm(name)
        if not needle:
            return None

        candidates: list[tuple[dict, list[str]]] = []
        for robot in self.get_all_rovs():
            targets = [target for target in robot.get("_lookup_targets", []) if target]
            candidates.append((robot, targets))

        # Prefer exact normalized matches so broad aliases like "001" do not
        # capture a specific identifier such as "WROV-250-001".
        for robot, targets in candidates:
            if any(needle == _norm(target) for target in targets):
                return robot

        for robot, targets in candidates:
            if any(needle in _norm(target) or _norm(target) in needle for target in targets):
                return robot
        return None

    def find_rov_by_description(self, description: str) -> list[dict]:
        return self.get_all_rovs()

    def _resolve_robot_class_key(self, value: str) -> str:
        value_norm = _norm(value)
        for key, cfg in self.get_robot_classes().items():
            if value_norm in {_norm(key), _norm(cfg.get("full_name"))}:
                return key
        return value

    def _cable_types_overview(self) -> str:
        types = self.assets["cable_types"]
        lines = ["【管缆类型（必须使用标准名称）】"]
        for t in types:
            aliases = "、".join(t["aliases"][:4])
            lines.append(f"- {t['label']}（别名：{aliases}）")
        return "\n".join(lines)

    def _payload_suggestions(self, task_type: str) -> str:
        pt = self.assets["payload_options"].get(task_type, {})
        common = "、".join(pt.get("common", []))
        desc = pt.get("description", "")
        label = self.task_schemas.get("task_templates", {}).get(task_type, {}).get("display_name", task_type)
        return f"【{label}常用携带工具建议】\n{common}\n备注: {desc}"

    def _vessels_overview(self) -> str:
        lines = ["【可用支持船只列表】"]
        for v in self.assets["vessels"]:
            status = "✓ 可用" if v["available"] else "✗ 不可用"
            lines.append(f"- {v['full_name']}（{v['type']}）[{status}] — {v['description']}")
        return "\n".join(lines)

    def _get_environment(self, coords: dict) -> str | None:
        if not isinstance(coords, dict):
            return None
        lat = coords.get("lat")
        lon = coords.get("lon")
        if lat is None or lon is None:
            return None
        for oil_field in self.environment["oil_fields"]:
            lat_ok = oil_field["lat_range"][0] <= lat <= oil_field["lat_range"][1]
            lon_ok = oil_field["lon_range"][0] <= lon <= oil_field["lon_range"][1]
            if lat_ok and lon_ok:
                return (
                    f"{oil_field['name']} \n"
                    f"海底底质: {oil_field['seabed_type']}\n"
                    f"备注: {oil_field['notes']}"
                )
        return None

    def _relevant_constraints(
        self, task_type: str | None, equipment_type: str | None, depth: float | None
    ) -> str:
        lines = ["【相关作业约束规则】"]
        for c in self.constraints:
            applies = c["applies_to"]
            if "all" not in applies and task_type and task_type not in applies:
                continue
            lines.append(f"[{c['id']}] {c['name']}: {c['violation_message'].strip()}")
        if len(lines) == 1:
            return ""
        return "\n".join(lines)

    # ──────────────────────────────────────────────────────────────────────────
    # 直接查询接口（供 validator 使用）
    # ──────────────────────────────────────────────────────────────────────────

    def get_rov(self, model_name: str) -> dict | None:
        return self._find_rov(model_name)

    def get_rov_for_task(self, model_name: str, task_type: str | None) -> dict | None:
        if not task_type:
            return self.get_rov(model_name)
        needle = _norm(model_name)
        if not needle:
            return None
        allowed_variants = self.get_task_allowed_robot_variants(task_type)
        for r in allowed_variants:
            targets = [target for target in r.get("_lookup_targets", []) if target]
            if any(needle == _norm(t) for t in targets):
                return r
        for r in allowed_variants:
            targets = [target for target in r.get("_lookup_targets", []) if target]
            if any(needle in _norm(t) or _norm(t) in needle for t in targets):
                return r
        return self.get_rov(model_name)

    def get_vessel(self, vessel_id: str) -> dict | None:
        vid_lower = vessel_id.lower().replace(" ", "")
        for v in self.assets["vessels"]:
            targets = [v["id"].lower()] + [a.lower().replace(" ", "") for a in v.get("aliases", [])]
            if any(vid_lower in t or t in vid_lower for t in targets):
                return v
        return None

    def get_task_schema(self, template_key: str) -> dict:
        """返回指定模板的完整配置（以 task_templates key 查找）"""
        return self.task_schemas["task_templates"].get(template_key, {})

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
        if not coords or not isinstance(coords, dict):
            return {
                "forbidden": None,
                "seabed_type": None,
                "obstacle_density": None,
                "acoustic_signal": None,
                "dvl_risk": None,
                "mothership_support": None
            }
        lat = coords.get("lat")
        lon = coords.get("lon")
        if lat is None or lon is None:
            return {
                "forbidden": None,
                "seabed_type": None,
                "obstacle_density": None,
                "acoustic_signal": None,
                "dvl_risk": None,
                "mothership_support": None
            }
        return self.env_info.get_all_info(lat, lon)

    def get_robot_state_dict(self, equipment_name: str) -> dict:
        empty_state = {
            "current_velocity": None,
            "turbidity": None,
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
        if not equipment_name or not isinstance(equipment_name, str):
            return empty_state

        lookup_keys: list[str] = [equipment_name]
        rov = self._find_rov(equipment_name)
        if rov:
            lookup_keys.extend(rov.get("_lookup_targets", []))
            for unit in rov.get("fleet_units", []):
                lookup_keys.extend([
                    unit.get("status_ref", ""),
                    unit.get("unit_id", ""),
                ])

        deduped_keys: list[str] = []
        seen = set()
        for key in lookup_keys:
            if key and key not in seen:
                deduped_keys.append(key)
                seen.add(key)

        for key in deduped_keys:
            state = self.state_info.get_all_info(key)
            if isinstance(state, dict):
                return state
        return empty_state

    def get_all_device_terms(self) -> set[str]:
        """从知识库配置动态生成只读设备检索词集合。"""
        terms: set[str] = set()
        all_rovs = self.get_all_rovs()
        for r in all_rovs:
            for field in ["full_name", "family_full_name", "robot_class_name", "model", "category_name"]:
                val = r.get(field)
                if val:
                    terms.add(str(val).strip())
            for alias in (r.get("aliases") or []):
                if alias and len(alias) >= 2:
                    terms.add(str(alias).strip())
            for unit in (r.get("fleet_units") or []):
                for u_field in ["unit_id", "display_name", "serial_no"]:
                    val = unit.get(u_field)
                    if val:
                        terms.add(str(val).strip())
                for u_alias in (unit.get("aliases") or []):
                    if u_alias and len(u_alias) >= 2:
                        terms.add(str(u_alias).strip())
        return terms

    # ──────────────────────────────────────────────────────────────────────────
    # ✅ 专属强类型只读查询接口
    # ──────────────────────────────────────────────────────────────────────────

    def execute_typed_query(self, query_type: str, user_message: str, context: dict | None = None) -> dict:
        """
        强类型知识库查询入口。
        返回包含 found, query_type, results, source, version, updated_at 的标准化 dict。
        """
        from datetime import datetime, timezone
        now_iso = datetime.now(timezone.utc).isoformat()
        base_resp = {
            "query_type": query_type,
            "results": [],
            "found": False,
            "source": "knowledge_base",
            "version": "kb_1.0",
            "updated_at": now_iso,
        }

        if query_type == "TOOL_QUERY":
            all_rovs = self.get_all_rovs()
            tool_set: set[str] = set()
            rov_tool_map: list[dict] = []
            for r in all_rovs:
                payloads = r.get("supported_payloads", [])
                for p in payloads:
                    tool_set.add(p)
                rov_tool_map.append({
                    "equipment_type": r.get("full_name"),
                    "robot_class": r.get("robot_class_name"),
                    "supported_payloads": payloads,
                })
            task_payloads = self.assets.get("payload_options", {})

            task_type_key = context.get("task_type_key") if context else None
            task_suggestions = []
            if task_type_key:
                task_suggestions = task_payloads.get(task_type_key, [])
                base_resp["used_task_type_key"] = task_type_key

            results = [
                {"category": "all_supported_tools", "tools": sorted(tool_set)},
                {"category": "equipment_payload_mapping", "mappings": rov_tool_map},
                {"category": "task_payload_suggestions", "task_suggestions": task_payloads, "current_task_suggestions": task_suggestions},
            ]
            base_resp["results"] = results
            base_resp["found"] = bool(tool_set or task_payloads)
            return base_resp

        if query_type == "DEVICE_CAPABILITY":
            all_rovs = self.get_all_rovs()
            query_norm = user_message.lower().replace(" ", "")

            import re

            # ── 1. 结构化深度条件解析 ──
            has_depth_num = bool(re.search(r"\d+\s*(?:米|m)?", user_message, re.IGNORECASE)) and any(
                kw in user_message for kw in ["米", "m", "深度", "下潜", "作业", "水深", "能力", "支持", "能够", "可在"]
            )

            depth_condition = {
                "operator": None,
                "depth_m": None,
                "has_depth_expression": has_depth_num,
                "parse_status": "absent" if not has_depth_num else "invalid",
            }

            m_exact = re.search(r"(\d+)\s*米级", user_message)
            m_max = re.search(r"(?:最大(?:下潜|作业)?深度(?:为|是)?|下潜极限(?:为|是)?)\s*(\d+)\s*(?:米|m)", user_message, re.IGNORECASE)
            m_lte = re.search(r"(?:不超过|至多|最大不超过|不大于|最多)\s*(\d+)\s*(?:米|m)", user_message, re.IGNORECASE)
            m_lt = re.search(r"(?:低于|小于|不到)\s*(\d+)\s*(?:米|m)", user_message, re.IGNORECASE)
            m_gte = re.search(r"(?:不少于|不低于|至少)\s*(\d+)\s*(?:米|m)", user_message, re.IGNORECASE)
            m_gt = re.search(r"(?:超过|大于)\s*(\d+)\s*(?:米|m)", user_message, re.IGNORECASE)
            m_cap = re.search(r"(?:支持在?|能够下潜至|能够下潜到|能够在?|能下潜到?|可在?|在)\s*(\d+)\s*(?:米|m)", user_message, re.IGNORECASE)

            if m_exact:
                depth_condition.update({"operator": "eq", "depth_m": int(m_exact.group(1)), "parse_status": "valid"})
            elif m_max:
                depth_condition.update({"operator": "lte", "depth_m": int(m_max.group(1)), "parse_status": "valid"})
            elif m_lte:
                depth_condition.update({"operator": "lte", "depth_m": int(m_lte.group(1)), "parse_status": "valid"})
            elif m_lt:
                depth_condition.update({"operator": "lt", "depth_m": int(m_lt.group(1)), "parse_status": "valid"})
            elif m_gte:
                depth_condition.update({"operator": "gte", "depth_m": int(m_gte.group(1)), "parse_status": "valid"})
            elif m_gt:
                gt_start = m_gt.start()
                prefix = user_message[max(0, gt_start - 1):gt_start]
                if prefix == "不":
                    depth_condition.update({"operator": "lte", "depth_m": int(m_gt.group(1)), "parse_status": "valid"})
                else:
                    depth_condition.update({"operator": "gt", "depth_m": int(m_gt.group(1)), "parse_status": "valid"})
            elif m_cap:
                depth_condition.update({"operator": "gte", "depth_m": int(m_cap.group(1)), "parse_status": "valid"})

            # ── 2. 设备名称匹配 ──
            generic_query_words = {"作业", "巡检", "水深", "深度", "机器人", "潜器", "设备", "型号", "能力", "参数", "下潜", "米", "1000", "500", "300", "工具"}
            matched_by_name = []
            for r in all_rovs:
                targets = [r.get("full_name"), r.get("robot_class_name"), r.get("model")] + (r.get("aliases") or [])
                for t in targets:
                    if not t:
                        continue
                    t_clean = str(t).lower().replace(" ", "")
                    if t_clean in generic_query_words:
                        continue
                    if t_clean in query_norm:
                        matched_by_name.append(r)
                        break

            # 检查问句是否指向特定未知设备（如"金牛座"）
            known_device_terms = ["观察级", "工作级", "重载级", "拖拉机", "auv", "rov", "潜器", "机器人", "设备", "型号"]
            has_unknown_device_name = False
            if not matched_by_name:
                m_dev_q = re.search(r"^([^能支持有包含哪些多少]+)(?:能|在|支持|可以|有)", query_norm)
                if m_dev_q:
                    prefix_term = m_dev_q.group(1)
                    if not any(k in prefix_term for k in known_device_terms) and len(prefix_term) >= 2:
                        has_unknown_device_name = True

            # ── 3. 组合逻辑判断与结果构建 ──
            results = []
            list_keywords = ["哪些", "列表", "所有", "有哪些", "分类", "推荐", "选择"]
            is_list_query = any(k in query_norm for k in list_keywords)

            if matched_by_name:
                query_mode = "device_check"
            elif is_list_query:
                query_mode = "device_list"
            else:
                query_mode = "device_check"
                has_unknown_device_name = True

            if query_mode == "device_check":
                if has_unknown_device_name or not matched_by_name:
                    base_resp["found"] = False
                    base_resp["query_mode"] = "device_check"
                    base_resp["depth_condition"] = depth_condition
                    base_resp["results"] = []
                    return base_resp

                for r in matched_by_name:
                    r_copy = dict(r)
                    matches = True
                    if depth_condition["parse_status"] == "valid":
                        op = depth_condition["operator"]
                        d = depth_condition["depth_m"]
                        md = r.get("max_depth_m") or 0
                        if op == "eq":
                            matches = (md == d)
                        elif op == "gte":
                            matches = (md >= d)
                        elif op == "gt":
                            matches = (md > d)
                        elif op == "lte":
                            matches = (md <= d)
                        elif op == "lt":
                            matches = (md < d)
                    r_copy["matches_depth_condition"] = matches
                    results.append(r_copy)

                base_resp["found"] = True
                base_resp["query_mode"] = "device_check"
                base_resp["depth_condition"] = depth_condition
                base_resp["results"] = results
                return base_resp

            # query_mode == "device_list"
            if depth_condition["has_depth_expression"] and depth_condition["parse_status"] == "invalid":
                base_resp["found"] = False
                base_resp["query_mode"] = "device_list"
                base_resp["depth_condition"] = depth_condition
                base_resp["results"] = []
                return base_resp

            if depth_condition["parse_status"] == "valid":
                op = depth_condition["operator"]
                d = depth_condition["depth_m"]
                for r in all_rovs:
                    r_copy = dict(r)
                    md = r.get("max_depth_m") or 0
                    matches = False
                    if op == "eq" and md == d:
                        matches = True
                    elif op == "gte" and md >= d:
                        matches = True
                    elif op == "gt" and md > d:
                        matches = True
                    elif op == "lte" and md <= d:
                        matches = True
                    elif op == "lt" and md < d:
                        matches = True

                    if matches:
                        r_copy["matches_depth_condition"] = True
                        results.append(r_copy)

                base_resp["found"] = bool(results)
                base_resp["query_mode"] = "device_list"
                base_resp["depth_condition"] = depth_condition
                base_resp["results"] = results
                return base_resp

            # 没有深度表达 -> 返回全部
            for r in all_rovs:
                r_copy = dict(r)
                r_copy["matches_depth_condition"] = True
                results.append(r_copy)

            base_resp["found"] = True
            base_resp["query_mode"] = "device_list"
            base_resp["depth_condition"] = depth_condition
            base_resp["results"] = results
            return base_resp

        if query_type == "KNOWLEDGE_QA":
            results = [
                {"category": "task_templates", "templates": self.task_schemas.get("task_templates", {})},
                {"category": "cable_types", "cable_types": self.assets.get("cable_types", [])},
                {"category": "vessels", "vessels": self.assets.get("vessels", [])},
            ]
            base_resp["results"] = results
            base_resp["found"] = True
            return base_resp

        return base_resp
