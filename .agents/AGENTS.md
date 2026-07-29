# AGENTS.md

## 1. Project Overview

This repository contains **SEAgent**, a task-intelligence layer positioned between human task requirements and robot control systems.

Repository:

```text
Pitohuie-AIversion/seagent1.0-main_asr
```

SEAgent converts text or speech input into structured, validated, reviewable, persistent, and eventually executable robot tasks.

Current major components include:

- Web frontend;
- Backend API;
- Ordinary LLM conversation;
- ASR speech transcription;
- Intent routing;
- `DialogueManager`;
- Slot collection and validation;
- Soft and hard constraint handling;
- Task confirmation and modification;
- Task staging, snapshot, publication, and recovery;
- Future robot execution adapters.

The current priority is to stabilize the existing software workflow before adding large new capabilities.

---

## 2. Core Objective

All work should improve the system's:

- Correctness;
- Reliability;
- Testability;
- Recoverability;
- Concurrency safety;
- Maintainability;
- Compatibility;
- Observability.

Do not prioritize feature quantity over system stability.

Do not begin large-scale world-model, task-graph, multi-robot allocation, or dynamic replanning work while unresolved P0 or P1 defects remain.

---

## 3. Source of Truth

Use evidence in the following order:

1. Current repository code;
2. Current `git diff`;
3. Existing tests;
4. Actual test output;
5. Runtime logs and tracebacks;
6. API and persistence schemas;
7. README and design documents;
8. Developer summaries.

Code and test evidence override outdated documentation.

Never claim that something is fixed, tested, committed, pushed, merged, or deployed without verifying it.

---

## 4. Required Working Method

Before modifying code:

1. Inspect the relevant entry point.
2. Trace the complete call chain.
3. Identify state transitions.
4. Identify data mutations.
5. Identify persistence operations.
6. Inspect existing tests.
7. Reproduce or understand the failure.
8. Confirm the root cause.
9. Design the smallest safe change.
10. Implement only after the above steps.

Do not patch only the visible symptom.

Do not refactor unrelated modules during a focused bug fix.

---

## 5. Development Priority

Work in this order:

1. Preserve ordinary LLM conversation.
2. Preserve ASR transcription and routing.
3. Stabilize `DialogueManager`.
4. Stabilize Slot schema and Slot transitions.
5. Correct soft-constraint and hard-constraint behavior.
6. Stabilize task staging, publication, locking, and recovery.
7. Stabilize frontend/backend API contracts.
8. Complete regression testing.
9. Improve logging and maintainability.
10. Add world-model and multi-robot capabilities later.

---

## 6. Ordinary Conversation and Task Mode

The application must support both:

- Ordinary LLM conversation;
- Structured task-building conversation.

ASR-transcribed text should enter the same logical routing layer as typed text unless the architecture explicitly requires otherwise.

Inputs that are not clear task requests must not be forced into:

- Slot collection;
- Constraint checking;
- Publish confirmation;
- Task-intent construction.

Any routing change must include regression tests for both task input and non-task input.

Task-mode improvements must not break ordinary LLM conversation.

---

## 7. DialogueManager Rules

Treat `DialogueManager` as an explicit state machine, not as a collection of unrelated keyword checks.

When modifying `src/dialogue_manager.py`, identify:

- The phase before input handling;
- The user event;
- The transition condition;
- The phase after processing;
- Whether `old_phase` must be preserved;
- Whether phase mutation occurs before a phase-dependent decision;
- Whether multiple handlers can process the same input;
- Whether an exception leaves state inconsistent.

The following meanings must remain distinct:

- Confirm task content;
- Confirm final publication;
- Modify task;
- Reject task;
- Cancel task;
- Ignore a soft warning;
- Resolve a hard constraint;
- Continue ordinary conversation.

### Soft-warning behavior

A soft warning may be explicitly ignored by the user.

Requirements:

