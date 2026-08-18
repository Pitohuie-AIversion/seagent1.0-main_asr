import yaml

fleet_path = "config/robot_fleet.yaml"
data = yaml.safe_load(open(fleet_path, encoding="utf-8"))

print("=========================================================================")
print("  ROBOT FLEET ALL PAYLOADS DOMAIN LOGIC AUDIT")
print("=========================================================================")

for variant_id, variant in data.get("model_variants", {}).items():
    full_name = variant.get("full_name")
    hp = variant.get("hard_params", {})
    onboard = hp.get("onboard_payloads", []) or []
    supported = hp.get("supported_payloads", []) or []

    print(f"\n🤖 [{variant_id}] ({full_name})")
    print("  🟢 onboard_payloads (自带标配):")
    for item in onboard:
        print(f"     - {item}")
    print("  🔵 supported_payloads (可选搭载):")
    for item in supported:
        print(f"     - {item}")
