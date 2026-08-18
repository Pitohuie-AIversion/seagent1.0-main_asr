import yaml
from pathlib import Path

fleet_path = Path("/root/mzy/seagent1.0-main_asr/config/robot_fleet.yaml")
assets_path = Path("/root/mzy/seagent1.0-main_asr/config/assets.yaml")
task_schemas_path = Path("/root/mzy/seagent1.0-main_asr/config/task_schemas.yaml")

with open(fleet_path, "r", encoding="utf-8") as f:
    fleet_data = yaml.safe_load(f)

with open(assets_path, "r", encoding="utf-8") as f:
    assets_data = yaml.safe_load(f)

with open(task_schemas_path, "r", encoding="utf-8") as f:
    task_data = yaml.safe_load(f)

task_rules = assets_data.get("task_payload_rules", {})
task_templates = task_data.get("task_templates", {})
variants = fleet_data.get("model_variants", {})
families = fleet_data.get("robot_families", {})

print("==========================================================================")
print(" 任务类型 <-> 允许机器人 <-> 载荷覆盖率全矩阵交叉校验")
print("==========================================================================")

for task_key, tinfo in task_templates.items():
    task_name = tinfo.get("display_name", task_key)
    allowed_classes = tinfo.get("allowed_robot_classes", [])
    required_payloads = task_rules.get(task_key, {}).get("common", [])
    
    print(f"\n📋 任务: 【{task_name}】({task_key})")
    print(f"   支持机器人类别: {allowed_classes}")
    print(f"   常规典型需求载荷: {required_payloads}")
    
    # 查找属于 allowed_classes 的所有型规格
    matching_variants = []
    for vid, vinfo in variants.items():
        fid = vinfo.get("family_id")
        family = families.get(fid, {})
        rclass = family.get("robot_class")
        if rclass in allowed_classes:
            matching_variants.append((vid, vinfo))
            
    print(f"   匹配的机器人规格数: {len(matching_variants)}")
    for vid, vinfo in matching_variants:
        p_params = vinfo.get("hard_params", {})
        onboard = set(p_params.get("onboard_payloads", []) or [])
        supported = set(p_params.get("supported_payloads", []) or [])
        all_p = onboard | supported
        
        missing = [p for p in required_payloads if p not in all_p]
        if missing:
            print(f"   ⚠️ 规格 {vinfo.get('full_name')} 缺少任务所需载荷: {missing}")
        else:
            print(f"   ✅ 规格 {vinfo.get('full_name')} 100% 覆盖该任务所有典型载荷需求！")
