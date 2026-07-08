# Workspace Guidelines & Rules (`AGENTS.md`)

Welcome! This workspace contains a subsea multi-agent task planning system integrating local ASR (automatic speech recognition), fuzzy parameter extraction, and multi-layered physical/sea-state constraint checking. Please follow these guidelines:

## 1. Development Environment & Testing
* **Python Runtime**: Always execute scripts and tests using the `seagent` conda environment interpreter:
  `/root/miniconda3/envs/seagent/bin/python`
* **Test Command**: Run the complete unit test suite before making or concluding any change:
  ```bash
  /root/miniconda3/envs/seagent/bin/python -m unittest discover tests
  ```
* **Offline Execution**: The server operates in an offline mode. Do NOT attempt to download external model weights or access remote HF hubs during runtime. Keep environment variables `TRANSFORMERS_OFFLINE=1` and `HF_HUB_OFFLINE=1` active.

## 2. Directory Conventions
* `src/`: Core Python modules (dialogue management, parsers, validators, API clients).
* `config/`: System definitions (schemas, vessel assets, seabed maps, safety constraints, robot health indexes).
* `tests/`: Module unit tests.
* `index.html` (root): Frontend i18n web page.

## 3. Core Operational Rules
* **Simulated Time**: Never use absolute system time (`datetime.now()`) for task validity checking or user responses. Always use the simulated time module via `src/simulated_time.get_current_datetime()`.
* **Subsea Christmas Trees**: The system no longer distinguishes between "vertical" and "horizontal" subsea tree types. Avoid introducing or prompting for `tree_type` fields.
* **ASR Direct Switch**: The `direct_to_llm` toggle in `config/asr.yaml` defines whether raw text is sent straight to the dialogue manager. Maintain this behavior.
* **TaskIntent Persistence**: Upon task confirmation, a JSON intent file must be built using `TaskIntentBuilder` and saved in `/root/autodl-tmp/result/task` with the format `task_intent_TI<YYYYMMDD><seq>.json`.
