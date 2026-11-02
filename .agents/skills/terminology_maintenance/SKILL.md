---
name: terminology_maintenance
description: Procedures for maintaining, updating, or troubleshooting ASR vocabulary rules and standard oilfield entity definitions.
---

# Terminology & Oilfield Maintenance Skill

This skill guides you through adding, removing, or tweaking ASR vocabulary corrections, ASR configurations, and oilfield standard entities.

## 1. Updating ASR Vocabulary Corrections (`src/asr/asr_normalizer.py`)
To add a new ASR term correction or edit an existing alias:
1. Open [asr_normalizer.py](../../../src/asr/asr_normalizer.py).
2. Locate `TERM_RULES` (a tuple of `TermRule` instances).
3. Add a new `TermRule`:
   ```python
   TermRule(
       target="标准术语",
       aliases=["同音字1", "误听字2"],
       context_words=["触发词A", "触发词B"],
       category="类别名称",
       base_score=3,      # 默认 3
       threshold=4        # 默认 4
   )
   ```
### ASR Correction Scoring Workflow:
- The text is matched for the specified aliases.
- When an alias is matched, a scoring context window around the word is evaluated.
- Additional score points are added based on neighboring context words matching `context_words`.
- A candidate is eligible when its score is `>= rule.threshold` (default `4`; individual rules can override it). Each matching context word adds 2 points to `base_score`. Conflict resolution can still reject an ambiguous eligible candidate; inspect `_select_candidates()` when adding overlapping terms.
- Run tests using the active project Python environment to verify matching logic:
   ```bash
   python -m pytest tests/test_asr_normalizer.py -q
   ```

## 2. Maintaining Standard Oilfield Entities
Oilfield linking matches spoken inputs to oilfield database coordinates and properties. To add a new oilfield:
1. **Define the metadata**:
   Open [oilfield.yaml](../../../config/oilfield.yaml) and add a new entry under `oil_fields`:
   ```yaml
     - id: "oilfield_id"
       name: "标准油田名"
       aliases: ["别名1", "简称2"]
       lat_range: [18.0, 19.0]
       lon_range: [112.0, 113.0]
       seabed_type: "soft" # "soft" or "hard"
       notes: "海床相关背景信息描述"
   ```
2. **Update the Pinyin Mapping**:
   If the new oilfield contains Chinese characters not already registered in [oilfield_linker.py](../../../src/extraction/oilfield_linker.py), you must add those characters and their pinyin representation to the `_PINYIN` dictionary inside `src/extraction/oilfield_linker.py`.
3. **Validate Entity Matching**:
   Verify matching by writing a test case in [test_oilfield_linker.py](../../../tests/test_oilfield_linker.py) and running it:
   ```bash
   python -m pytest tests/test_oilfield_linker.py -q
   ```

### Oilfield Linker Scoring & Matching Criteria:
- Matches are evaluated using `OilfieldEntityLinker.link(raw_name, coords)` in [oilfield_linker.py](../../../src/extraction/oilfield_linker.py) against the following scoring framework:
  - **Exact name/alias match**: `95` points.
  - **Substring/inclusion match**: `82` points.
  - **Character similarity** (using `SequenceMatcher`): up to `35` points.
  - **Pinyin phonetic similarity**: up to `55` points.
  - **Numeric segment alignment** (e.g., "17-2" in "陵水17-2"): `28` points.
  - **Coordinate bounds match**: `40` points when inside the field bounds; otherwise `15` points when within 1 degree of the center in the implementation's latitude/longitude distance calculation. These are alternatives, not additive bonuses.
- **Acceptance Threshold**: An entity is auto-linked (`accepted`) if its score is `>= 75` AND it is at least `8` points higher than the second-best candidate. If it does not satisfy this, the result is marked as `ambiguous` or `unmatched`, and not auto-assigned.

## 3. Configuring ASR Model Settings (`config/asr.yaml`)
To adjust ASR models or pipeline options:
- Edit [asr.yaml](../../../config/asr.yaml):
  - `model_path`: Path pointing to the local ASR model (e.g. `/root/autodl-tmp/model/Qwen-asr-0.6B`).
  - `direct_to_llm`: Returned to the frontend as an auto-submit preference. When enabled, the frontend submits the transcript through the regular chat route; the ASR endpoint itself does not invoke `DialogueManager`.
  - `language`, `max_new_tokens`, `allowed_extensions`, and upload size limits.
- Core transcriber service is implemented under [asr_service.py](../../../src/asr/asr_service.py).
