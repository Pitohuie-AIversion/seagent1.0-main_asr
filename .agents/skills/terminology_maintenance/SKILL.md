---
name: terminology_maintenance
description: Procedures for maintaining, updating, or troubleshooting ASR vocabulary rules and standard oilfield entity definitions.
---

# Terminology & Oilfield Maintenance Skill

This skill guides you through adding, removing, or tweaking ASR vocabulary corrections and oilfield standard entities.

## 1. Updating ASR Vocabulary Corrections (`src/asr_normalizer.py`)
To add a new ASR term correction or edit an existing alias:
1. Open [asr_normalizer.py](file:///root/mzy/seagent1.0-main_asr/src/asr_normalizer.py).
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
4. Run tests in the `seagent` conda environment to verify matching logic:
   ```bash
   /root/miniconda3/envs/seagent/bin/python -m unittest tests/test_asr_normalizer.py
   ```

## 2. Maintaining Standard Oilfield Entities
Oilfield linking uses multiple features (pinyin, digit match, coordinates, aliases). To add a new oilfield:
1. **Define the metadata**:
   Open [environment.yaml](file:///root/mzy/seagent1.0-main_asr/config/environment.yaml) and add a new entry under `oil_fields`:
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
   If the new oilfield contains Chinese characters not already registered in [oilfield_linker.py](file:///root/mzy/seagent1.0-main_asr/src/oilfield_linker.py), you must add those characters and their pinyin representation to the `_PINYIN` dictionary inside `src/oilfield_linker.py` (e.g. line 42).
3. **Validate Entity Matching**:
   Verify matching by writing a test case in [test_oilfield_linker.py](file:///root/mzy/seagent1.0-main_asr/tests/test_oilfield_linker.py) and running it:
   ```bash
   /root/miniconda3/envs/seagent/bin/python -m unittest tests/test_oilfield_linker.py
   ```
