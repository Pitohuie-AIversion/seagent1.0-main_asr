---
name: constraint_validation_maintenance
description: Procedures for maintaining, updating, or troubleshooting task safety parameters, environmental geofences, and robot telemetry state validation rules.
---

# Constraint Rules & Safety Validation Maintenance Skill

This skill explains how to modify, add, or troubleshoot the safety parameters, static maps, dynamic states, and validator code that determine if a task can be admitted.

## 1. Modifying Safety Limits (`config/constraints.yaml`)
Validation rules are set in [constraints.yaml](../../../config/constraints.yaml).
- **Hard Constraints**: Rules that must never be broken (e.g. coordinates entering a forbidden area, task depth exceeding ROV's max operating depth). Breaking these transitions the dialogue state to `blocked_hard` and blocks task finalization.
- **Soft Constraints**: Rules that represent warnings or minor risks (e.g. high current velocity or medium turbidity). Breaking these transitions the dialogue state to `blocked_soft`, which prompts the user for acknowledgement before proceeding.

## 2. Maintaining Geofences & Environment Map (`config/oilfield.yaml`, `src/environment_info.py`)
Environmental features checks are queryable via [environment_info.py](../../../src/environment_info.py) based on coordinates:
- **No-Go Zones**: Managed under `forbidden_areas` in [oilfield.yaml](../../../config/oilfield.yaml). If coordinates (`start_point`, `end_point`, `oilfield_coordinates`, or `cable_position`) fall within these bounds, a hard block is flagged.
- **Oilfields & Seabed Types**: Under `oil_fields` in `oilfield.yaml`. Seabed types can be `soft` or `hard`. The validator compares these with the ROV's `forbidden_seabed` parameter in `robot_fleet.yaml`. If matched, a hard violation is raised.
- **Oilfield Depth Semantics**: `water_depth` is the default representative depth used to prefill a task. `maximum_reference_water_depth` is the separately sourced upper bound used by hard constraint C029. Never use an average/default depth as the hard upper bound.
- **DVL Failure Areas**: Under `dvl_bottom_lock_failure_areas` in `oilfield.yaml`. Triggers a soft warning.

## 3. Telemetry State Parameters & Validation (`config/state.yaml`, `src/state_info.py`, `src/validation/validator.py`)
Dynamic robot health metrics are handled in [state_info.py](../../../src/state_info.py) and evaluated in [validator.py](../../../src/validation/validator.py).

### Telemetry / Dynamic checks detail:
- **Survival and Availability**:
  - `overall_status`: If `unavailable`, immediately triggers a hard validation block (**C020**).
  - `survival_status`: If `abnormal`, triggers violation (**C021**).
- **Movement Subsystems**:
  - `thruster_status`: If `abnormal`, triggers violation (**C022**) (not recommended for high-dynamic maneuvers or complex terrain operations).
  - `depth_keeping_status`: If `abnormal`, triggers violation (**C023**) (unstable depth-holding, not recommended for high-precision suspended tasks).
- **Perception Subsystems**:
  - `sonar_status`: If `abnormal`, triggers violation (**C024**) (reduced sonar range, not recommended for sonar-dependent inspections).
  - `vision_status`: If `abnormal`, triggers violation (**C025**) (impaired visual observation, not recommended for visual inspections).
- **Executive Mechanism**:
  - `arm_status` or `end_effector_status`: If either is `abnormal`, triggers violation (**C026**) (impaired mechanical capabilities, not recommended for operations).
- **Communication & Integration**:
  - `acoustic_comms_status` (for AUV) or `tether_connection_status` (for ROV/Trencher): If `abnormal`, triggers warning (**C027**).
- **Sea state & Environmental dynamic indicators**:
  - `turbidity`: If `5 < turbidity <= 10` triggers a soft warnings alert (**C013**). If `turbidity > 10` triggers higher warning alert (**C014**).
  - `current_velocity`: Evaluated against limits: velocity `> 0.5` (**C015**), `> 0.8` (**C016**), and `> 1.2` (**C017**, hard constraint).
  - `obstacle_density`: If `high`, triggers soft alert (**C011**).
  - `mothership_support`: If `weak`, triggers warning (**C012**).
  - `confidence`: If `< 0.5`, triggers low-confidence warning (**C018**).
  - `update_timestamp`: C019 currently triggers a soft expiration warning after 1800 seconds (30 minutes) for immediate tasks. Runtime availability and dispatch checks have separate age/validity gates.

## 4. Main Validator Logic (`src/validation/validator.py`)
[validator.py](../../../src/validation/validator.py) handles the execution loop of all constraints:
- **Immediate vs Future Task check**:
  - [telemetry_gate.py](../../../src/validation/telemetry_gate.py) defaults to a 60-minute future-start window, with a 5-minute past-start tolerance. A task that started earlier but has not ended is also immediate. Missing or invalid start times keep dynamic checks enabled; separate validation still rejects invalid time fields.
  - Future tasks starting more than 60 minutes ahead defer dynamic telemetry checks. This classification window is distinct from telemetry freshness and does not establish automatic future dispatch.
- **Data Freshness Threshold**:
  - C019 checks immediate-task state timestamps against 1800 seconds. Separate runtime availability checks use their own age limit; do not describe all telemetry checks as a single one-hour or 24-hour window.

## 5. Hard Refusal Counter & Automatic Rejections (`src/handlers/constraint_decision.py`)
The constraint handler maintains `_hard_refusal_counts` on the dialogue manager:
- **Counter scope**: The hard-block continuation branch increments counts for active hard constraints when it is reached. This is not a counter for every chat turn; queries and earlier bypass guards can return without reaching it.
- **Automatic Rejection**: If a count reaches `HARD_REFUSAL_LIMIT` (currently **4**, defined in `src/constants.py`), the dialogue phase becomes `rejected`.
- **Warning Threshold**: At 3, the response context type is `hard_final_warning`; the persisted phase remains `blocked_hard`.
- **Counter Reset**: Once a hard violation is successfully corrected (the user changes parameters to satisfy the constraint), the refusal counter for that constraint ID is cleared.

## 6. Robot Candidate Domains and Validation
- `KnowledgeBase.get_feasible_robot_selection_domain()` delegates to [selection_engine.py](../../../src/knowledge/selection_engine.py). It filters robot families by template `required_capabilities` and returns the class → family → model variant → fleet unit hierarchy.
- Interactive collection keeps registered units visible. Non-interactive purposes apply runtime availability filtering when the task starts within the runtime window.
- `OutputBuilder` obtains field candidates through [catalog_resolver.py](../../../src/dispatch/catalog_resolver.py); candidate convergence and slot updates are handled separately by the existing slot/handler chain. Zero candidates block selection, one may converge automatically, and multiple candidates require disambiguation.
- The domain function does not perform general depth, payload or seabed validation, or soft-warning ranking. Those checks and any narrower recommendation evidence must be traced at their actual call sites; a listed candidate is not proof of publication or execution eligibility.

## 7. Validation Fallback Disabled (`src/dialogue_manager.py`)
The previous behavior of falling back to a clarification question when slot validation failed has been **disabled**. Current behavior:
- If a slot value fails schema validation, the dialogue manager does **not** silently re-prompt with a generic clarification.
- Instead, the validation error is surfaced through structured slot schema filtering so the responder can generate a targeted correction message.
- This change prevents ambiguous clarification loops that previously obscured the root validation failure from the user.
