import yaml

fleet_path = "config/robot_fleet.yaml"
data = yaml.safe_load(open(fleet_path, encoding="utf-8"))

arm_payloads = {"多功能液压机械臂", "电液机械臂", "夹爪"}

print("=== Moving Arm Payloads to onboard_payloads for Work-Class Robots ===")

for variant_id, variant in data.get("model_variants", {}).items():
    family_id = str(variant.get("family_id", ""))
    full_name = str(variant.get("full_name", ""))
    
    is_work_class = "work_class" in variant_id or "work_class" in family_id or "工作级" in full_name
    if not is_work_class:
        continue
    
    hp = variant.get("hard_params", {})
    onboard = hp.get("onboard_payloads", []) or []
    supported = hp.get("supported_payloads", []) or []
    
    arms_to_move = [p for p in supported if p in arm_payloads]
    
    if arms_to_move:
        print(f"\n[Variant: {variant_id}] Moving arms to onboard: {arms_to_move}")
        # Remove from supported
        hp["supported_payloads"] = [p for p in supported if p not in arm_payloads]
        # Add to onboard if not already present
        for arm in arms_to_move:
            if arm not in onboard:
                onboard.append(arm)
        hp["onboard_payloads"] = onboard

# Write back cleaner formatted YAML while preserving line breaks
with open(fleet_path, "w", encoding="utf-8") as f:
    yaml.dump(data, f, allow_unicode=True, sort_keys=False)

print("\nSuccessfully updated config/robot_fleet.yaml!")
