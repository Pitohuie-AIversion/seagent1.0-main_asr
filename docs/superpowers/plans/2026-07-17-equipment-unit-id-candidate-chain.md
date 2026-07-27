# Equipment Unit ID Candidate Chain Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ensure `equipment_unit_id` candidates are resolved from the selected robot model and delivered intact to extraction and response prompts.

**Architecture:** `OutputBuilder` owns dynamic allowed-value resolution, `SlotStore` accepts an injected resolver while computing missing fields, and `DialogueManager` connects them with the current task state. `prompts.py` enforces model-specific display and an explicit no-unit response.

**Tech Stack:** Python 3, unittest, YAML-backed KnowledgeBase.

## Global Constraints

- Do not add third-party dependencies.
- Do not change robot fleet data or model-matching rules.
- Do not fall back to another model's unit IDs.
- Do not commit or push; the current worktree already contains unrelated staged changes.
- Use `/root/miniconda3/envs/seagent/bin/python` with `TRANSFORMERS_OFFLINE=1` and `HF_HUB_OFFLINE=1` for tests.

---

### Task 1: Add failing candidate-chain regression tests

**Files:**
- Create: `tests/test_equipment_unit_candidates.py`

**Interfaces:**
- Consumes: `OutputBuilder.get_required()`, `SlotStore.get_missing_slots()`, `build_responder_messages()`.
- Produces: behavioral tests for selected-model filtering, schema immutability, prompt candidates, and no-unit handling.

- [ ] Write tests asserting that `观察级深海机器人 HP` resolves only `OBSROV-HP-001`.
- [ ] Write a SlotStore test whose resolver injects candidates without mutating the input Schema.
- [ ] Write prompt tests for non-empty and empty `equipment_unit_id` candidate lists.
- [ ] Run `env TRANSFORMERS_OFFLINE=1 HF_HUB_OFFLINE=1 /root/miniconda3/envs/seagent/bin/python -m unittest tests.test_equipment_unit_candidates` and verify failures are caused by missing state-aware APIs and prompt instructions.

### Task 2: Preserve dynamic candidates through SlotStore

**Files:**
- Modify: `src/slot_store.py:121`
- Test: `tests/test_equipment_unit_candidates.py`

**Interfaces:**
- Consumes: `allowed_values_resolver: Callable[[dict], list[str]] | None`.
- Produces: `get_missing_slots(required_schema, allowed_values_resolver=None) -> list[dict]` with copied fields and resolved `allowed_values`.

- [ ] Extend `get_missing_slots()` with an optional resolver.
- [ ] Copy each missing field before enrichment so runtime values do not pollute the YAML-backed Schema.
- [ ] Store an empty list when the resolver returns no candidates and propagate resolver exceptions.
- [ ] Run the SlotStore-focused tests and verify they pass.

### Task 3: Bind current task state to candidate resolution

**Files:**
- Modify: `src/output_builder.py:36`
- Modify: `src/dialogue_manager.py:156`
- Modify: `src/dialogue_manager.py:240`
- Test: `tests/test_equipment_unit_candidates.py`

**Interfaces:**
- Produces: `OutputBuilder.resolve_allowed_values(field_def, task_type_key='', task_state=None) -> list[str]`.
- Produces: `OutputBuilder.get_required(task_type_key, mode='normal', task_state=None) -> list[dict]`.

- [ ] Add the public resolver as a thin wrapper over existing `_resolve_allowed()`.
- [ ] Pass `task_state` through `get_required()` so extraction candidates are model-specific.
- [ ] Pass the transactional current state when DialogueManager builds extraction requirements.
- [ ] Inject a state-aware resolver when DialogueManager computes SlotStore missing fields.
- [ ] Run the candidate filtering and integration-focused tests and verify they pass.

### Task 4: Add deterministic equipment-unit prompt behavior

**Files:**
- Modify: `src/prompts.py:177`
- Test: `tests/test_equipment_unit_candidates.py`

**Interfaces:**
- Consumes: the `equipment_unit_id` missing-field entry and its resolved `allowed_values`.
- Produces: an explicit model-specific candidate instruction or an explicit no-available-unit instruction.

- [ ] Keep the existing equipment-type-first dependency branch.
- [ ] When equipment type is confirmed and candidates exist, require the responder to list only those candidates and forbid knowledge fallback.
- [ ] When candidates are empty, require the exact meaning “当前型号暂无可用机器人编号” and forbid cross-model recommendations.
- [ ] Run prompt-focused tests and verify they pass.

### Task 5: Verify the repair

**Files:**
- Verify: `src/slot_store.py`
- Verify: `src/output_builder.py`
- Verify: `src/dialogue_manager.py`
- Verify: `src/prompts.py`
- Verify: `tests/test_equipment_unit_candidates.py`

- [ ] Run the focused test module.
- [ ] Run `tests.test_slot_consistency` to check SlotStore regressions.
- [ ] Run the full unittest suite in offline mode.
- [ ] Run `git diff --check` and inspect the final scoped diff.
- [ ] Report new-test results separately from unrelated pre-existing failures.
