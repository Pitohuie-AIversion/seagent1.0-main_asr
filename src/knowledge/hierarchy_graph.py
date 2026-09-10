"""
src/knowledge/hierarchy_graph.py — NetworkX 设备 4 级拓扑图谱与级联关系管理器
"""

from __future__ import annotations

import networkx as nx
from typing import TYPE_CHECKING, Any

from .models import RobotSelectionDataError, _norm

if TYPE_CHECKING:
    from ..knowledge_retriever import KnowledgeBase


class HierarchyGraphManager:
    """负责管理 Class -> Family -> Variant -> Unit 4 级拓扑结构图及其连通性判定。"""

    def __init__(self, kb: "KnowledgeBase"):
        self.kb = kb
        self.graph = nx.DiGraph()

    def build_hierarchy_graph(self) -> nx.DiGraph:
        """基于 NetworkX 建立层级有向图。"""
        G = nx.DiGraph()

        # 1. 节点与 Task -> Family 边
        task_templates = self.kb.task_schemas.get("task_templates", {}) or {}
        for task_key, template in task_templates.items():
            task_node = f"task:{task_key}"
            G.add_node(task_node, level="task", raw_id=task_key)
            required_caps = set(template.get("required_capabilities", []) or [])
            for fam_id, fam_cfg in (self.kb.robot_fleet.get("robot_families", {}) or {}).items():
                caps = set(fam_cfg.get("capabilities", []) or [])
                if required_caps and not required_caps.issubset(caps):
                    continue
                fam_node = f"family:{fam_id}"
                G.add_node(
                    fam_node,
                    level="family",
                    raw_id=fam_id,
                    full_name=fam_cfg.get("full_name", fam_id),
                )
                G.add_edge(task_node, fam_node)

        # 2. Class -> Family 边
        families = self.kb.robot_fleet.get("robot_families", {}) or {}
        for fam_id, fam_cfg in families.items():
            fam_node = f"family:{fam_id}"
            G.add_node(
                fam_node,
                level="family",
                raw_id=fam_id,
                full_name=fam_cfg.get("full_name", fam_id),
            )
            cls_id = fam_cfg.get("robot_class")
            if cls_id:
                cls_node = f"class:{cls_id}"
                G.add_node(cls_node, level="class", raw_id=cls_id)
                G.add_edge(cls_node, fam_node)

        # 3. Family -> Variant 边
        variants = self.kb.robot_fleet.get("model_variants", {}) or {}
        for var_id, var_cfg in variants.items():
            var_node = f"variant:{var_id}"
            G.add_node(
                var_node,
                level="variant",
                raw_id=var_id,
                full_name=var_cfg.get("full_name", var_id),
            )
            fam_id = var_cfg.get("family_id")
            if fam_id:
                fam_node = f"family:{fam_id}"
                G.add_edge(fam_node, var_node)

        # 4. Variant -> Unit 边
        units = self.kb.robot_fleet.get("fleet_units", []) or []
        for unit_cfg in units:
            unit_id = unit_cfg.get("unit_id")
            var_id = unit_cfg.get("variant_id")
            if unit_id and var_id:
                unit_node = f"unit:{unit_id}"
                var_node = f"variant:{var_id}"
                G.add_node(unit_node, level="unit", raw_id=unit_id)
                G.add_edge(var_node, unit_node)

        self.graph = G
        return G

    def normalize_node_id(self, item: str, source_level: str | None = None) -> str:
        """支持前缀或裸 Key 的节点名标准化。"""
        if ":" in item:
            return item
        if source_level:
            candidate = f"{source_level}:{item}"
            if candidate in self.graph:
                return candidate
        for prefix in ("class", "family", "variant", "unit", "task"):
            candidate = f"{prefix}:{item}"
            if candidate in self.graph:
                return candidate
        return item

    def get_ancestor_by_level(
        self, node_id: str, target_level: str, source_level: str | None = None
    ) -> str | None:
        """通过 NetworkX 图求取特定级别的唯一上级祖先节点。"""
        norm_node = self.normalize_node_id(node_id, source_level)
        if norm_node not in self.graph:
            return None
        ancestors = nx.ancestors(self.graph, norm_node)
        for anc in ancestors:
            if self.graph.nodes[anc].get("level") == target_level:
                return self.graph.nodes[anc].get("raw_id", anc)
        return None

    def get_descendants_by_level(
        self, node_id: str, target_level: str, source_level: str | None = None
    ) -> list[str]:
        """通过 NetworkX 图求取特定级别的所有下级后代节点。"""
        norm_node = self.normalize_node_id(node_id, source_level)
        if norm_node not in self.graph:
            return []
        descendants = nx.descendants(self.graph, norm_node)
        return [
            self.graph.nodes[desc].get("raw_id", desc)
            for desc in descendants
            if self.graph.nodes[desc].get("level") == target_level
        ]

    def is_valid_cascade_path(
        self,
        ancestor_node: str,
        descendant_node: str,
        ancestor_level: str | None = None,
        descendant_level: str | None = None,
    ) -> bool:
        """通过 NetworkX 图校验 Ancestor 到 Descendant 的连通路径合法性。"""
        norm_anc = self.normalize_node_id(ancestor_node, ancestor_level)
        norm_desc = self.normalize_node_id(descendant_node, descendant_level)
        if norm_anc not in self.graph or norm_desc not in self.graph:
            return False
        return nx.has_path(self.graph, norm_anc, norm_desc)
