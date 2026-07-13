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

## 2. Directory Conventions & Code Mapping
* `src/`: Core Python modules.
  * [coord_parser.py](file:///root/mzy/seagent1.0-main_asr/src/coord_parser.py): Conversions for spoken/non-standard coordinates into standard (lat, lon).
  * [dialogue_manager.py](file:///root/mzy/seagent1.0-main_asr/src/dialogue_manager.py): Main dialogue controller and state machine.
  * [environment_info.py](file:///root/mzy/seagent1.0-main_asr/src/environment_info.py): Static environment and geofencing/bottom lock queries.
  * [extractor.py](file:///root/mzy/seagent1.0-main_asr/src/extractor.py): LLM-based field extraction and parameter diff generation.
  * [history_manager.py](file:///root/mzy/seagent1.0-main_asr/src/history_manager.py): Session/history snapshot writer.
  * [normalizer.py](file:///root/mzy/seagent1.0-main_asr/src/normalizer.py): Fuzzy matching value mapping tool.
  * [output_builder.py](file:///root/mzy/seagent1.0-main_asr/src/output_builder.py): Validates fields against schema, constructs flat JSON, tracks missing keys.
  * [prompts.py](file:///root/mzy/seagent1.0-main_asr/src/prompts.py): System/User message compilation for response generation.
  * [simulated_time.py](file:///root/mzy/seagent1.0-main_asr/src/simulated_time.py): Target for current simulated time queries.
  * [state_info.py](file:///root/mzy/seagent1.0-main_asr/src/state_info.py): Dynamic robot telemetry state tracking.
  * [task_intent_builder.py](file:///root/mzy/seagent1.0-main_asr/src/task_intent_builder.py): Compilation of finalized TaskIntent JSON.
  * [validator.py](file:///root/mzy/seagent1.0-main_asr/src/validator.py): Constraint-checking core.
  * [asr_normalizer.py](file:///root/mzy/seagent1.0-main_asr/src/asr_normalizer.py): ASR candidate word + context correction rules. (Note: Root `README.md` mistakenly refers to this as `context_rule_normalizer.py`).
  * [oilfield_linker.py](file:///root/mzy/seagent1.0-main_asr/src/oilfield_linker.py): Oilfield entity linker score & mapping core. (Note: Root `README.md` mistakenly refers to this as `oilfield_entity_linker.py`).
  * [asr_service.py](file:///root/mzy/seagent1.0-main_asr/src/asr_service.py): Local ASR transcription wrapper using Qwen ASR model.
* `config/`: System definitions and parameters.
  * [asr.yaml](file:///root/mzy/seagent1.0-main_asr/config/asr.yaml): ASR model parameters and model direct-to-llm toggle config.
  * [assets.yaml](file:///root/mzy/seagent1.0-main_asr/config/assets.yaml): Allowed tools, vessels, payloads.
  * [constraints.yaml](file:///root/mzy/seagent1.0-main_asr/config/constraints.yaml): Rule limits and logic constants.
  * [environment.yaml](file:///root/mzy/seagent1.0-main_asr/config/environment.yaml): Map definitions, oilfield bounds, and geofence areas.
  * [robot_fleet.yaml](file:///root/mzy/seagent1.0-main_asr/config/robot_fleet.yaml): Available ROV/AUV types and physical parameter capacities.
  * [state.yaml](file:///root/mzy/seagent1.0-main_asr/config/state.yaml): Initial state file.
  * [task_schemas.yaml](file:///root/mzy/seagent1.0-main_asr/config/task_schemas.yaml): Field templates for Normal and Emergency modes.
* `tests/`: Module unit tests.
* `index.html` (root): Frontend i18n web page.

## 3. Core Operational Rules & Logic

### 3.1 Simulated Time
* Never use absolute system time (`datetime.now()`) for task validity checking or user responses. Always use the simulated time module via `src/simulated_time.get_current_datetime()`.

### 3.2 Subsea Christmas Trees
* The system no longer distinguishes between "vertical" and "horizontal" subsea tree types. Avoid introducing or prompting for `tree_type` fields.

### 3.3 ASR Direct Switch
* The `direct_to_llm` toggle in `config/asr.yaml` defines whether raw text is sent straight to the dialogue manager. Maintain this behavior.

### 3.4 TaskIntent & History Persistence
* Upon task confirmation, a JSON intent file must be built using `TaskIntentBuilder` and saved in `/root/autodl-tmp/result/task` with the format `task_intent_TI<YYYYMMDD><seq>.json`.
* Complete chat session snapshots must be saved to `/root/autodl-tmp/result/history` using the format `history_<intent_id>.json`.

### 3.5 Dialogue State Machine Transitions & Refusal Limit
Dialogue managers transition through the following states:
- `collecting`: Actively querying for missing fields.
- `blocked_hard`: A hard constraint is violated. Blocked until the parameter is modified.
  - **Hard Refusal Count**: The system tracks consecutive hard violation turns. If the violation remains uncorrected for **4 consecutive turns**, the task transitions to `rejected`.
  - **Final Refusal Warning**: At 3 consecutive turns, the state changes to `hard_final_warning` to alert the user.
- `blocked_soft`: A soft constraint risk is flagged. Proceeds once user confirms/ignores the warning.
- `confirming`: All required fields collected. Waiting for final confirmation.
- `done`: Final intent JSON generated and persistent.
- `rejected`: Task cancelled or rejected.

### 3.6 Immediate vs. Future Task Validation
- **Immediate Task**: Defined as having a start time within 10 minutes of the current simulated time. Must undergo a full set of checks including parameters, static environment, and real-time robot state telemetry.
- **Future Task**: Starts > 10 minutes in the future. Requires check for parameters and static environment only; real-time dynamic states checks are bypassed.

### 3.7 Data Freshness Rules
- Environmental or robot state telemetry data must have an update timestamp within 1 hour of the simulated time. If the timestamp is older than 1 hour, it is considered stale/expired, and the validator must block the task run.

### 3.8 Emergency Mode Field Collection
- If the user uses "emergency", "urgent", "加急", "急", or similar keywords, the system switches to `emergency` mode.
- In emergency mode, a reduced subset of essential parameters is collected instead of the full set (configured in `config/task_schemas.yaml`).

### 3.9 ASR Candidate & Context Corrections
* Rules for ASR corrections are defined in [asr_normalizer.py](file:///root/mzy/seagent1.0-main_asr/src/asr_normalizer.py) as `TermRule` instances.
* A score-based matching algorithm decides whether a candidate word in the transcribed audio should be corrected:
  - If target word aliases are found in raw transcription, score is calculated based on nearby context words.
  - If total matching score is `>= 5`, the text segment is corrected to standard `target` word; otherwise, it is left unchanged.

### 3.10 Oilfield Entity Linker Scoring Matrix
* Oilfield matching is performed using the `OilfieldEntityLinker.link(raw_name, coords)` method in [oilfield_linker.py](file:///root/mzy/seagent1.0-main_asr/src/oilfield_linker.py).
* The linker queries standard oilfields in [environment.yaml](file:///root/mzy/seagent1.0-main_asr/config/environment.yaml) using the following scoring criteria:
  - Exact match of name/alias: `95` points.
  - String inclusion relationship: `82` points.
  - Character similarity (using `SequenceMatcher`): up to `35` points.
  - Pinyin similarity (using homophones/near-homophones mapping): up to `55` points.
  - Number segment match: `28` points.
  - Coordinate verification (points fall in range): `40` points (OR `15` points proximity bonus if outside range but distance to center is `<= 1.0`).
* Acceptance criteria: The candidate oilfield is auto-accepted if its score is `>= 75` AND it is at least `8` points higher than the runner-up. Otherwise, it is marked as `ambiguous` or `unmatched`.

### 3.11 Dialogue Query Shortcuts
* **Identity Queries**: If the user inputs identity-related queries (e.g. `"你是谁"`, `"自我介绍"`, `"你叫什么"`), the system bypasses dialogue cycle evaluation and directly responds with the business description statement.
* **Standalone Time Queries**: If the user queries standalone simulated time context, it directly returns the simulated datetime content.
