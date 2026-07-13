---
name: task_collection_maintenance
description: Procedures for maintaining, updating, or troubleshooting task templates, parameters extraction, and value normalization logic.
---

# Task Collection & Parameter Extraction Maintenance Skill

This skill outlines how to modify, add, or troubleshoot the fields and logic required for collecting and compiling task information.

## 1. Modifying/Adding Task Schemas (`config/task_schemas.yaml`)
Task templates are defined in [task_schemas.yaml](file:///root/mzy/seagent1.0-main_asr/config/task_schemas.yaml).
- **Adding a task parameter**: Locate the relevant task type (`pipeline_inspection` or `tree_valve_operation`) and add the field configuration under `fields`.
- **Configuring Normal vs Emergency Mode**:
  - Normal mode fields are defined under `normal_fields` or the default fields list.
  - Emergency mode fields represent a reduced subset of critical fields and are defined under `emergency_fields`.
  - Field metadata options:
    - `type`: `string`, `number`, `list`, `datetime`, `coordinate`.
    - `required`: boolean.
    - `auto`/`fixed`: fields that are calculated automatically (e.g. `task_id`) or static, and should not be asked to the user.
    - `allowed_values` or `allowed_values_ref`: lists of standard values or references to asset lists (vessels, ROVs, etc.).

## 2. Maintaining Extraction Prompts & Logic (`src/extractor.py`)
[extractor.py](file:///root/mzy/seagent1.0-main_asr/src/extractor.py) is responsible for calling LLM to extract task fields from natural language conversation.
- **Adjusting extraction prompt**: Modify the prompt inside `src/extractor.py` to refine how dates, coordinates, and lists are formatted by the LLM.
- **Workflow Steps**:
  1. **Task Type Identification**: Check if the task type has been determined. If not, prompt and identify task type first.
  2. **JSON diff logic**: Only return the diff (new or updated parameters) of the current turn, avoiding repeating existing fields.
  3. **Continuous Commands & Clarifications**: Evaluate context history to handle continuous edits (e.g., changes to previously stated parameters) and confirmation of suggestions.
  4. **Fuzzy ROV description**: Extract ambiguous ROV names to `rov_description` first to prevent direct assignment of incorrect standard types.
  5. **ROV model recommendations**: Recommend up to 3 candidate ROVs from `robot_fleet.yaml` based on task type and equipment metadata.
  6. **Numbered Option Selection**: If the assistant lists numbered choices (e.g. "1.", "2.", "3.") for a parameter in the previous message, and the user replies with a digit (e.g. "1", "2"), the extractor must map the digit back to the corresponding standard option.

## 3. Customizing Parameter Normalization (`src/normalizer.py`)
[normalizer.py](file:///root/mzy/seagent1.0-main_asr/src/normalizer.py) maps raw natural language inputs to their corresponding standard enum options.
- **Mapping mechanism**:
  1. Exact matching: checks if the value matches any allowed value.
  2. Fallback to LLM: asks LLM to choose the closest standard value.
  3. Discards invalid options: if LLM returns a value not in the options, normalizer returns `None`.
- **List fields**: Splitting string by delimiters (e.g. comma, space) and normalizing each item individually.

## 4. Troubleshooting JSON output building (`src/output_builder.py`)
[output_builder.py](file:///root/mzy/seagent1.0-main_asr/src/output_builder.py) compiles extracted and normalized values into the final flat JSON output.
- **Schema Routing**: Route validation and field construction based on selected task types and execution mode (emergency vs. normal).
- **Filtering System Fields**: Exclude fields marked `auto` or `fixed` from the user-facing prompts.
- **Missing fields detection**: Compiles a list of required fields that have not yet been successfully filled, which the dialogue manager uses to generate follow-up questions.
- **ID Generation**: Interfaces with [id_sequence.py](file:///root/mzy/seagent1.0-main_asr/src/id_sequence.py) to build incremental `task_id` tags (e.g. based on date and serial sequence) while scanning existing files to prevent duplicates.
- **Data Type Validation**: Verify that coordinates, numeric values, datetimes, and lists adhere to correct schemas, and references (vessels, payloads) are matched correctly in assets. Caches lookup results to improve normalization efficiency.
