"""Regression tests for the legacy module paths kept during the architecture refactor.

The refactor moved implementations into parent-child subpackages while preserving
the original import paths. These tests make that compatibility contract explicit.
"""

from datetime import datetime
import importlib
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Keep this contract explicit: discovering only the shims that still exist would
# let an accidentally deleted legacy import path disappear from the tests too.
LEGACY_MODULES = [
    (
        "src.dispatch.atomic_task_storage",
        "src.dispatch.storage.atomic_task_storage",
        "TaskPublishLock",
    ),
    (
        "src.dispatch.id_sequence",
        "src.dispatch.storage.id_sequence",
        "next_daily_id",
    ),
    (
        "src.dispatch.result_paths",
        "src.dispatch.storage.result_paths",
        "get_result_dir",
    ),
    (
        "src.dispatch.task_capability_adapter",
        "src.dispatch.validation.task_capability_adapter",
        "TaskCapabilityAdapter",
    ),
    (
        "src.dispatch.task_intent_schema_validator",
        "src.dispatch.validation.task_intent_schema_validator",
        "validate_task_intent",
    ),
    (
        "src.handlers.command_predicates",
        "src.handlers.lifecycle.command_predicates",
        "is_final_publish_confirmation",
    ),
    (
        "src.handlers.equipment_assertion_grounding",
        "src.handlers.equipment.equipment_assertion_grounding",
        "ground_equipment_values",
    ),
    (
        "src.handlers.equipment_cascade",
        "src.handlers.equipment.equipment_cascade",
        "EquipmentCascadeResolver",
    ),
    (
        "src.handlers.equipment_collapse",
        "src.handlers.equipment.equipment_collapse",
        "EquipmentCollapseHandler",
    ),
    (
        "src.handlers.equipment_scoping",
        "src.handlers.equipment.equipment_scoping",
        "EquipmentScopingHandler",
    ),
    (
        "src.handlers.execution_control",
        "src.handlers.lifecycle.execution_control",
        "ExecutionControlHandler",
    ),
    (
        "src.handlers.explicit_value_grounding",
        "src.handlers.extraction.explicit_value_grounding",
        "ground_explicit_values",
    ),
    (
        "src.handlers.grounded_catalog",
        "src.handlers.routing.grounded_catalog",
        "GroundedCatalogHandler",
    ),
    (
        "src.handlers.hypothetical_time_preview",
        "src.handlers.routing.hypothetical_time_preview",
        "build_hypothetical_time_reply",
    ),
    (
        "src.handlers.knowledge_reply_grounder",
        "src.handlers.routing.knowledge_reply_grounder",
        "ground_payload_requirement_reply",
    ),
    (
        "src.handlers.off_topic_gate",
        "src.handlers.routing.off_topic_gate",
        "check_off_topic_gate",
    ),
    (
        "src.handlers.oilfield_confirmation",
        "src.handlers.extraction.oilfield_confirmation",
        "OilfieldConfirmationHandler",
    ),
    (
        "src.handlers.payload_mutation",
        "src.handlers.extraction.payload_mutation",
        "PayloadMutationManager",
    ),
    (
        "src.handlers.slot_extraction_pipeline",
        "src.handlers.extraction.slot_extraction_pipeline",
        "SlotExtractionPipeline",
    ),
    (
        "src.handlers.slot_transaction",
        "src.handlers.extraction.slot_transaction",
        "SlotTransactionManager",
    ),
    (
        "src.handlers.task_structure_grounding",
        "src.handlers.routing.task_structure_grounding",
        "ground_task_structure_reply",
    ),
    (
        "src.handlers.task_transition",
        "src.handlers.lifecycle.task_transition",
        "TaskTransitionManager",
    ),
    (
        "src.handlers.telemetry_status",
        "src.handlers.routing.telemetry_status",
        "TelemetryStatusHandler",
    ),
    (
        "src.handlers.write_reply_grounder",
        "src.handlers.grounding.write_reply_grounder",
        "WriteReplyGrounder",
    ),
    (
        "src.knowledge.hierarchy_graph",
        "src.knowledge.selection.hierarchy_graph",
        "HierarchyGraphManager",
    ),
    (
        "src.knowledge.models",
        "src.knowledge.selection.models",
        "RobotSelectionDataError",
    ),
    (
        "src.knowledge.prompt_grounder",
        "src.knowledge.selection.prompt_grounder",
        "PromptGrounder",
    ),
    (
        "src.knowledge.selection_engine",
        "src.knowledge.selection.selection_engine",
        "RobotSelectionEngine",
    ),
    (
        "src.knowledge.unit_resolver",
        "src.knowledge.selection.unit_resolver",
        "UnitResolver",
    ),
    (
        "src.knowledge.variant_evaluator",
        "src.knowledge.selection.variant_evaluator",
        "VariantEvaluator",
    ),
    (
        "src.slots.candidate_resolver",
        "src.slots.resolution.candidate_resolver",
        "CandidateResolver",
    ),
    (
        "src.slots.normalization_contract",
        "src.slots.resolution.normalization_contract",
        "NormalizationApplyPlan",
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
    (
        "src.slots.task_patch",
        "src.slots.mutation.task_patch",
        "TaskPatch",
    ),
    (
        "src.slots.task_slot_filter",
        "src.slots.mutation.task_slot_filter",
        "TaskSlotFilter",
    ),
    (
        "src.slots.visible_selection_provenance",
        "src.slots.resolution.visible_selection_provenance",
        "parse_ordinal_reference",
    ),
    (
        "src.temporal.duration_parser",
        "src.temporal.parsers.duration_parser",
        "DurationSpec",
    ),
    (
        "src.temporal.relative_time_parser",
        "src.temporal.parsers.relative_time_parser",
        "parse_relative_datetime",
    ),
    (
        "src.temporal.simulated_time",
        "src.temporal.context.simulated_time",
        "get_simulated_time",
    ),
    (
        "src.temporal.time_context",
        "src.temporal.context.time_context",
        "get_time_context",
    ),
    (
        "src.temporal.time_range_engine",
        "src.temporal.context.time_range_engine",
        "parse_time_range",
    ),
    ("src.web.routes_asr", "src.web.routes.asr", "asr_bp"),
    ("src.web.routes_chat", "src.web.routes.chat", "chat_bp"),
    ("src.web.routes_dev", "src.web.routes.dev", "dev_bp"),
    ("src.web.routes_mcp", "src.web.routes.mcp", "mcp_bp"),
    ("src.web.routes_pages", "src.web.routes.pages", "pages_bp"),
    ("src.web.routes_robot", "src.web.routes.robot", "robot_bp"),
    ("src.web.routes_time_history", "src.web.routes.time_history", "time_history_bp"),
    ("src.web.routes_translate", "src.web.routes.translate", "translate_bp"),
]


@pytest.mark.parametrize(
    "legacy_name, implementation_name, symbol",
    LEGACY_MODULES,
    ids=[legacy for legacy, _, _ in LEGACY_MODULES],
)
def test_legacy_module_path_resolves_to_implementation(
    legacy_name: str,
    implementation_name: str,
    symbol: str,
):
    legacy = importlib.import_module(legacy_name)
    implementation = importlib.import_module(implementation_name)

    assert legacy is implementation
    assert sys.modules[legacy_name] is sys.modules[implementation_name]
    assert legacy.__name__ == implementation.__name__
    assert getattr(legacy, symbol) is getattr(implementation, symbol)


@pytest.mark.parametrize(
    "legacy_name, implementation_name, symbol",
    LEGACY_MODULES,
    ids=[legacy for legacy, _, _ in LEGACY_MODULES],
)
def test_module_global_patch_is_shared_in_both_directions(
    legacy_name: str,
    implementation_name: str,
    symbol: str,
    monkeypatch,
):
    legacy = importlib.import_module(legacy_name)
    implementation = importlib.import_module(implementation_name)
    original = getattr(implementation, symbol)
    legacy_patch = object()
    implementation_patch = object()

    # Replace the module binding, not an attribute on a shared class: a simple
    # "from implementation import Symbol" shim would fail this contract.
    with monkeypatch.context() as patch:
        patch.setattr(legacy, symbol, legacy_patch)
        assert getattr(implementation, symbol) is legacy_patch
        patch.setattr(implementation, symbol, implementation_patch)
        assert getattr(legacy, symbol) is implementation_patch

    assert getattr(legacy, symbol) is original
    assert getattr(implementation, symbol) is original


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


@pytest.mark.parametrize(
    "first_module",
    [
        "src.temporal.relative_time_parser",
        "src.temporal.parsers.relative_time_parser",
        "src.temporal.time_range_engine",
        "src.temporal.context.time_range_engine",
    ],
)
def test_temporal_reexports_work_from_each_cold_import_entrypoint(first_module):
    # Only the circular-import boundary needs fresh interpreters. The 50 alias
    # contracts above share pytest's process to avoid repeatedly loading models.
    code = """
from datetime import datetime
import importlib
import sys

assert not any(name.startswith('src.temporal') for name in sys.modules)
importlib.import_module(sys.argv[1])
legacy_parser = importlib.import_module('src.temporal.relative_time_parser')
parser = importlib.import_module('src.temporal.parsers.relative_time_parser')
legacy_engine = importlib.import_module('src.temporal.time_range_engine')
engine = importlib.import_module('src.temporal.context.time_range_engine')
assert legacy_parser is parser
assert legacy_engine is engine

for name in (
    'TimeFieldState', 'TimePointSpec', 'TimeRangeParseResult',
    'format_datetime_template', 'parse_time_range',
):
    assert getattr(legacy_parser, name) is getattr(engine, name), name

result = legacy_parser.parse_time_range(
    '明天下午3点', '两个小时', None, base_dt=datetime(2026, 10, 29, 10, 0),
)
assert result.success, result
assert isinstance(result, engine.TimeRangeParseResult)
assert result.start_time.state is engine.TimeFieldState.EXPLICIT
assert result.end_time.state is engine.TimeFieldState.DERIVED
assert result.start_time.iso_string == '2026-10-30T15:00:00'
assert result.end_time.iso_string == '2026-10-30T17:00:00'
"""
    result = subprocess.run(
        [sys.executable, "-c", code, first_module],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_temporal_aliases_and_consumers_share_the_clock_state(monkeypatch):
    legacy = importlib.import_module("src.temporal.simulated_time")
    implementation = importlib.import_module("src.temporal.context.simulated_time")
    legacy_context = importlib.import_module("src.temporal.time_context")
    context = importlib.import_module("src.temporal.context.time_context")
    parser = importlib.import_module("src.temporal.relative_time_parser")
    clock = legacy.SimulatedTime()

    # Swap the singleton through the legacy module, leaving the application's
    # existing clock untouched. Freeze only this module's time source.
    monkeypatch.setattr(legacy, "_simulated_time", clock)
    monkeypatch.setattr(legacy, "time", SimpleNamespace(time=lambda: 1000.0))
    assert implementation.get_simulated_time() is clock
    assert legacy.get_simulated_time() is clock

    first = datetime(2026, 10, 29, 9, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
    legacy.get_simulated_time().set_current_time(first)
    assert implementation.get_current_datetime() == first
    assert context.get_time_context().current == first
    assert parser.parse_relative_datetime("明天下午3点") == "2026-10-30T15:00:00"

    second = datetime(2026, 11, 2, 14, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    implementation.get_simulated_time().set_current_time(second)
    assert legacy.get_current_datetime() == second
    assert legacy_context.get_time_context().current == second
    assert parser.parse_relative_datetime("明天下午3点") == "2026-11-03T15:00:00"
