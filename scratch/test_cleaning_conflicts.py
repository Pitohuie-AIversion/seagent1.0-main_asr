import yaml
from pathlib import Path

fleet_path = Path("/root/mzy/seagent1.0-main_asr/config/robot_fleet.yaml")
with open(fleet_path, "r", encoding="utf-8") as f:
    data = yaml.safe_load(f)

# 方案调整：
# 1. robot_classes 放基础大类代称与包含“大类/类”的明确词
# 2. robot_families 精确剔除与大类逐字完全相同的碰撞别名

classes = data["robot_classes"]
families = data["robot_families"]

# 清理 Class 与 Family 冲突
classes["cable_burial_robot"]["aliases"] = ["管缆埋设大类", "埋缆机器人类", "埋缆ROV类", "海底埋设设备类"]
classes["work_class_rov"]["aliases"] = ["工作级ROV大类", "工作级机器人类", "深海工作级ROV类", "重载工作级类"]
classes["observation_rov"]["aliases"] = ["观察级ROV大类", "观察级机器人类", "观察型ROV类", "巡检ROV类"]
classes["auv"]["aliases"] = ["AUV大类", "水下无人自主航行器类", "自主水下航行器类", "无人潜航器类"]

# 校验依然冲突的项
from collections import defaultdict
c_aliases = {}
for cid, cinfo in classes.items():
    for a in cinfo.get("aliases", []):
        c_aliases[a.strip().lower()] = cid

f_aliases = defaultdict(list)
for fid, finfo in families.items():
    for a in finfo.get("aliases", []):
        f_aliases[a.strip().lower()].append(fid)

conflicts = []
for alias, cid in c_aliases.items():
    if alias in f_aliases:
        conflicts.append((alias, cid, f_aliases[alias]))

print(f"清理后冲突数量: {len(conflicts)}")
for c in conflicts:
    print(c)
