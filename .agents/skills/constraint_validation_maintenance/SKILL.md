---
name: constraint_validation_maintenance
description: Procedures for maintaining, updating, or troubleshooting task safety parameters, environmental geofences, and robot telemetry state validation rules.
---

# Constraint Rules & Safety Validation Maintenance Skill

This skill explains how to modify, add, or troubleshoot the safety parameters, static maps, dynamic states, and validator code that determine if a task can be admitted.

## 1. Modifying Safety Limits (`config/constraints.yaml`)
Validation rules are set in [constraints.yaml](file:///root/mzy/seagent1.0-main_asr/config/constraints.yaml).
- **Hard Constraints**: Rules that must never be broken (e.g. coordinates entering a forbidden area, task depth exceeding ROV's max operating depth). Breaking these transitions the dialogue state to `blocked_hard` and blocks task finalization.
- **Soft Constraints**: Rules that represent warnings or minor risks (e.g. high current velocity or medium turbidity). Breaking these transitions the dialogue state to `blocked_soft`, which prompts the user for acknowledgement before proceeding.

## 2. Maintaining Geofences & Environment Map (`config/environment.yaml`, `src/environment_info.py`)
Environmental features checks are queryable via [environment_info.py](file:///root/mzy/seagent1.0-main_asr/src/environment_info.py) based on coordinates:
- **No-Go Zones**: Managed under `exclusion_zones` in [environment.yaml](file:///root/mzy/seagent1.0-main_asr/config/environment.yaml). If coordinates (`start_point`, `end_point`, `oilfield_coordinates`, or `cable_position`) fall within these bounds, a hard block is flagged.
- **Oilfields & Seabed Types**: Under `oil_fields` in `environment.yaml`. Seabed types can be `soft` or `hard`. The validator compares these with the ROV's `forbidden_seabed` parameter in `robot_fleet.yaml`. If matched, a hard violation is raised.
- **DVL Failure Areas**: Under `dvl_bottom_lock_failure_areas` in `environment.yaml`. Triggers a soft warning.

## 3. Telemetry State Parameters & Validation (`config/state.yaml`, `src/state_info.py`, `src/validator.py`)
Dynamic robot health metrics are handled in [state_info.py](file:///root/mzy/seagent1.0-main_asr/src/state_info.py) and evaluated in [validator.py](file:///root/mzy/seagent1.0-main_asr/src/validator.py).

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
  - `update_timestamp`: If older than 1 hour relative to simulated time, triggers expiration warning (**C019**).

## 4. Main Validator Logic (`src/validator.py`)
[validator.py](file:///root/mzy/seagent1.0-main_asr/src/validator.py) handles the execution loop of all constraints:
- **Immediate vs Future Task check**:
  - Immediate task (starts within 10 minutes of simulated time): The validator executes full checks (hard parameters + static environment + dynamic robot states).
  - Future task (starts > 10 minutes from simulated time): The validator only checks static hard parameters and static environment limits; it bypasses active robot state checks.
- **Data Freshness Threshold**:
  - Environment/state updates must have an `update_timestamp` within 1 hour of the simulated time. If the data is older, the validator raises an expiration error and blocks the task.

## 5. Hard Refusal Counter & Automatic Rejections (`src/dialogue_manager.py`)
Dialogue states track consecutive hard validation failures:
- **Consecutive turn limit**: The dialogue manager counts how many turns a hard constraint violation remains active without correction (`self._hard_refusal_counts`).
- **Automatic Rejection**: If the failure count for any hard constraint reaches **4 turns**, the dialogue phase is set to `rejected` and the task fails.
- **Warning Threshold**: At 3 consecutive turns, the system transitions to a final warning state (`hard_final_warning`) to notify the user.
- **Counter Reset**: Once a hard violation is successfully corrected (the user changes parameters to satisfy the constraint), the refusal counter for that constraint ID is cleared.
