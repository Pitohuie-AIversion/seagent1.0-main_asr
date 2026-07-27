# SEAgent Current Design Contract & Architecture Specification

This document defines the authoritative system design contract for SEAgent (Phase 1.9.4+). All dialogue management, intent routing, slot extraction, validation, and snapshot persistence logic must conform to these rules.

---

## 1. Routing & Interaction Types

### 1.1 Intent Router Dichotomy
The system classifies all natural language user inputs into two primary interaction types:
- **`QUERY`**: Information retrieval, status check, device capability inquiry, knowledge base Q&A, or general conversation.
- **`WRITE`**: Task creation, parameter modification, equipment selection, field confirmation, or cancellation.

### 1.2 Query State Invariance Rule
- A `QUERY` interaction **MUST NOT** mutate `SlotStore`, `task_state`, slot values, or `SlotStore.version`.
- A `QUERY` interaction **MUST NOT** change the current dialogue phase (`phase`).
- `QUERY` interactions bypass `Extractor` field extraction and validation commits.

### 1.3 Write Pipeline
- A `WRITE` interaction routes through `Extractor` for canonical slot extraction, followed by `Validator` constraint checking, `SlotStore` state commit, and optional `TaskIntentBuilder` atomic publication.

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
  └── Equipment Display Name ("观察级深海机器人-001")
        └── equipment_unit_id ("OBSROV--001")
              └── equipment_variant / equipment_type ("观察级深海机器人")
                    └── equipment_family ("观察级深海机器人")
```

### 5.2 Model Selection Rule
- Changing `equipment_variant` or `equipment_unit_id` automatically updates `equipment_family` and clears stale `equipment_unit_id` values from `SlotStore`.

---

## 6. Oilfield & Payload Disambiguation

### 6.1 Oilfield Disambiguation
- Ambiguous oilfield names (< 75 score or < 8 margin) enter `pending_oilfield` state without mutating standard `oilfield_name` slot.
- Explicit confirmation confirms the top candidate; explicit rejection clears pending candidate.

### 6.2 Targeted Conflict Resolution
- Targeted cancellation of a conflicting payload slot (e.g., `"取消支持船修改"`) clears the conflicting candidate while retaining the original valid slot value.
