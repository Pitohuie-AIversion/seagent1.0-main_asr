import yaml

fleet_path = "config/robot_fleet.yaml"
data = yaml.safe_load(open(fleet_path, encoding="utf-8"))

arm_keywords = ["机械臂", "臂", "夹爪"]

print("=== Work-Class Robots Payload Inspection ===")

for variant_id, variant in data.get("model_variants", {}).items():
    family_id = str(variant.get("family_id", ""))
    full_name = str(variant.get("full_name", ""))
    
    is_work_class = "work_class" in variant_id or "work_class" in family_id or "工作级" in full_name
    print(f"\n[Variant: {variant_id}] (is_work_class={is_work_class}) ({full_name})")
    
    hp = variant.get("hard_params", {})
    onboard = hp.get("onboard_payloads", []) or []
    supported = hp.get("supported_payloads", []) or []
    
    print("  - Current onboard_payloads:", onboard)
    print("  - Current supported_payloads:", supported)
    
    onboard_arms = [p for p in onboard if any(k in p for k in arm_keywords)]
    supported_arms = [p for p in supported if any(k in p for k in arm_keywords)]
    
    print("  -> Arm payloads in onboard:", onboard_arms)
    print("  -> Arm payloads in supported:", supported_arms)
