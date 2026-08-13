# SEAgent Current Design Contract & Architecture Specification

This document defines the authoritative system design contract for SEAgent (Phase 2, commit 8254b37, 2026-08-13). All dialogue management, intent routing, slot extraction, validation, snapshot persistence, and robot candidate selection logic must conform to these rules.

---

## 1. Routing & Interaction Types

### 1.1 InteractionPlan Semantic Authority (ADR-005)
- Every turn, the LLM produces a structured `InteractionPlan` with an `operation` field: `READ`, `WRITE`, `CONTROL`, or `CLARIFY`.
- `operation` is the **sole routing authority**. Backend deterministic code MUST NOT override the operation type based on keyword heuristics.
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
- A `WRITE` / `CONTROL` interaction routes through `Extractor` for canonical slot extraction, followed by `Validator` constraint checking, `SlotStore` state commit, and optional `TaskIntentBuilder` atomic publication.
- Model returning `WRITE` does not guarantee write success — the result still passes through Extractor → Validator → SlotStore.

---

## 2. Control Commands & Negation Syntax

### 2.1 Control Intent Definitions
- **Confirm (`TASK_CONFIRM` / `"确认发布"`, `"确认"`):** When phase is `confirming`, triggers atomic persistence of finalized `TaskIntent_TI*.json` and history snapshot.
- **Cancel (`TASK_CANCEL` / `"取消任务"`, `"放弃"`):** Resets current task state and sets phase to `rejected`.
- **Continue / Ignore Warning (`"继续"`, `"忽略预警"`):** Overrides `blocked_soft` warnings and proceeds to next state.

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
- Publishing requires acquiring an atomic process lock (`TaskPublishLock`).
- Staging files (`TI*_staging.json`) are validated before atomic symlink/atomic rename publishing to final destination `/root/autodl-tmp/result/task`.

### 4.2 Failure Rollback Guarantee
- If a persistence or symlink error occurs during publishing (`TaskPersistenceError`), the system MUST roll back `SlotStore`, `task_state`, and disk artifacts to their pre-transaction snapshot.

### 4.3 Intent ID Inheritance & Invalidation
- Modifying a slot in `done` phase invalidates the published intent and generates a new daily sequence `intent_id`.

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

### 8.2 Filtering Layers
The candidate tree is pruned in the following order:
1. `allowed_robot_classes` and `required_capabilities` (task type gate).
2. Confirmed `water_depth` vs. `Variant.hard_params.max_depth_m` (strict `<=` semantics).
3. Confirmed `payload` vs. `onboard_payloads ∪ supported_payloads` of each Variant.
4. For immediate tasks (confirmed `start_time` within current 10-minute window): filter Units by online, idle, and telemetry validity.
5. Prune empty Variants, Families, and Classes after filtering.

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
