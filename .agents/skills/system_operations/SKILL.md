---
name: system_operations
description: Procedures for executing unit tests, starting the local dialogue server, simulating robot telemetry updates, and verifying system artifacts.
---

# System Operations & Verification Skill

This skill explains how to build, test, run, and update the subsea dialog application locally.

## 1. Environment & Running Tests
* **Conda Environment**: Always use the `/root/miniconda3/envs/seagent/bin/python` interpreter.
* **Testing Command**: Run the entire test suite before proposing or finalizing code edits:
  ```bash
  /root/miniconda3/envs/seagent/bin/python -m unittest discover tests
  ```
* **Offline Execution**: Ensure the model loading remains offline. Keep the environment variables `TRANSFORMERS_OFFLINE=1` and `HF_HUB_OFFLINE=1` active.

## 2. Running the Server & Port Forwarding
To start the Flask dialogue server and local models:
1. Navigate to the project root:
   ```bash
   cd /root/mzy/seagent1.0-main_asr
   ```
2. Launch the backend:
   ```bash
   /root/miniconda3/envs/seagent/bin/python run.py
   ```
3. To forward the local port for external Web access, run:
   ```bash
   /root/miniconda3/envs/seagent/bin/python port_forward.py
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
    "robot_name": "sealien_inspection",
    "params": {
      "current_velocity": 0.3,
      "turbidity": 3,
      "obstacle_density": "low",
      "mothership_support": "strong",
      "update_timestamp": "2026-07-13T14:00:00+08:00",
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
> The `update_timestamp` must match the current simulated time (queried from `src/simulated_time.py`). If the data is more than 1 hour old relative to the simulated time, the validator will mark the state as stale and block task execution.

## 4. Verifying Saved Output Artifacts
When a dialogue task is completed and confirmed by the user:
- Verify that the TaskIntent JSON has been successfully generated in:
  `/root/autodl-tmp/result/task/task_intent_TI<YYYYMMDD><seq>.json`
- Verify that the chat session history is archived in:
  `/root/autodl-tmp/result/history/history_<intent_id>.json`
