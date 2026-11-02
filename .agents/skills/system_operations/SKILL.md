---
name: system_operations
description: Procedures for executing unit tests, starting the local dialogue server, simulating robot telemetry updates, and verifying system artifacts.
---

# System Operations & Verification Skill

This skill explains how to build, test, run, and update the subsea dialog application locally.

## 1. Environment & Running Tests
* **Python Environment**: Use the environment with this checkout's dependencies installed. The local server commonly uses `/root/miniconda3/envs/seagent/bin/python`; verify availability rather than assuming every checkout has that path.
* **Testing Command**: The project uses **pytest** as its primary test runner. Run targeted and related checks for behavior changes, then the full suite for acceptance; report incomplete or unavailable runs explicitly. Documentation-only changes use reference and diff checks unless they also change behavior:
  ```bash
  python -m pytest -q
  ```
  For a targeted single test:
  ```bash
  python -m pytest path/to/test_file.py::test_specific_case -q
  ```
  > [!NOTE]
  > The legacy `python -m unittest discover tests` invocation is no longer the canonical entry point. Use `pytest -q` instead. The collected test count changes as suites grow; verify the actual count in the run output.
* **Offline Execution**: Ensure the model loading remains offline. Keep the environment variables `TRANSFORMERS_OFFLINE=1` and `HF_HUB_OFFLINE=1` active.

## 2. Running the Server & Port Forwarding
To start the Flask dialogue server and local models:
1. Activate the project Python environment and run from the repository root.
2. Launch the backend. For interface-only work without local models, use explicit mock mode:
   ```bash
   OFFLINE_MOCK=1 ENABLE_MCP=0 python run.py
   ```
   Real mode requires the local Qwen model, GPU dependencies, and the ASR model configured in `config/asr.yaml`.
3. To forward the local port for external Web access, run:
   ```bash
   python port_forward.py
   ```
4. The server runs at `http://localhost:8890`. You can query chat operations, set simulated states, or access dialogue history through the backend endpoints.

## 3. Simulating Robot Telemetry Updates
If you need to update or reset the robot's real-time parameters (e.g. current velocity, turbidity, health status):
Send a POST request to the `/api/robot/set-state-info` endpoint.
Example payload:
```shell
curl -X POST http://localhost:8890/api/robot/set-state-info \
  -H "Content-Type: application/json" \
  -d '{
    "robot_name": "OBSROV-75-001",
    "params": {
      "current_velocity": 0.3,
      "turbidity": 3,
      "obstacle_density": "low",
      "mothership_support": "strong",
      "update_timestamp": "2026-07-10T18:00:00+08:00",
      "confidence": 0.95,
      "overall_status": "available",
      "survival_status": "normal",
      "thruster_status": "normal",
      "depth_keeping_status": "normal",
      "sonar_status": "normal",
      "vision_status": "normal",
      "arm_status": "normal",
      "end_effector_status": "normal",
      "acoustic_comms_status": "normal",
      "tether_connection_status": "normal"
    }
  }'
```
> [!IMPORTANT]
> The `update_timestamp` must match the current simulated time (queried from `src/temporal/simulated_time.py`). C019 currently warns when an immediate-task state timestamp is older than 30 minutes; runtime availability and dispatch perform separate validity checks.

Use `/api/time/current` for the running server's simulated time, and verify the robot's `status_ref` in `config/robot_fleet.yaml`. Manual POSTs mutate persisted state; use a dedicated test instance and restore its initial state afterwards. Automatic fixture restoration only applies when using the corresponding test runner.

## 4. Verifying Saved Output Artifacts
When a dialogue task is completed and confirmed by the user:
- Resolve output directories using [result_paths.py](../../../src/dispatch/result_paths.py): `SEAGENT_TASK_DIR` / `SEAGENT_HISTORY_DIR` override the task/history destinations; otherwise use the result directory's corresponding child (without doubling an existing `task` or `history` suffix).
- `SEAGENT_RESULT_DIR` overrides the result root. When absent, the helper uses the configured default or repository fallback according to writability. Explicit invalid/unwritable overrides fail instead of silently switching directories.
- A published TaskIntent and saved history prove persistence. Inspect ROS dispatch status and robot `FINISH` / `FAIL` feedback separately before claiming execution completion.
