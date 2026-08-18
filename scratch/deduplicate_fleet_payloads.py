import yaml

fleet_path = "config/robot_fleet.yaml"
data = yaml.safe_load(open(fleet_path, encoding="utf-8"))

for variant_id, variant in data.get("model_variants", {}).items():
    hp = variant.get("hard_params", {})
    if "onboard_payloads" in hp and isinstance(hp["onboard_payloads"], list):
        hp["onboard_payloads"] = list(dict.fromkeys(hp["onboard_payloads"]))
    if "supported_payloads" in hp and isinstance(hp["supported_payloads"], list):
        hp["supported_payloads"] = list(dict.fromkeys(hp["supported_payloads"]))

with open(fleet_path, "w", encoding="utf-8") as f:
    yaml.dump(data, f, allow_unicode=True, sort_keys=False)

print("Successfully deduplicated all payload lists in config/robot_fleet.yaml!")
