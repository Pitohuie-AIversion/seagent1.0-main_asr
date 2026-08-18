import yaml
from pathlib import Path
from collections import defaultdict

fleet_path = Path("/root/mzy/seagent1.0-main_asr/config/robot_fleet.yaml")
with open(fleet_path, "r", encoding="utf-8") as f:
    data = yaml.safe_load(f)

classes = data.get("robot_classes", {})
families = data.get("robot_families", {})
variants = data.get("model_variants", {})
units = data.get("fleet_units", [])

print(f"Loaded: {len(classes)} classes, {len(families)} families, {len(variants)} variants, {len(units)} units")

alias_to_entities = defaultdict(list)

# 1. Classes
for cid, cinfo in classes.items():
    aliases = cinfo.get("aliases", []) or []
    full_name = cinfo.get("full_name", cid)
    for a in aliases:
        alias_to_entities[str(a)].append(("class", cid, full_name))

# 2. Families
for fid, finfo in families.items():
    aliases = finfo.get("aliases", []) or []
    full_name = finfo.get("full_name", fid)
    for a in aliases:
        alias_to_entities[str(a)].append(("family", fid, full_name))

# 3. Variants
for vid, vinfo in variants.items():
    aliases = vinfo.get("aliases", []) or []
    full_name = vinfo.get("full_name", vid)
    for a in aliases:
        alias_to_entities[str(a)].append(("variant", vid, full_name))

# 4. Units
for uinfo in units:
    uid = uinfo.get("unit_id")
    aliases = uinfo.get("aliases", []) or []
    disp_name = uinfo.get("display_name", uid)
    for a in aliases:
        alias_to_entities[str(a)].append(("unit", uid, disp_name))

print("\n--- ALL ALIASES AND ENTITIES ---")
for alias, entity_list in sorted(alias_to_entities.items(), key=lambda x: x[0]):
    if len(entity_list) > 1:
        print(f"⚠️ CONFLICT / MULTI-MAPPING ALIAS: '{alias}' -> {entity_list}")
    else:
        print(f"'{alias}' -> {entity_list[0]}")