- The user action must be unambiguous.
- The ignore action should be recorded where appropriate.
- Ignoring a warning must not corrupt task state.
- Soft-warning continuation must not weaken hard constraints.

### Hard-constraint behavior

A hard constraint must block publication.

It must not be bypassed by inputs such as:

- `确认`;
- `继续`;
- `忽略警告`;
- `没问题`;
- `OK`.

The workflow may continue only after the hard-constraint violation is corrected.

### Phase-handling requirement

Do not rely on `self.phase` after it has already been reset or reassigned when the decision depends on the previous phase.

Preserve the previous state with a value such as `old_phase` where required.

Every critical transition should have:

- Positive tests;
- Negative tests;
- Boundary tests;
- Regression tests.

---

## 8. Slot Model Rules

The Slot model may contain fields such as:

- `value`;
- `value_type`;
- `status`;
- `source`;
- `raw_value`;
- `confidence`;
- `validation_error`;
- `updated_at`;
- `version`;
- `candidate_value`.

When changing Slot behavior, verify that:

- Raw input and normalized values are not confused;
- Candidate values are not promoted before validation;
- Validation failure does not overwrite a confirmed value;
- Version increments are consistent;
- Serialization and deserialization are symmetric;
- Snapshot restoration preserves state;
- Old snapshots are migrated or explicitly rejected;
- Missing fields receive safe defaults when compatibility is required.

Do not silently discard confirmed values or Slot history.

Do not change a persisted Slot schema without compatibility tests.

---

## 9. Constraint Validation Rules

Soft constraints and hard constraints must be represented and handled separately.

Constraint-processing code should provide enough structured information to determine:

- Constraint type;
- Constraint cause;
- Affected Slot or task field;
- Whether correction is required;
- Whether user override is allowed;
- What state should follow.

A validation failure must not be converted into success merely to allow publication.

### Soft constraints

- May produce a warning;
- May allow explicit user override;
- Must preserve task consistency;
- Must not interpret ambiguous input as an override.

### Hard constraints

- Must block publication;
- Must provide a clear reason;
- Must preserve the task context for correction;
- Must not be bypassed by generic confirmation.

---

## 10. Persistence and File Safety

Changes involving `src/task_intent_builder.py` or task persistence require special care.

Relevant artifacts may include:

- `staging`;
- `snapshot`;
- `final`;
- `claim`;
- `temp`;
- `quarantine`.

Required properties:

- Atomic publication;
- Cross-process synchronization;
- Recoverability;
- Schema validation;
- Fail-closed behavior;
- Safe retention of uncertain files.

### Required rules

- Write to a temporary file before atomic replacement.
- Flush and `fsync` file contents where durability is required.
- `fsync` the parent directory where required for durable rename operations.
- Use the project lock consistently across publish, load, and recovery operations.
- Do not use unsafe `stat -> unlink` sequences.
- Do not delete paths inferred only from filename guesses.
- Do not delete files when transaction ownership is uncertain.
- Retain uncertain or replaced files in quarantine.
- Do not report publication success after a persistence failure.
- Do not remove staging files outside the required lock.
- Validate snapshot schema before restoring runtime state.
- Handle an existing final artifact deterministically.
- Make repeated publication idempotent or explicitly reject it.

### Risks to inspect

- TOCTOU races;
- Concurrent publication;
- Duplicate publication;
- Crash during rename;
- Crash before directory synchronization;
- Staging/final conflicts;
- Lock-scope gaps;
- Exception-path deletion;
- Invalid or partial snapshots;
- Interrupted-transaction recovery.

Persistence failures must fail closed.

---

## 11. API and Frontend Contracts

Before changing an API response, request schema, streaming format, error shape, or status code:

1. Find every frontend and backend consumer.
2. Inspect existing tests.
3. Preserve compatibility or update all consumers in the same change.
4. Add contract tests.
5. Verify ordinary conversation and task mode separately.

Do not return task-specific structures to ordinary-chat consumers unless the frontend explicitly supports them.

Do not hide backend failures behind successful HTTP or application responses.

