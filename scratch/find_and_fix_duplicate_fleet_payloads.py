import yaml

fleet_path = "config/robot_fleet.yaml"
data = yaml.safe_load(open(fleet_path, encoding="utf-8"))

print("=== Checking Duplicate Payloads in robot_fleet.yaml ===")

for variant_id, variant in data.get("model_variants", {}).items():
    hp = variant.get("hard_params", {})
    onboard = hp.get("onboard_payloads", []) or []
    supported = hp.get("supported_payloads", []) or []

    # Check duplicates in onboard
    seen_onboard = set()
    dup_onboard = []
    for item in onboard:
        if item in seen_onboard:
            dup_onboard.append(item)
        seen_onboard.add(item)

    # Check duplicates in supported
    seen_supported = set()
    dup_supported = []
    for item in supported:
        if item in seen_supported:
            dup_supported.append(item)
        seen_supported.add(item)

    # Check items in both onboard and supported
    overlap = seen_onboard.intersection(seen_supported)

    if dup_onboard or dup_supported or overlap:
        print(f"\n[Variant: {variant_id}] ({variant.get('full_name')})")
        if dup_onboard:
            print("  - Duplicates in onboard_payloads:", dup_onboard)
        if dup_supported:
            print("  - Duplicates in supported_payloads:", dup_supported)
        if overlap:
            print("  - Overlap between onboard and supported:", list(overlap))
