---
name: task_collection_maintenance
description: Procedures for maintaining, updating, or troubleshooting task templates, parameters extraction, and value normalization logic.
---

# Task Collection & Parameter Extraction Maintenance Skill

This skill outlines how to modify, add, or troubleshoot the fields and logic required for collecting and compiling task information.

## 1. Modifying/Adding Task Schemas (`config/task_schemas.yaml`)
Task templates are defined in [task_schemas.yaml](../../../config/task_schemas.yaml).
- **Adding a task parameter**: Locate the relevant task type (`pipeline_inspection`, `pipeline_burial` or `tree_valve_operation`) and add the field configuration under `output_schema`.
- **Configuring Normal vs Emergency Mode**:
  - Normal mode fields are defined under `output_schema.normal`.
  - Emergency mode fields are defined under `output_schema.emergency`.
  - Field metadata options:
    - `key` and `label`: machine field name and display label.
    - `type`: `string`, `number`, `list`, `datetime`, `coord`, `raw`, `tasktype`, `auto` or `fixed`.
    - `auto`/`fixed` are type values, not boolean flags. They are omitted from user-facing missing-field prompts. `fixed_value` supplies a fixed field value.
    - `OutputBuilder` treats a missing non-`auto`/`fixed` field in the selected schema as required; a separate `required` boolean is not the current switch.
    - `allowed_values` or `allowed_values_ref`: lists of standard values or references to asset lists (vessels, ROVs, etc.).

## 2. Maintaining Extraction Prompts & Logic (`src/extraction/extractor.py`)
[extractor.py](../../../src/extraction/extractor.py) is responsible for calling LLM to extract task fields from natural language conversation.
- **Adjusting extraction prompt**: Modify the prompt inside `src/extraction/extractor.py` to refine how dates, coordinates, and lists are formatted by the LLM.
- **Workflow Steps**:
  1. **Task Type Identification**: Check if the task type has been determined. If not, prompt and identify task type first.
  2. **JSON diff logic**: Only return the diff (new or updated parameters) of the current turn, avoiding repeating existing fields.
  3. **Continuous Commands & Clarifications**: Evaluate context history to handle continuous edits (e.g., changes to previously stated parameters) and confirmation of suggestions.
  4. **Fuzzy ROV description**: Extract ambiguous ROV names to `rov_description` first to prevent direct assignment of incorrect standard types.
  5. **ROV candidate resolution**: `resolve_rov_description()` delegates ambiguous descriptions to the candidate resolver. Candidate resolution is separate from the grounded recommendation flow below; do not treat a candidate as a confirmed selection.
  6. **Numbered Option Selection**: If the assistant lists numbered choices (e.g. "1.", "2.", "3.") for a parameter in the previous message, and the user replies with a digit (e.g. "1", "2"), the extractor must map the digit back to the corresponding standard option.

## 3. Customizing Parameter Normalization (`src/extraction/normalizer.py`)
[normalizer.py](../../../src/extraction/normalizer.py) maps raw natural language inputs to their corresponding standard enum options.
- **Mapping mechanism**:
  1. Exact matching: checks if the value matches any allowed value.
  2. Fallback to LLM: asks LLM to choose the closest standard value.
  3. Discards invalid options: if LLM returns a value not in the options, normalizer returns `None`.
- **List fields**: Splitting string by delimiters (e.g. comma, space) and normalizing each item individually.

## 4. Troubleshooting JSON output building (`src/dispatch/output_builder.py`)
[output_builder.py](../../../src/dispatch/output_builder.py) compiles extracted and normalized values into the final flat JSON output.
- **Schema Routing**: Route validation and field construction based on selected task types and execution mode (emergency vs. normal).
- **Filtering System Fields**: Exclude fields marked `auto` or `fixed` from the user-facing prompts.
- **Missing fields detection**: Compiles a list of required fields that have not yet been successfully filled, which the dialogue manager uses to generate follow-up questions.
- **ID Generation**: `preview_task_id()` provides a non-consuming estimate; `reserve_task_id()` allocates the authoritative ID at final publication through [id_sequence.py](../../../src/dispatch/id_sequence.py). Ordinary rendering must not consume a sequence number.
- **Data Type Validation**: Verify that coordinates, numeric values, datetimes, and lists adhere to correct schemas, and references (vessels, payloads) are matched correctly in assets. Caches lookup results to improve normalization efficiency.

## 5. Pending Action & Confirm/Reject Flow (`src/session/interaction_plan.py`, `src/session/intent_router.py`)
`InteractionPlan.pending_action` (`"confirm"`, `"reject"`, or `None`) is the protocol for the active `pending_oilfield` candidate. Both confirmation and rejection use WRITE and are processed by [oilfield_confirmation.py](../../../src/handlers/oilfield_confirmation.py). Other turns must leave it unset.

Accepting a previously offered single robot recommendation instead uses WRITE with `relation=recommend`, the same subject type/value, and visible-source validation. Soft-warning acknowledgement uses `warning_action=acknowledge`; neither action should be encoded as `pending_action`.

## 6. Grounded Recommendation Logic
[conversation_router.py](../../../src/handlers/conversation_router.py) delegates grounded recommendations and device-class answers to [grounded_catalog.py](../../../src/handlers/grounded_catalog.py). `DialogueManager` retains compatibility delegates.
- READ with `relation=recommend` uses current legal options and confirmed task evidence. Some semantic disambiguation may still call the LLM; the result must remain inside the validated candidate domain.
- If the recommendation branch does not match, the router may try a grounded device-class answer.
- [equipment_scoping.py](../../../src/handlers/equipment_scoping.py) limits accepted recommendations to the preceding visible offer and the current legal candidate domain before slot mutation.