---

## 12. Error Handling and Logging

Handle errors at the correct abstraction level.

Avoid suppressing failures with code such as:

```python
try:
    ...
except Exception:
    pass
```

When broad exception handling is necessary:

- Preserve the original exception;
- Add relevant context;
- Log at the appropriate level;
- Preserve or restore consistent state;
- Re-raise or return a typed failure when required.

Useful log context may include:

- Request or transaction identifier;
- Dialogue phase;
- Operation name;
- File role;
- Validation type;
- Recovery action.

Do not log:

- API tokens;
- Credentials;
- Full sensitive payloads;
- Unnecessary user data.

---

## 13. Testing Requirements

Every behavior change requires tests.

Run tests in the following order.

### Level 1: Targeted tests

Run the exact test related to the change.

Example:

```bash
pytest path/to/test_file.py::test_specific_case -q
```

Use real repository paths and test names. Do not invent them.

### Level 2: Related module tests

Run tests for affected modules, such as:

- `DialogueManager`;
- Slot Store;
- Constraint validation;
- Task Intent Builder;
- Persistence;
- API;
- ASR;
- Frontend integration.

### Level 3: Full regression suite

Run the repository's actual full-suite command.

A common command may be:

```bash
pytest -q
```

Verify the project configuration before assuming this is correct.

### Required test report

Report:

- Exact command;
- Passed count;
- Failed count;
- Skipped count;
- Warnings;
- Timeouts;
- Hangs;
- Test duration when available.

A passing targeted test is not enough for phase acceptance.

---

## 14. Required Regression Scenarios

Maintain or add tests for:

- Ordinary LLM conversation;
- ASR transcription routing;
- Clear task input;
- Non-task input;
- Missing Slot;
- Slot correction;
- Invalid candidate value;
- Confirmed Slot preservation;
- User confirmation;
- User rejection;
- User cancellation;
- Soft warning;
- Explicit soft-warning ignore;
- Ambiguous warning response;
- Hard-constraint blocking;
- Attempted hard-constraint bypass;
- Duplicate confirmation;
- Duplicate publication;
- Existing staging artifact;
- Existing final artifact;
- Invalid snapshot;
- Interrupted publication;
- Recovery after interruption;
- Concurrent publication;
- File-lock behavior;
- Frontend/backend compatibility.

---

## 15. Prohibited Solutions

Do not:

- Add test-only branches to production code;
- Hardcode output for one failing case;
- Delete or weaken assertions;
- Skip failing tests without an approved reason;
- Weaken hard constraints;
- Convert persistence failures into successful responses;
- Catch and suppress unknown exceptions;
- Delete uncertain files;
- Change persistence schemas without compatibility tests;
- Perform broad unrelated refactoring during a bug fix;
- Add dependencies without demonstrating necessity;
- Claim test success without running tests;
- Claim commit or push completion without checking Git state.

---

## 16. Code Quality

Prefer:

- Explicit state transitions;
- Typed data structures;
- Small functions with clear responsibilities;
- Deterministic behavior;
- Structured errors;
- Clear invariants;
- Focused tests;
- Backward-compatible changes;
- Minimal diffs.

Avoid:

- Hidden global state;
- Implicit phase mutation;
- Duplicate keyword logic;
- Boolean expressions with unclear semantics;
- Functions mixing parsing, validation, persistence, and presentation;
- Silent fallback behavior;
- Unbounded retries.

Comments should explain why a non-obvious decision exists, not merely repeat the code.

---

## 17. Severity Levels

### P0

- Data loss;
- Unsafe file deletion;
- Security issue;
- Incorrect final task publication;
- Hard-constraint bypass;
- Core service unavailable;
- Irrecoverable persistence corruption.

### P1

- Main workflow failure;
- Ordinary LLM regression;
- ASR routing failure;
- Incorrect state transition;
- API contract break;
- Duplicate publication;
- Concurrency race;
- Incorrect confirmation behavior.

