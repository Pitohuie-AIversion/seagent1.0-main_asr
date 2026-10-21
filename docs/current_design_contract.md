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
- **Immediate Task Start Grounding**: Explicit expressions requesting immediate startup (e.g., `"任务现在开始"`, `"立刻开始"`, `"即刻开始"`) are deterministically anchored to absolute current system time (`get_current_datetime()`) without ambiguity or relative shift errors.
- `CONTROL` is handled by the relevant phase handler for confirmation, cancellation or warning acknowledgement; not every control action runs the extractor.
- A model returning `WRITE` does not guarantee a field commit or publication.


---

## 2. Control Commands & Negation Syntax

### 2.1 Control Intent Definitions
- **Publish (`TASK_CONFIRM` / `"确认发布"`):** In `confirming`, explicit publication intent triggers TaskIntent persistence and history snapshot handling. A generic `"确认"` alone requests final publication confirmation; it does not itself publish.
- **Cancel (`TASK_CANCEL` / `"取消任务"`, `"放弃"`):** During collection, resets current task state and sets phase to `rejected`. Archived tasks retain their immutable plan and use persistent execution controls; see [the execution dispatch contract](execution_dispatch_contract.md).
- **Accept warning (`"忽略警告"`, `"接受风险"`):** Explicit acceptance in `blocked_soft` records acknowledgement against the current validation context and proceeds after revalidation. `"继续"` alone is not explicit risk acceptance, and acceptance is separate from publication.

### 2.2 Negation Handling Rules
- **Cancel Negation (`"不要取消"`, `"别取消"`, `"不取消"`):** MUST NOT trigger task cancellation or phase transition to `rejected`.
- **Publish Negation (`"不要发布"`, `"不发布"`):** MUST NOT trigger task publication in `confirming` phase.
- **Confirmation Negation (`"不确认"`):** MUST NOT confirm task or publish intent JSON.

### 2.3 Control Command Processing Order
- Control commands (e.g., confirm, cancel, continue/ignore warning) MUST be evaluated and processed BEFORE passing user inputs to standard WRITE extractor.

### 2.4 Explicit Edit Restrictions (2026-10-19)
- A standalone task-wide restriction such as `"不要修改任务"`, `"先别修改"` or `"只是讨论"` applies across the current message. Before processing model-proposed writes or pending selections, the manager preserves task fields, slot metadata, validation state and phase; its reply uses saved constraint context without revalidation. Emergency controls retain their existing route.
- Domain-wide restrictions such as `"不要改任何时间"` or `"工具不要改"` suppress that domain's model candidates and deterministic recovery, while independent changes to other fields remain available. `"不要修改任务编号"` and `"先不发布"` are not task-wide edit restrictions.
- Discussion or negation limited to one operation does not suppress a separate affirmative operation: `"只是讨论删除A，新增B"` may add B while retaining A. Time recovery applies the same nonassertive-context checks to explicit endpoints, ranges and duration relations.
- These guards cover the explicit expressions tested in the [October 19 verification record](progress/2026-10-19-whole-turn-protection-fixes.md). They do not establish complete natural-language negation coverage.

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
4. The entry point filters variants by confirmed water depth and payload through `VariantEvaluator`, records reasons in `rejected_variants`, and reports payload installation requirements. Family and model choices share this feasible domain. The condition-free admission domain remains available to preserve explicit user choices for specific validation errors. Seabed filtering and soft-warning ranking remain [ADR-008 targets](decisions/ADR-008-constraint-aware-robot-selection.md).

Candidate membership therefore does not imply that all task constraints pass. Publication and runtime execution require their own validation.

### 8.3 Three-Segment Decision Rule
After filtering:
- **0 feasible candidates**: Fail closed — block with `NO_FEASIBLE_ROBOT_CANDIDATE`.
- **1 feasible candidate**: Auto-bind with `source="auto"`. Auto-bound values are revocable if conditions change.
- **2+ feasible candidates**: Present list and await user disambiguation.

### 8.4 Auto-Bound Value Revocation
- Auto-bound (`source="auto"`) selections are **not** user preferences. If task conditions change and the candidate domain changes, the auto-bound value and its downstream selections MUST be cleared and re-converged.
- Payload edits evaluate against explicit equipment choices, then recompute the automatic suffix only after successful updates. Apply scalar, list and explicit equipment updates before candidate convergence so old payloads cannot constrain their replacements.
- User-explicit selections are retained even if incompatible with current conditions; `Validator` MUST produce a hard constraint block in that case.

