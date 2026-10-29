"""Regression tests for the legacy module paths kept during the architecture refactor.

The refactor moved implementations into parent-child subpackages while preserving
the original import paths. These tests make that compatibility contract explicit.
"""

import importlib

import pytest


LEGACY_MODULES = [
    (
        "src.slots.candidate_resolver",
        "src.slots.resolution.candidate_resolver",
        "CandidateResolver",
    ),
    (
        "src.slots.slot_list_mutation",
        "src.slots.mutation.slot_list_mutation",
        "SlotListMutationEngine",
    ),
    (
        "src.slots.slot_snapshot_codec",
        "src.slots.codec.slot_snapshot_codec",
        "SlotSnapshotCodec",
    ),
]


@pytest.mark.parametrize("legacy_name, implementation_name, symbol", LEGACY_MODULES)
def test_legacy_module_path_resolves_to_implementation(
    legacy_name: str,
    implementation_name: str,
    symbol: str,
):
    legacy = importlib.import_module(legacy_name)
    implementation = importlib.import_module(implementation_name)

    assert legacy is implementation
    assert legacy.__name__ == implementation.__name__
    assert getattr(legacy, symbol) is getattr(implementation, symbol)


def test_candidate_resolver_patch_through_legacy_path_is_visible_to_implementation(monkeypatch):
    legacy = importlib.import_module("src.slots.candidate_resolver")
    implementation = importlib.import_module("src.slots.resolution.candidate_resolver")

    def patched_prefix(value: str) -> str:
        return f"patched:{value}"

    monkeypatch.setattr(
        legacy.CandidateResolver,
        "strip_colloquial_prefixes",
        staticmethod(patched_prefix),
    )

    assert implementation.CandidateResolver.strip_colloquial_prefixes("value") == "patched:value"