### P2

- Missing boundary handling;
- Incomplete tests;
- Compatibility risk;
- Weak logging;
- Recovery gap;
- Maintainability problem.

### P3

- Naming;
- Formatting;
- Minor duplication;
- Comments;
- Non-critical documentation.

Unresolved P0 or P1 findings block phase acceptance.

---

## 18. Review Output Format

When reviewing code, use the following structure.

### Conclusion

Use one of:

- `PASS`;
- `PASS WITH CONDITIONS`;
- `FAIL`;
- `BLOCKED`.

### Review Scope

Include:

- Branch, commit, PR, or diff;
- Files inspected;
- Tests inspected or executed;
- Areas not inspected.

### Confirmed Correct

Only include statements supported by direct code or test evidence.

### Findings

For every finding include:

- Severity;
- File;
- Function or class;
- Trigger condition;
- Actual behavior;
- Expected behavior;
- Root cause;
- Recommended correction;
- Required test.

### Regression Risks

Identify adjacent modules and behavior that may be affected.

### Verification

List exact commands and results.

### Acceptance Criteria

State what must be true before the change or phase can be accepted.

---

## 19. Implementation Report

After implementing a change, report:

1. Root cause;
2. Modified files;
3. Key implementation decisions;
4. Public-interface changes;
5. Schema changes;
6. Tests added or modified;
7. Targeted test command and result;
8. Related test command and result;
9. Full-suite command and result;
10. Skipped tests and warnings;
11. Remaining risks;
12. `git status`;
13. Commit hash, only if committed;
14. Push status, only if pushed;
15. Whether acceptance criteria were met.

Do not replace evidence with statements such as “should work”.

---

## 20. Git Requirements

Before committing, inspect:

```bash
git status
git diff
git diff --stat
git diff --check
```

Before completing the task, confirm:

- No temporary files;
- No debug output;
- No local absolute paths;
- No credentials or tokens;
- No unrelated modifications;
- Required tests were run;
- Commit message matches the change.

Each commit should have one clear purpose.

Do not mix unrelated:

- Features;
- Bug fixes;
- Formatting;
- Refactoring;
- Dependency upgrades.

---

## 21. Architecture Documentation

Architecture diagrams and Mermaid flows must reflect actual code.

Clearly label modules as:

- Implemented;
- Partially implemented;
- Planned;
- Defective;
- Unknown.

At minimum, distinguish:

- Frontend;
- Backend API;
- ASR;
- LLM;
- Intent router;
- `DialogueManager`;
- Slot Store;
- Constraint validation;
- Task Intent Builder;
- Persistence;
- Robot execution adapter.

Show ordinary conversation and task-construction flows separately.

Do not present planned functionality as already implemented.

---

## 22. Project Progress Reporting

Project status reports must separate:

- Implemented and verified;
- Implemented but not fully verified;
- Partially implemented;
- Known defects;
- Technical debt;
- Planned;
- Not started.

Use this roadmap structure:

### Now

Tasks required to remove blockers and stabilize the main workflow.

### Next

Tasks for contracts, tests, maintainability, and integration after the main workflow is stable.

### Later

Future capabilities such as:

- World models;
- Task graphs;
- Multi-robot allocation;
- Weak-communication coordination;
- Dynamic replanning;
- Execution-feedback loops.

Every roadmap task should include:

- Objective;
- Affected modules;
- Prerequisites;
- Risks;
- Acceptance criteria.

---

## 23. Definition of Done

A change is complete only when:

- The root cause is identified;
- The implementation addresses the root cause;
- Unrelated behavior is preserved;
- Required tests are added;
- Targeted tests pass;
- Related module tests pass;
- The full suite passes, or limitations are explicitly documented;
- No unresolved P0 or P1 regression is introduced;
- Git diff is focused and clean;
- Documentation is updated where required;
- The final report contains real evidence.

A merged commit alone does not prove completion.

A passing targeted test alone does not prove phase acceptance.