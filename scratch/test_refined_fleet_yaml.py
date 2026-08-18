import yaml
from pathlib import Path
from collections import defaultdict

# 读取当前的 robot_fleet.yaml
fleet_path = Path("/root/mzy/seagent1.0-main_asr/config/robot_fleet.yaml")
with open(fleet_path, "r", encoding="utf-8") as f:
    data = yaml.safe_load(f)

# 检查当前 bare serial aliases ("001", "1号机", "一号机") 碰撞的单机
bare_serials = {"001", "1号机", "一号机", "002", "2号机", "二号机"}

print("=== 1. 排查裸序号别名碰撞 ===")
units = data.get("fleet_units", [])
collided = defaultdict(list)
for u in units:
    uid = u.get("unit_id")
    for a in u.get("aliases", []):
        if a in bare_serials:
            collided[a].append(uid)

for alias, uids in collided.items():
    print(f"裸序号别名 '{alias}' 被多台单机共享绑定: {uids}")

print("\n=== 2. 排查 robot_classes 别名缺失 ===")
classes = data.get("robot_classes", {})
for cid, cinfo in classes.items():
    aliases = cinfo.get("aliases")
    print(f"Class '{cid}' aliases: {aliases}")

print("\n=== 3. 排查语法杂质与双连字符 ===")
for u in units:
    uid = u.get("unit_id")
    for a in u.get("aliases", []):
        if "--" in a:
            print(f"单机 '{uid}' 存在语法杂质别名: '{a}'")

print("\n=== 4. 排查 robot_class 归属错乱 ===")
families = data.get("robot_families", {})
for fid, finfo in families.items():
    rclass = finfo.get("robot_class")
    print(f"Family '{fid}' (full_name: {finfo.get('full_name')}) -> robot_class: '{rclass}'")
