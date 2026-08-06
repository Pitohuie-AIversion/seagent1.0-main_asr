---
name: distill-accumulation-tests
description: Distill an evolving dialogue or task-agent integration test suite into a portable test contract for a next-generation system. Use when inventorying accumulated regression cases, separating product invariants from legacy fixtures, migrating multi-turn dialogue and constraint tests, strengthening brittle response assertions, building a reusable case catalog, or checking parity among manual test documents, executable runners, and migration artifacts.
---

# Distill Accumulation Tests

Convert a historically accumulated integration suite into a versioned, adapter-driven contract. Preserve observed behavior and safety invariants; do not copy legacy endpoints, entities, coordinates, reply prose, or persistence paths into the new core.

## Required source inspection

Read all executable runners and their direct fixture/meta-tests before proposing a migration. Read manual scenario documents as review evidence, not as executable truth. Read runtime schemas and configuration that give fixture values their meaning.

For this SEAgent baseline, load:

- `references/seagent-suite-map.md` for the distilled 39-case inventory, source hierarchy, and known gaps.
- `references/migration-playbook.md` for the portable contract, assertion rules, isolation protocol, and acceptance gates.
- `references/seagent-case-catalog.json` when exact case-level mapping or automated parity checks are needed.

## Workflow

1. **Freeze the observed baseline.** Record repository revision, dirty files, runner command, service mode, simulated clock, configuration revision, and whether external models or ASR are involved. Never claim a baseline passed without real output.
2. **Inventory every case.** Extract case ID, turns/actions, fixture mutation, expected state transition, output oracle, cleanup behavior, and side effects. Run `scripts/audit_catalog.py` to detect missing or duplicate cases.
3. **Classify the invariant.** Assign each case to one or more layers: routing, extraction, normalization, slot state, dialogue state, constraints, telemetry, knowledge, security, publication, or recovery.
4. **Separate invariant from adapter data.** Keep safety outcome, state transition, normalized schema, and artifact semantics in the contract. Move endpoint URLs, API field names, entity aliases, coordinates, timestamps, and response wording into a version-specific adapter or fixture pack.
5. **Strengthen the oracle.** Prefer structured codes, phase, missing-slot sets, normalized fields, `done`, artifact identity, and unchanged file sets. Use semantic text matching only when the API exposes no structured equivalent; mark it as migration debt.
6. **Port in risk order.** Port ordinary dialogue and input routing first, then multi-turn state, hard/soft constraints, warning acknowledgement, publication, idempotency, recovery, concurrency, and ASR.
7. **Verify in layers.** Run catalog audit, adapter/unit tests, selected integration cases, related module regression, then the full suite. Preserve raw command output and distinguish pass, expected failure, skip, and unexecuted.

## Non-negotiable invariants

- Keep ordinary conversation, ASR text, and structured-task flows distinct and simultaneously usable.
- Give every case a unique session and reset it before and after execution.
- Derive fresh and stale telemetry from one controlled clock; do not depend on wall-clock timing.
- Mutate one target risk axis per constraint case unless the case intentionally tests composition.
- Keep hard blocks non-bypassable. Treat warning acknowledgement separately from publication confirmation.
- Re-evaluate soft constraints after a corrected hard violation.
- Preserve confirmed slot values when a candidate update fails validation.
- Verify publication through schema-valid artifacts and lifecycle state, not only a success sentence.
- Verify duplicate confirmation creates no new artifact and does not rewrite the original.
- Restore mutable fixtures byte-for-byte on success, failure, and exception paths.
- Fail closed on unknown selected case IDs, setup errors, unexpected passes of known regressions, and persistence failures.

## Adapter boundary for the next generation

Implement these operations behind the new system's native test client:

```text
set_clock(instant)
new_session(case_id) -> session_id
reset_session(session_id)
inject_state(state_ref, values)
send_text(session_id, text) -> structured_response
send_asr(session_id, audio_fixture) -> transcript + structured_response
read_artifacts(intent_id) -> task + history + metadata
snapshot_mutable_fixtures() / restore_mutable_fixtures()
```

Map the response into a stable observation model containing at least `route`, `phase`, `task_type`, `slots`, `missing`, `violations`, `done`, and `final_artifact`. If the new API lacks one field, document the fallback oracle rather than silently weakening the test.

## Use the bundled assets

- Copy `assets/case-catalog.template.json` to start a new catalog; replace all example values.
- Copy `assets/migration-evidence.template.md` for each migration batch and fill it with actual commands and raw result summaries.
- Do not treat an asset template as evidence until it has been adapted and executed.

## Audit commands

Audit only the catalog:

```bash
python scripts/audit_catalog.py references/seagent-case-catalog.json
```

Check parity with the current SEAgent runner and manual document:

```bash
python scripts/audit_catalog.py references/seagent-case-catalog.json \
  --runner tests/run_accumulation_integration_tests.py \
  --manual-doc tests/test_accumulation/测试集05.md
```

Use the target project's required interpreter and test commands when migrating. Keep the audit independent of network services.

## Completion gate

Declare the migration complete only when:

- every legacy case is mapped as `verified`, `adapted`, or explicitly `deferred` with a reason;
- all P0/P1 invariants have structured or artifact-level oracles;
- ordinary dialogue, ASR, hard-block bypass, warning acknowledgement, cancellation, recheck, publication, duplicate publication, crash recovery, and concurrent persistence are covered;
- fixture cleanup and deterministic time are verified by tests;
- selected and full test commands have real recorded results;
- no target test depends on obsolete SEAgent entity values unless those values are intentionally retained as compatibility fixtures.
