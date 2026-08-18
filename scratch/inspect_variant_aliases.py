import yaml
from pathlib import Path
from collections import defaultdict

fleet_path = Path("/root/mzy/seagent1.0-main_asr/config/robot_fleet.yaml")
with open(fleet_path, "r", encoding="utf-8") as f:
    data = yaml.safe_load(f)

variants = data.get("model_variants", {})
families = data.get("robot_families", {})

print("=== 1. 检查 model_variants 之间的别名碰撞 ===")
variant_alias_map = defaultdict(list)
for vid, vinfo in variants.items():
    for a in vinfo.get("aliases", []):
        variant_alias_map[a.strip().lower()].append(vid)

for alias, vids in variant_alias_map.items():
    if len(vids) > 1:
        print(f"⚠️ Variant 别名碰撞: '{alias}' -> {vids}")

print("\n=== 2. 检查 model_variants 别名与 robot_families 别名的重叠 ===")
family_alias_set = set()
for fid, finfo in families.items():
    for a in finfo.get("aliases", []):
        family_alias_set.add(a.strip().lower())

for alias, vids in variant_alias_map.items():
    if alias in family_alias_set:
        print(f"⚠️ Variant 别名与 Family 别名重叠: '{alias}' (Variant: {vids})")

print("\n=== 3. 检查是否有带空格/不带空格，马力/匹/HP 等常用表达遗漏 ===")
for vid, vinfo in variants.items():
    aliases = vinfo.get("aliases", [])
    print(f"Variant '{vid}' ({vinfo.get('full_name')}): total aliases = {len(aliases)}")
