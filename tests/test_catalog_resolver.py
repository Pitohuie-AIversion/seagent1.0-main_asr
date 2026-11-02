# -*- coding: utf-8 -*-
"""
test_catalog_resolver.py

Tests for CatalogResolver in src/dispatch/catalog_resolver.py.
Validates:
1. Catalog resolution across classes, families, models, units, and payloads.
2. Alias indexing and mapping separation (unique vs ambiguous aliases).
3. Allowed values resolution with caching.
4. Delegation from OutputBuilder to CatalogResolver.
"""

import pytest
from src.knowledge_retriever import KnowledgeBase
from src.dispatch.catalog_resolver import CatalogResolver
from src.dispatch.output_builder import OutputBuilder


@pytest.fixture
def kb():
    return KnowledgeBase()


@pytest.fixture
def resolver(kb):
    return CatalogResolver(kb)


class TestCatalogResolver:
    def test_init_and_cache(self, kb):
        cache = {}
        resolver = CatalogResolver(kb, cache)
        assert resolver.kb is kb
        assert resolver._ref_cache is cache

    def test_resolve_allowed_inline(self, resolver):
        field_def = {"allowed_values": ["val_a", "val_b"]}
        assert resolver.resolve_allowed_values(field_def) == ["val_a", "val_b"]

    def test_resolve_allowed_tasktype(self, resolver):
        field_def = {"type": "tasktype"}
        values = resolver.resolve_allowed_values(field_def, task_type_key="pipeline_inspection")
        assert isinstance(values, list)
        assert len(values) > 0

    def test_resolve_allowed_robot_families(self, resolver):
        field_def = {"allowed_values_ref": "robot_family_full_names"}
        families = resolver.resolve_allowed_values(field_def, task_type_key="pipeline_inspection")
        assert isinstance(families, list)
        assert len(families) > 0

    def test_resolve_candidate_catalog_structure(self, resolver):
        field_def = {"allowed_values_ref": "robot_family_full_names"}
        catalog = resolver.resolve_candidate_catalog(
            field_def,
            task_type_key="pipeline_inspection",
        )
        assert isinstance(catalog, list)
        assert len(catalog) > 0
        for entry in catalog:
            assert "canonical_value" in entry
            assert "aliases" in entry
            assert "display_name" in entry
            assert "parent" in entry

    def test_build_alias_indexes_separation(self, resolver):
        test_catalog = [
            {"canonical_value": "Unit-1", "aliases": ["alpha", "shared_alias"]},
            {"canonical_value": "Unit-2", "aliases": ["beta", "shared_alias"]},
            {"canonical_value": "Unit-3", "aliases": ["gamma"]},
        ]
        mappings, ambiguous = resolver.build_alias_indexes(test_catalog)
        # alpha and beta are unambiguous
        assert mappings["alpha"] == "Unit-1"
        assert mappings["beta"] == "Unit-2"
        assert mappings["gamma"] == "Unit-3"
        # shared_alias is ambiguous
        assert "shared_alias" in ambiguous
        assert set(ambiguous["shared_alias"]) == {"Unit-1", "Unit-2"}
        assert "shared_alias" not in mappings

    def test_resolve_alias_mappings_for_family(self, resolver):
        field_def = {"allowed_values_ref": "robot_family_full_names"}
        mappings = resolver.resolve_alias_mappings(field_def, task_type_key="pipeline_inspection")
        assert isinstance(mappings, dict)

    def test_output_builder_delegation(self, kb):
        ob = OutputBuilder(kb)
        assert hasattr(ob, "catalog_resolver")
        assert isinstance(ob.catalog_resolver, CatalogResolver)

        field_def = {"allowed_values_ref": "robot_family_full_names"}
        allowed_direct = ob.catalog_resolver.resolve_allowed_values(field_def, task_type_key="pipeline_inspection")
        allowed_delegated = ob.resolve_allowed_values(field_def, task_type_key="pipeline_inspection")
        assert allowed_direct == allowed_delegated

    def test_resolve_allowed_variants(self, resolver):
        field_def = {"allowed_values_ref": "robot_variant_full_names"}
        variants = resolver.resolve_allowed_values(field_def, task_type_key="pipeline_inspection")
        assert isinstance(variants, list)
        assert len(variants) > 0

    def test_resolve_allowed_vessels(self, resolver):
        field_def = {"allowed_values_ref": "vessel_ids"}
        vessels = resolver.resolve_allowed_values(field_def, task_type_key="pipeline_inspection")
        assert isinstance(vessels, list)
        assert len(vessels) > 0

    def test_resolve_allowed_payloads(self, resolver):
        field_def = {"allowed_values_ref": "payload_options.pipeline_inspection"}
        payloads = resolver.resolve_allowed_values(field_def, task_type_key="pipeline_inspection")
        assert isinstance(payloads, list)
        assert len(payloads) > 0

    def test_resolve_allowed_unknown_ref(self, resolver):
        field_def = {"allowed_values_ref": "non_existent_ref_12345"}
        res = resolver.resolve_allowed_values(field_def, task_type_key="pipeline_inspection")
        assert res == []

    def test_candidate_catalog_empty_for_unsupported_ref(self, resolver):
        field_def = {"allowed_values_ref": "unknown_catalog_ref"}
        catalog = resolver.resolve_candidate_catalog(field_def, task_type_key="pipeline_inspection")
        assert catalog == []

