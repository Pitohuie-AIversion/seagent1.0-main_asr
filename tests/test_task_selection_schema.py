"""Keep the task selector from generating parameter-stage candidates."""
import jsonschema
import pytest

from src.extraction.extractor import ParameterExtractor, TASK_SELECTION_JSON_SCHEMA
from src.llm_client import SLOT_EXTRACTION_JSON_SCHEMA
from tests.interaction_plan_support import ScriptedLLM, extraction_result as legacy_extraction_result, slot_candidate


def extraction_result(*candidates, **kwargs):
    result = legacy_extraction_result(*candidates, **kwargs)
    return {
        "slot_candidates": result["slot_candidates"],
        "list_mutations": result["list_mutations"],
        "time_relation": None,
        "unresolved": result["unresolved"],
    }


@pytest.mark.parametrize("key", ["start_time", "water_depth", "duration"])
def test_parameter_candidates_from_real_failed_turn_are_not_stage_one_output(key):
    result = extraction_result(slot_candidate(key, "300", raw_value="300"))
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(result, TASK_SELECTION_JSON_SCHEMA)


def test_task_selector_schema_does_not_restrict_parameter_stage():
    task = extraction_result(slot_candidate("task_type_key", "pipeline_inspection"))
    jsonschema.validate(task, TASK_SELECTION_JSON_SCHEMA)
    jsonschema.validate(extraction_result(unresolved=["请明确要执行哪一种任务"]), TASK_SELECTION_JSON_SCHEMA)
    parameters = extraction_result(slot_candidate("water_depth", 300))
    jsonschema.validate(parameters, SLOT_EXTRACTION_JSON_SCHEMA)


def test_stage_one_passes_the_restricted_schema_to_the_model():
    class RecordingLLM(ScriptedLLM):
        def extract_json(self, messages, max_tokens=800, role=None, json_schema=None):
            self.schema = json_schema
            return super().extract_json(messages, max_tokens, role, json_schema)

    llm = RecordingLLM(extractions=[extraction_result(
        slot_candidate("task_type_key", "pipeline_inspection"),
        slot_candidate("task_type", "管缆巡检"),
    )])
    result = ParameterExtractor(llm).extract_updates(
        "检查海底输送线路是否损坏", {}, None,
        task_type_map={"管缆巡检": "pipeline_inspection"},
    )
    assert llm.schema == TASK_SELECTION_JSON_SCHEMA
    assert any(c["normalized_value"] == "pipeline_inspection" for c in result["slot_candidates"])


def test_legacy_client_without_schema_or_role_still_works():
    class LegacyLLM:
        def extract_json(self, messages, max_tokens=800):
            return extraction_result(unresolved=["需要明确任务类型"])

    result = ParameterExtractor(LegacyLLM()).extract_updates("做这个任务", {}, None)
    assert "需要明确任务类型" in result["unresolved"]


@pytest.mark.parametrize("schema", [TASK_SELECTION_JSON_SCHEMA, SLOT_EXTRACTION_JSON_SCHEMA])
def test_deployed_grammar_requires_complete_nonempty_results(schema):
    import json
    xgrammar = pytest.importorskip("xgrammar")
    from xgrammar.testing import _is_grammar_accept_string

    grammar = xgrammar.Grammar.from_json_schema(schema)
    valid = extraction_result(slot_candidate("task_type_key", "pipeline_inspection"))
    assert _is_grammar_accept_string(grammar, json.dumps(valid))
    assert not _is_grammar_accept_string(grammar, json.dumps({"slot_candidates": valid["slot_candidates"]}))
    assert not _is_grammar_accept_string(grammar, json.dumps(extraction_result()))
    assert _is_grammar_accept_string(grammar, json.dumps(extraction_result(unresolved=["clarify task"])))


def test_deployed_task_selector_grammar_rejects_parameter_fields():
    import json
    xgrammar = pytest.importorskip("xgrammar")
    from xgrammar.testing import _is_grammar_accept_string

    grammar = xgrammar.Grammar.from_json_schema(TASK_SELECTION_JSON_SCHEMA)
    assert not _is_grammar_accept_string(grammar, json.dumps(
        extraction_result(slot_candidate("start_time", "2023-10-27T09:00:00")),
    ))
