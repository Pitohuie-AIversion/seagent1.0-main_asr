import yaml
from pathlib import Path

fleet_path = Path("/root/mzy/seagent1.0-main_asr/config/robot_fleet.yaml")
assets_path = Path("/root/mzy/seagent1.0-main_asr/config/assets.yaml")

with open(fleet_path, "r", encoding="utf-8") as f:
    fleet_data = yaml.safe_load(f)

with open(assets_path, "r", encoding="utf-8") as f:
    assets_data = yaml.safe_load(f)

catalog = assets_data.get("payload_catalog", {})
catalog_names = set()
alias_to_canonical = {}
for pid, pinfo in catalog.items():
    name = pinfo.get("name")
    if name:
        catalog_names.add(name)
        alias_to_canonical[name] = name
    for a in pinfo.get("aliases", []):
        alias_to_canonical[a] = name

variants = fleet_data.get("model_variants", {})

print("==========================================================================")
print(" 机器人 onboard_payloads 与 supported_payloads 深度逻辑审计报告")
print("==========================================================================")

for vid, vinfo in variants.items():
    name = vinfo.get("full_name", vid)
    params = vinfo.get("hard_params", {})
    onboard = params.get("onboard_payloads", []) or []
    supported = params.get("supported_payloads", []) or []
    
    print(f"\n🤖 机器人规格: {name} ({vid})")
    print(f"   功率: {params.get('power_hp')} HP | 水深: {params.get('max_depth_m')}m | 口径: {params.get('diameter_mm')}")
    
    # 1. 检查 onboard 和 supported 重复
    onboard_set = set(onboard)
    supported_set = set(supported)
    intersection = onboard_set & supported_set
    if intersection:
        print(f"   🚨 [矛盾] onboard 和 supported 中重复包含以下载荷: {intersection}")
    else:
        print("   ✅ onboard 与 supported 无重叠重复")
        
    # 2. 检查载荷名是否在 catalog 中
    invalid_onboard = [p for p in onboard if p not in catalog_names and p not in alias_to_canonical]
    invalid_supported = [p for p in supported if p not in catalog_names and p not in alias_to_canonical]
    
    if invalid_onboard:
        print(f"   ⚠️ [标准名不符] onboard 中有载荷不在 assets.yaml 规范 catalog 中: {invalid_onboard}")
    if invalid_supported:
        print(f"   ⚠️ [标准名不符] supported 中有载荷不在 assets.yaml 规范 catalog 中: {invalid_supported}")

    # 3. 物理与工程合理性启发式检查
    all_payloads = onboard_set | supported_set
    
    # Check AUV (324cc)
    if "autonomous_underwater_vehicle" in vid:
        heavy_tools = {"高压水射流喷冲埋设模块", "机械切割开沟模块", "多功能液压机械臂", "电液机械臂", "双机液压机械臂", "海缆压埋装置"}
        found_heavy = all_payloads & heavy_tools
        if found_heavy:
            print(f"   🚨 [物理不合理] AUV (324mm口径) 包含了无法搭载的重型工装/机械臂: {found_heavy}")
            
    # Check 观察级 (75hp)
    if "observation_rov" in vid:
        burial_tools = {"高压水射流喷冲埋设模块", "机械切割开沟模块", "海缆压埋装置"}
        found_burial = all_payloads & burial_tools
        if found_burial:
            print(f"   🚨 [能力不匹配] 观察级ROV (75HP) 包含了无法驱动的重型埋设开沟模块: {found_burial}")
            
    # Check 履带式/拖曳式重载埋设机器人 (1600hp / 1500hp)
    if "crawler" in vid or "towed" in vid:
        tree_tools = {"三维视觉系统", "采油树专用飞翼", "液压剪切器"}
        # 查看是否有过度的采油树精密干预挂载

