import yaml
from pathlib import Path
from collections import defaultdict

fleet_path = Path("/root/mzy/seagent1.0-main_asr/config/robot_fleet.yaml")
with open(fleet_path, "r", encoding="utf-8") as f:
    data = yaml.safe_load(f)

classes = data["robot_classes"]
families = data["robot_families"]
variants = data["model_variants"]
units = data["fleet_units"]

# 目标：
# Class 包含抽象大类代称：["观察级ROV", "观察级", "巡检ROV"] 等
# Family observation_rov 不再包含与 Class 相同的裸词 "观察级", "观察级ROV"
# Class auv 包含抽象代称：["AUV", "水下无人自主航行器", "自主水下航行器", "无人潜航器"]
# Family autonomous_underwater_vehicle 不再包含与 Class 相同的裸词 "AUV", "水下无人自主航行器"

# 1. 调整 robot_classes 别名（纯粹抽象大类）
classes["cable_burial_robot"]["aliases"] = ["管缆埋设机器人", "埋缆机器人", "埋缆ROV", "海底埋设机器人", "埋缆设备"]
classes["work_class_rov"]["aliases"] = ["工作级ROV", "工作级机器人", "深海工作级ROV", "重载工作级ROV", "工作级"]
classes["observation_rov"]["aliases"] = ["观察级ROV", "观察级机器人", "观察型ROV", "巡检ROV", "观察级"]
classes["auv"]["aliases"] = ["AUV", "水下无人自主航行器", "自主水下航行器", "无人潜航器"]

# 2. 调整 robot_families 别名（剔除与 Class 逐字碰撞的裸词）
families["observation_rov"]["aliases"] = [
    "检查ROV", "深海观察机器人", "巡检机器人", "深海巡检ROV", 
    "深海巡检机器人", "深海观察型ROV", "深海观察型机器人", "深海观察级ROV", "深海观察级机器人"
]

families["autonomous_underwater_vehicle"]["aliases"] = [
    "自主型", "水下无人自主机器人", "自主潜航器", "调查型AUV", "水下巡检航行器", "自主型AUV"
]

# 校验四级集合别名碰撞
all_aliases = defaultdict(list)
for cid, c in classes.items():
    for a in c.get("aliases", []):
        all_aliases[a.strip().lower()].append(f"Class:{cid}")

for fid, f in families.items():
    for a in f.get("aliases", []):
        all_aliases[a.strip().lower()].append(f"Family:{fid}")

for vid, v in variants.items():
    for a in v.get("aliases", []):
        all_aliases[a.strip().lower()].append(f"Variant:{vid}")

for u in units:
    uid = u.get("unit_id")
    for a in u.get("aliases", []):
        all_aliases[a.strip().lower()].append(f"Unit:{uid}")

print("=== 四级集合别名碰撞排查结果 ===")
collisions = {a: targets for a, targets in all_aliases.items() if len(targets) > 1}
if not collisions:
    print("✅ 完美！四级别名无任何碰撞！")
else:
    for a, targets in collisions.items():
        print(f"🚨 碰撞词: '{a}' -> {targets}")
