# SEAgent Current Design Contract & Architecture Specification

This document describes the design contract and implementation boundaries checked on 2026-09-30. Requirements marked MUST describe intended invariants; the explicit implementation notes below identify known gaps rather than claiming every requirement is implemented or newly verified.

---

## 1. Routing & Interaction Types

### 1.1 InteractionPlan Semantic Authority (ADR-005)
- The semantic router requests a structured `InteractionPlan` with `operation`: `READ`, `WRITE`, `CONTROL`, or `CLARIFY`. Deterministic control paths may be handled before a model call.
- ADR-005 intends `operation` to be the semantic routing authority. **Current exception:** `_call_llm_router()` still promotes some `READ` / `CLARIFY` results to `WRITE` for grounded candidate/alias selections, list selections and fixed task-start expressions, then revalidates the plan. See [ADR-005's implementation note](decisions/ADR-005-llm-semantic-authority.md); the target of no heuristic operation changes is not fully implemented.
- If the LLM returns an invalid protocol or is unavailable, the system MUST fall back to `CLARIFY` — it MUST NOT guess a write operation.
- Low-confidence `WRITE` or `CONTROL` operations MUST be demoted to `CLARIFY` to prevent uncertain semantics from producing state side effects.

### 1.2 Intent Router Dichotomy
The system classifies all natural language user inputs into two primary interaction types:
- **`QUERY` / `READ`**: Information retrieval, status check, device capability inquiry, knowledge base Q&A, or general conversation.
- **`WRITE` / `CONTROL`**: Task creation, parameter modification, equipment selection, field confirmation, or cancellation.

### 1.3 Query State Invariance Rule
- A `READ` / `CLARIFY` interaction **MUST NOT** mutate `SlotStore`, `task_state`, slot values, or `SlotStore.version`.
- A `READ` / `CLARIFY` interaction **MUST NOT** change the current dialogue phase (`phase`).
- `READ` interactions bypass `Extractor` field extraction and validation commits.

### 1.4 Write Pipeline
- `WRITE` produces candidates through extraction, grounding and normalization; schema-accepted values update SlotStore before task-level constraint validation. Violations preserve a correction context and prevent publication.
- `CONTROL` is handled by the relevant phase handler for confirmation, cancellation or warning acknowledgement; not every control action runs the extractor.
- A model returning `WRITE` does not guarantee a field commit or publication.

---

## 2. Control Commands & Negation Syntax

### 2.1 Control Intent Definitions
- **Publish (`TASK_CONFIRM` / `"确认发布"`):** In `confirming`, explicit publication intent triggers TaskIntent persistence and history snapshot handling. A generic `"确认"` alone requests final publication confirmation; it does not itself publish.
- **Cancel (`TASK_CANCEL` / `"取消任务"`, `"放弃"`):** Resets current task state and sets phase to `rejected`.
- **Accept warning (`"忽略警告"`, `"接受风险"`):** Explicit acceptance in `blocked_soft` records acknowledgement against the current validation context and proceeds after revalidation. `"继续"` alone is not explicit risk acceptance, and acceptance is separate from publication.

### 2.2 Negation Handling Rules
- **Cancel Negation (`"不要取消"`, `"别取消"`, `"不取消"`):** MUST NOT trigger task cancellation or phase transition to `rejected`.
- **Publish Negation (`"不要发布"`, `"不发布"`):** MUST NOT trigger task publication in `confirming` phase.
- **Confirmation Negation (`"不确认"`):** MUST NOT confirm task or publish intent JSON.

### 2.3 Control Command Processing Order
- Control commands (e.g., confirm, cancel, continue/ignore warning) MUST be evaluated and processed BEFORE passing user inputs to standard WRITE extractor.

---

## 3. Confidence & Validation Security

### 3.1 Confidence Score Requirements
- Extracted slot candidates MUST include a numeric `confidence` score in `[0.0, 1.0]`.
- Candidates with **missing confidence**, **confidence < 0.6**, or **NaN / Inf confidence** MUST be rejected.

### 3.2 Candidate Validation Bypass Prevention
- Extracted slot candidates CANNOT bypass `Validator` rules.
- Invalid or out-of-bound values trigger `blocked_hard` or `blocked_soft` states regardless of confidence.

---

## 4. Snapshot Persistence & Rollback Rules

### 4.1 Atomic Publishing & Locking
- Publishing requires acquiring the cross-process lock (`TaskPublishLock`).
- The builder writes and validates a temporary JSON file, then uses a no-overwrite hard-link commit (`os.link`) to create the final file. It does not publish through a symlink.
- The task directory is resolved by `SEAGENT_TASK_DIR` when set, otherwise by `SEAGENT_RESULT_DIR/task`; the default result root may fall back to the repository `result/` directory when the production default is not writable.

### 4.2 Failure and Uncertain-Commit Semantics
- Before the final file is committed, a `TaskPersistenceError` must restore the in-memory transaction and retain any uncertain staging evidence safely.
- If the final file has already been linked but a later durability step fails, the system returns `TaskCommitUncertainError`, preserves the exact published intent, and requires a later confirmation/reconciliation. It must not report ordinary publication success or allocate a replacement ID.

### 4.3 Terminal Task Protection
- A `done` task cannot be modified in place. Read-only conversation remains available; a new task must use the new-task/reset workflow, preserving the existing published file.
- Repeated confirmation does not create a second file. An uncertain commit is reconciled against its original intent ID.

---

## 5. Equipment Model Resolution Hierarchy

### 5.1 Resolution Hierarchy
```
User Alias (e.g., "观察级一号机")
  └── Equipment Display Name ("观察级深海机器人75HP-001")
        └── equipment_unit_id ("OBSROV-75-001")
              └── equipment_variant / equipment_type ("观察级深海机器人 75HP")
                    └── equipment_family ("观察级深海机器人")
```

### 5.2 Model Selection & Variant Change Rule
- Changing `equipment_variant` clears only non-compatible legacy `equipment_unit_id` values from `SlotStore`.

### 5.3 Direct Unit ID Selection Rule
- Directly selecting a new `equipment_unit_id` retains the new `equipment_unit_id` value and automatically synchronizes corresponding `equipment_variant` and `equipment_family`.

---

## 6. Oilfield & Payload Disambiguation

### 6.1 Oilfield Disambiguation
- Ambiguous oilfield names (< 75 score or < 8 margin) enter `pending_oilfield` state without mutating standard `oilfield_name` slot.
- Explicit confirmation confirms the top candidate; explicit rejection clears pending candidate.

### 6.2 Targeted Conflict Resolution
- Targeted cancellation of a conflicting payload slot (e.g., `"取消支持船修改"`) clears the conflicting candidate while retaining the original valid slot value.

---

## 7. LLM Semantic Authority — Enumeration & Ordinal Selection (ADR-005 Phase 2)

### 7.1 Visible Ordinal Candidate Selection
Ordinal references (e.g., `"第三个"`, `"选 2"`, `"最后一个"`) may be resolved by the model to a standard candidate value, subject to the following conditions **all** being true:
1. The immediately preceding assistant message **explicitly displayed** a numbered candidate list.
2. The referenced ordinal **uniquely maps** to the model's chosen standard value or an unambiguous alias.
3. The chosen standard value is still present in the target field's current `allowed_values`.
4. No new assistant reply has appeared between the numbered list and the user's ordinal reference.

If **any** condition fails, the provenance check MUST delete the write candidate and enter `CLARIFY`. Hidden backend candidate ordering MUST NOT be used as a selection source.

### 7.2 Enum Disambiguation
- Standard enum values, explicit aliases, and natural-language fuzzy descriptions are resolved in sequence through whitelist + source validation paths.
- Directly naming a candidate, using a natural-language description, or referencing a single unambiguous model recommendation bypasses ordinal gate checks.
- Model MUST return only values from the current `allowed_values`; out-of-domain model output MUST be rejected.

### 7.3 Multi-Candidate Recommendation
- When multiple candidates exist and cannot be reliably distinguished, the system MUST present the candidate list and ask the user for preference.
- The system MUST NOT default to the first list item as a pseudo-intelligent recommendation.

---

## 8. Constraint-Aware Robot Candidate Domain (ADR-008)

### 8.1 Single Authority Entry Point
- `KnowledgeBase.get_feasible_robot_selection_domain()` is the **sole authority** for computing robot candidates. Both `DialogueManager` and `OutputBuilder` MUST consume the same domain result.

### 8.2 Current Filtering and Unimplemented Layers
1. The current entry point matches template `required_capabilities` against family capabilities. Class is grouping metadata; legacy `allowed_robot_classes` is not the hard task-compatibility authority.
2. With the default `purpose="interactive"`, it keeps runtime-unavailable units visible. Other purposes filter units only when the task starts within the next 0–60 minutes, using online, idle and telemetry-validity checks. This candidate-window predicate is distinct from the validator's handling of ongoing tasks.
3. Variants without remaining units are retained with `has_available_units=false`; empty families/classes are pruned while assembling the tree.
4. The entry point does **not** currently filter variants by water depth, payload or seabed, or rank them by soft warnings; `rejected_variants` is returned empty. A separate `VariantEvaluator` exists, but its existence does not establish integration into this path. Full filtering remains an [ADR-008 target](decisions/ADR-008-constraint-aware-robot-selection.md).

Candidate membership therefore does not imply that all task constraints pass. Publication and runtime execution require their own validation.

### 8.3 Three-Segment Decision Rule
After filtering:
- **0 feasible candidates**: Fail closed — block with `NO_FEASIBLE_ROBOT_CANDIDATE`.
- **1 feasible candidate**: Auto-bind with `source="auto"`. Auto-bound values are revocable if conditions change.
- **2+ feasible candidates**: Present list and await user disambiguation.

### 8.4 Auto-Bound Value Revocation
- Auto-bound (`source="auto"`) selections are **not** user preferences. If task conditions change and the candidate domain changes, the auto-bound value and its downstream selections MUST be cleared and re-converged.
- User-explicit selections are retained even if incompatible with current conditions; `Validator` MUST produce a hard constraint block in that case.

### 8.5 Four-Level Relationship Validation
- `Validator` and `SlotStore.load_snapshot()` MUST reuse the same `KnowledgeBase` four-level (Class → Family → Variant → Unit) static relationship check.
- On snapshot restore, missing parent levels are inferred from the Registry for uniquely-determined selections, but no child-level selection is auto-chosen on behalf of the user.

### 8.6 Validation Fallback Prohibition
- Constraint validation failures (`blocked_hard`, `blocked_soft`) MUST NOT fall back to `CLARIFY`. The system MUST return an explicit constraint reason and require the user to correct task parameters.

### 8.7 Telemetry and Dispatch
- C019 is currently a soft warning after 1800 seconds. Runtime availability has a separate age limit and invalid-state checks; see [the architecture overview](architecture/overview.md).
- Future-task archival may defer dynamic telemetry validation. Dispatch uses `purpose="runtime_execution"` and fresh validation; archival alone is not execution authorization.
- Sending, scheduling, uncertain results and unsupported valve withdrawal follow [the execution dispatch contract](execution_dispatch_contract.md).