### 8.5 Four-Level Relationship Validation
- `Validator` and `SlotStore.load_snapshot()` MUST reuse the same `KnowledgeBase` four-level (Class → Family → Variant → Unit) static relationship check.
- On snapshot restore, missing parent levels are inferred from the Registry for uniquely-determined selections, but no child-level selection is auto-chosen on behalf of the user.

### 8.6 Validation Fallback Prohibition
- Constraint validation failures (`blocked_hard`, `blocked_soft`) MUST NOT fall back to `CLARIFY`. The system MUST return an explicit constraint reason and require the user to correct task parameters.

### 8.7 Telemetry and Dispatch
- C019 is currently a soft warning after 1800 seconds. Runtime availability has a separate age limit and invalid-state checks; see [the architecture overview](architecture/overview.md).
- Future-task archival may defer dynamic telemetry validation. Dispatch uses `purpose="runtime_execution"` and fresh validation; archival alone is not execution authorization.
- Archived tasks may acknowledge a new runtime soft warning against its displayed evidence, then dispatch separately. Evidence changes require another review; hard violations remain blocking and archived parameters remain unchanged.
- Sending, scheduling, uncertain results and unsupported valve withdrawal follow [the execution dispatch contract](execution_dispatch_contract.md).

## 9. HTTP Retry and Streaming Boundaries

### 9.1 Recent Request Replay

- `/api/chat` and `/api/chat/stream` share the same replay records within a live session manager. An explicit `request_id` plus the same trimmed message replays the original outcome without re-running extraction, mutation, publication or dispatch. Concurrent duplicates serialize behind the same session lock.
- Reusing a retained ID with different message content returns `RequestIdConflict` (409; an SSE `error` event for the streaming transport). The first result is not overwritten. IDs are scoped to the session, not globally.
- Records last at most 10 minutes from completion, with at most 128 entries and a 4 MiB serialized-response budget per manager; older records may be evicted earlier by either budget. Oversized responses retain a bounded tombstone returning `RequestReplayUnavailable` instead of executing again while that record remains.
- This is process-local, bounded retry protection. Configuration migration preserves the replay cache; reset, history forks, process restart, expiry or eviction end that protection. It is not durable exactly-once processing. Clients must check current session state before retrying an old or uncertain incremental command.
- Publication and runtime risk acceptance also require the current `expected_confirmation_token`. Successful request replay precedes that check. Stale confirmation, history forks and configuration migration follow [the session confirmation and recovery contract](architecture/session-confirmation-recovery.md).
- Errors reached during processing are also replayed because an error may follow a state mutation. To intentionally attempt an operation again, inspect current state first and use a new request ID; the `retryable` flag does not invalidate a retained outcome. Input validation and streaming-capacity rejection occur before processing and are not recorded as completed turns.
- Replayed responses contain the original snapshot. They do not revert newer session state; use `/api/session/state` for the latest state. A missing request ID creates a new logical turn. The browser supplies one ID per send and keeps it across its supported transport fallback; interrupted streams are still not automatically resent.

### 9.2 Live Progress and Committed Results

- An accepted stream starts an independent worker and immediately emits `ping` and `queued` progress, then live `step` events and a heartbeat while waiting. These events do not imply that a field is saved or a task published.
- Slot/warning events, reply `delta`, final `result` and `end` wait until the session transaction, persistence/dispatch handling and response serialization finish. Reply deltas remain chunks of the validated final reply, not unvalidated model token streaming.
- A disconnected observer stops receiving events but does not cancel an accepted transaction. Reset continues to acquire the session lock before removing/resetting the manager.
- Each process allows at most 8 active streaming workers, with a 64-event advisory progress buffer per worker. Saturation returns HTTP 503 `ServerBusy` with `Retry-After: 1`; a slow/disconnected reader cannot block the worker by filling the progress queue. This worker bound does not remove the shared model lock or bound the ordinary JSON endpoint's request queue.
- Invalid transport input still returns HTTP 400 before streaming. Processing failures after stream acceptance are represented by an SSE `error` event carrying the application code, without a success `result`.

### 9.3 State Reads Concurrent with Reset

- `/api/session/state` rechecks manager identity after acquiring the session lock. If reset removed or replaced that manager while the read waited, the endpoint returns HTTP 409 `SessionReset` with `retryable=true`; it neither exposes nor refreshes the detached manager.
- A subsequent read observes the current manager or `exists=false`. Successful response fields remain unchanged. This check does not make reset interrupt in-progress generation; reset still waits for the session lock.
