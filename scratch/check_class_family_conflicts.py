import yaml
from pathlib import Path
from collections import defaultdict

fleet_path = Path("/root/mzy/seagent1.0-main_asr/config/robot_fleet.yaml")
with open(fleet_path, "r", encoding="utf-8") as f:
    data = yaml.safe_load(f)

classes = data.get("robot_classes", {})
families = data.get("robot_families", {})

print("=== 1. robot_classes 内部及与 robot_families 的别名重叠检测 ===")

# 收集 class 别名
class_aliases = {}
for cid, cinfo in classes.items():
    for a in cinfo.get("aliases", []):
        class_aliases[a.strip().lower()] = cid

# 收集 family 别名
family_aliases = defaultdict(list)
for fid, finfo in families.items():
    for a in finfo.get("aliases", []):
        family_aliases[a.strip().lower()].append(fid)

print("--- [Class 与 Family 别名冲突] ---")
for alias, cid in class_aliases.items():
    if alias in family_aliases:
        print(f"🚨 冲突: 别名 '{alias}' 同时存在于 Class '{cid}' 和 Family {family_aliases[alias]}")

print("\n--- [Family 之间别名冲突] ---")
for alias, fids in family_aliases.items():
    if len(fids) > 1:
        print(f"🚨 冲突: 别名 '{alias}' 被多个 Family 共享: {fids}")
